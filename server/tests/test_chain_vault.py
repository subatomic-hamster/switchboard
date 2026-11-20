"""`ChainVault` against the real `Vault.sol` on a local anvil node.

Skipped when `anvil` is not on PATH (put `~/.foundry/bin` there). Needs `forge build` in
`contracts/` first. The keys are anvil's public development keys.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from web3 import Web3
from web3.types import RPCEndpoint

from app.chain import transfer
from app.chain.vault import ARTIFACT, ChainVault, VaultRevert
from app.config import REPO_ROOT, Settings
from app.context import AppContext
from app.db import Database
from app.main import create_app
from app.schemas import Chain as ArcChain
from app.schemas import Reason
from tests.conftest import make_settings
from tests.test_stream import SECRET, sign

pytestmark = [
    pytest.mark.anvil,
    pytest.mark.skipif(shutil.which("anvil") is None, reason="anvil is not on PATH"),
]

KEYS = {
    "owner": "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
    "agent": "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
    "approver": "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",
}
PAYEE = "0x90F79bf6EB2c4f870365E785982E1f101E93b906"
NEW_PAYEE = "0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65"
SAM_ARC = "0x4ccFA9e40f035038edfe5b6A5c5111993B1bEFfA"  # in the wallet book
SOLANA = "Sam11111111111111111111111111111111111111111"
CHAIN_ID = 31337
MUSD = 10**6


class Chain:
    def __init__(self, url: str, w3: Web3, usdc: Any, vault: Any) -> None:
        self.url, self.w3, self.usdc, self.vault = url, w3, usdc, vault
        self.agent = w3.eth.account.from_key(KEYS["agent"]).address

    def rpc(self, method: str, *params: object) -> Any:
        return self.w3.provider.make_request(RPCEndpoint(method), list(params)).get("result")

    def balance(self, address: str) -> Decimal:
        return Decimal(self.usdc.functions.balanceOf(address).call()) / MUSD

    def owner_send(self, fn: Any) -> None:
        w3 = self.w3
        owner = w3.eth.account.from_key(KEYS["owner"])
        tx = fn.build_transaction(
            {"from": owner.address, "nonce": w3.eth.get_transaction_count(owner.address)}
        )
        receipt = w3.eth.wait_for_transaction_receipt(
            w3.eth.send_raw_transaction(owner.sign_transaction(tx).raw_transaction)
        )
        assert receipt["status"] == 1

    def settings(self, **extra: object) -> Settings:
        return make_settings(
            vault_address=self.vault.address,
            arc_rpc_url=self.url,
            arc_chain_id=CHAIN_ID,
            owner_private_key=KEYS["owner"],
            agent_private_key=KEYS["agent"],
            approver_private_key=KEYS["approver"],
            quicknode_stream_secret=SECRET,
            **extra,
        )


def _artifact(path: str) -> dict[str, Any]:
    file = REPO_ROOT / "contracts" / "out" / path
    if not file.exists():
        pytest.fail(f"{file} is missing: run `forge build` in contracts/")
    data: dict[str, Any] = json.loads(file.read_text(encoding="utf-8"))
    return data


@pytest.fixture(scope="module")
def chain() -> Iterator[Chain]:
    assert ARTIFACT.exists() or pytest.fail("run `forge build` in contracts/")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(  # noqa: S603 - fixed arguments
        [shutil.which("anvil") or "anvil", "--port", str(port), "--silent"],
        stdout=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    w3 = Web3(Web3.HTTPProvider(url))
    try:
        for _ in range(100):
            if w3.is_connected():
                break
            time.sleep(0.1)
        else:
            pytest.fail("anvil did not start")
        owner = w3.eth.account.from_key(KEYS["owner"])
        approver = w3.eth.account.from_key(KEYS["approver"])
        agent = w3.eth.account.from_key(KEYS["agent"])

        def deploy(art: dict[str, Any], *args: object) -> Any:
            factory = w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"]["object"])
            tx = factory.constructor(*args).build_transaction(
                {"from": owner.address, "nonce": w3.eth.get_transaction_count(owner.address)}
            )
            receipt = w3.eth.wait_for_transaction_receipt(
                w3.eth.send_raw_transaction(owner.sign_transaction(tx).raw_transaction)
            )
            return w3.eth.contract(address=receipt["contractAddress"], abi=art["abi"])

        usdc = deploy(_artifact("Vault.t.sol/MockUSDC.json"))
        vault = deploy(
            _artifact("Vault.sol/Vault.json"), usdc.address, owner.address, approver.address
        )
        result = Chain(url, w3, usdc, vault)
        result.owner_send(usdc.functions.mint(vault.address, 1_000 * MUSD))
        result.owner_send(vault.functions.setPolicy(agent.address, 100 * MUSD, 500 * MUSD, 3600))
        result.owner_send(vault.functions.setPayeeAllowed(agent.address, PAYEE, True))
        result.owner_send(vault.functions.setPayeeAllowed(agent.address, SAM_ARC, True))
        yield result
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture(autouse=True)
def _revert_chain(chain: Chain) -> Iterator[None]:
    """Each test starts from the same chain state."""
    snapshot = chain.rpc("evm_snapshot")
    yield
    chain.rpc("evm_revert", snapshot)


@pytest.fixture
def vault(chain: Chain) -> ChainVault:
    return ChainVault(chain.settings(), [PAYEE, SOLANA])


async def test_permit_then_redeem_pays_the_payee(chain: Chain, vault: ChainVault) -> None:
    result = await vault.request_spend(PAYEE, Decimal("50"), "invoice 1")
    assert result.permitted and result.permit_id is not None and result.request_id is None
    assert chain.balance(PAYEE) == 0
    await vault.redeem(result.permit_id)
    assert chain.balance(PAYEE) == 50
    assert await vault.spent_today() == Decimal("50")
    with pytest.raises(VaultRevert) as revert:
        await vault.redeem(result.permit_id)
    assert revert.value.error == "PermitUsedOrUnknown"


async def test_over_the_cap_opens_a_request(chain: Chain, vault: ChainVault) -> None:
    result = await vault.request_spend(PAYEE, Decimal("150.25"), "big invoice")
    assert not result.permitted
    assert result.reason is Reason.OVER_TRANSFER_CAP
    assert result.request_id == "1"
    assert chain.balance(PAYEE) == 0


async def test_unlisted_payee_and_budget_reasons(chain: Chain, vault: ChainVault) -> None:
    result = await vault.request_spend(NEW_PAYEE, Decimal("5"), "new payee")
    assert result.reason is Reason.PAYEE_NOT_ALLOWED
    for _ in range(5):
        permit = await vault.request_spend(PAYEE, Decimal("100"), "fill the budget")
        assert permit.permitted
    assert (await vault.request_spend(PAYEE, Decimal("1"), "one more")).reason is (
        Reason.OVER_DAILY_BUDGET
    )


async def test_approve_pays_at_once_and_redeem_is_a_no_op(chain: Chain, vault: ChainVault) -> None:
    result = await vault.request_spend(PAYEE, Decimal("250"), "big invoice")
    assert result.request_id is not None
    permit_id = await vault.approve(result.request_id)
    assert permit_id == "1"
    assert chain.balance(PAYEE) == 250
    await vault.redeem(permit_id)  # the contract already paid; nothing is sent
    assert chain.balance(PAYEE) == 250
    with pytest.raises(VaultRevert) as revert:
        await vault.approve(result.request_id)
    assert revert.value.error == "NotPending"
    assert isinstance(revert.value, ValueError)


async def test_deny_resolves_without_paying(chain: Chain, vault: ChainVault) -> None:
    result = await vault.request_spend(PAYEE, Decimal("250"), "big invoice")
    assert result.request_id is not None
    await vault.deny(result.request_id, via="test")
    assert chain.balance(PAYEE) == 0
    with pytest.raises(VaultRevert) as revert:
        await vault.approve(result.request_id)
    assert revert.value.error == "NotPending"


async def test_expire_only_after_the_deadline(chain: Chain, vault: ChainVault) -> None:
    result = await vault.request_spend(PAYEE, Decimal("250"), "big invoice")
    assert result.request_id is not None
    with pytest.raises(VaultRevert) as revert:
        await vault.expire(result.request_id)
    assert revert.value.error == "DeadlineNotPassed"
    chain.rpc("evm_increaseTime", 3601)
    chain.rpc("evm_mine")
    await vault.expire(result.request_id)
    with pytest.raises(VaultRevert):
        await vault.approve(result.request_id)


async def test_freeze_blocks_and_only_the_owner_unfreezes(chain: Chain, vault: ChainVault) -> None:
    await vault.freeze(True, via="test")  # approver key
    assert await vault.is_frozen()
    result = await vault.request_spend(PAYEE, Decimal("1"), "while frozen")
    assert result.reason is Reason.FROZEN
    assert result.request_id is None and result.permit_id is None
    await vault.freeze(False, via="test")  # owner key
    assert not await vault.is_frozen()
    assert (await vault.request_spend(PAYEE, Decimal("1"), "after")).permitted


async def test_set_policy_changes_limits_and_the_allowlist(chain: Chain, vault: ChainVault) -> None:
    before = await vault.get_policy()
    assert before.transfer_cap_usd == 100
    assert before.daily_budget_usd == 500
    assert before.approval_expiry_s == 3600
    assert before.allowlist == [PAYEE, SOLANA]  # SAM_ARC is not in this vault's wallet list

    changed = before.model_copy(
        update={"transfer_cap_usd": Decimal("300"), "allowlist": [*before.allowlist, NEW_PAYEE]}
    )
    await vault.set_policy(changed, via="test")

    after = await vault.get_policy()
    assert after.transfer_cap_usd == 300
    assert after.allowlist == [PAYEE, NEW_PAYEE, SOLANA]
    assert chain.vault.functions.payeeAllowed(chain.agent, NEW_PAYEE).call()
    assert (await vault.request_spend(NEW_PAYEE, Decimal("250"), "now allowed")).permitted

    removed = after.model_copy(update={"allowlist": [NEW_PAYEE, SOLANA]})
    await vault.set_policy(removed, via="test")
    assert not chain.vault.functions.payeeAllowed(chain.agent, PAYEE).call()
    assert (await vault.get_policy()).allowlist == [NEW_PAYEE, SOLANA]


async def test_a_non_evm_payee_is_paid_through_the_agent_wallet(
    chain: Chain, vault: ChainVault
) -> None:
    chain.owner_send(chain.vault.functions.setPayeeAllowed(chain.agent, chain.agent, True))
    result = await vault.request_spend(SOLANA, Decimal("20"), "to Sam on Solana")
    assert result.permit_id is not None
    await vault.redeem(result.permit_id)
    assert chain.balance(chain.agent) == 20


async def test_an_evm_payee_on_another_chain_is_paid_through_the_agent_wallet(
    chain: Chain, vault: ChainVault
) -> None:
    """A Sepolia address looks like an Arc address. Without the chain hint the vault would pay
    it on Arc, where the transfer module would never carry it across."""
    chain.owner_send(chain.vault.functions.setPayeeAllowed(chain.agent, chain.agent, True))
    before = chain.balance(PAYEE)
    result = await vault.request_spend(
        PAYEE, Decimal("20"), "to Sam on Sepolia", ArcChain.ETH_SEPOLIA
    )
    assert result.permit_id is not None
    await vault.redeem(result.permit_id)
    assert chain.balance(chain.agent) == 20
    assert chain.balance(PAYEE) == before


async def test_an_evm_payee_on_arc_is_still_paid_directly(chain: Chain, vault: ChainVault) -> None:
    before = chain.balance(PAYEE)
    result = await vault.request_spend(PAYEE, Decimal("20"), "to Sam on Arc", ArcChain.ARC_TESTNET)
    assert result.permit_id is not None
    await vault.redeem(result.permit_id)
    assert chain.balance(PAYEE) == before + 20
    assert chain.balance(chain.agent) == 0


async def test_amounts_below_a_micro_dollar_are_refused(vault: ChainVault) -> None:
    with pytest.raises(ValueError, match="decimal places"):
        await vault.request_spend(PAYEE, Decimal("0.0000001"), "dust")


# ---------------------------------------------------------------------- end to end


@pytest.fixture
async def real_app(chain: Chain) -> AsyncIterator[FastAPI]:
    settings = chain.settings(use_p1_stubs=False)
    application = create_app(settings, Database(None))
    yield application
    await application.state.ctx.aclose()


async def test_over_limit_plan_through_planner_and_approval(
    chain: Chain, real_app: FastAPI
) -> None:
    """Plan -> needs_approval -> the Stream's log is delivered -> owner approves -> payee paid."""
    ctx: AppContext = real_app.state.ctx
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app), base_url="http://localhost"
    ) as http:

        async def deliver() -> None:
            logs = [
                {
                    "address": entry["address"],
                    "topics": ["0x" + bytes(t).hex() for t in entry["topics"]],
                    "data": "0x" + bytes(entry["data"]).hex(),
                    "transactionHash": "0x" + bytes(entry["transactionHash"]).hex(),
                    "logIndex": hex(entry["logIndex"] + 1000 * entry["blockNumber"]),
                }
                for entry in chain.w3.eth.get_logs({"address": chain.vault.address, "fromBlock": 0})
            ]
            body = json.dumps({"logs": logs}).encode()
            response = await http.post("/api/hooks/quicknode", content=body, headers=sign(body))
            assert response.status_code == 200

        plan = (
            await http.post(
                "/api/intent", json={"text": "send 250 from my arc wallet to sam's arc wallet"}
            )
        ).json()
        assert plan["preview"]["reason"] == "OVER_TRANSFER_CAP"
        body = (await http.post(f"/api/plans/{plan['plan_id']}/execute", json={})).json()
        assert body["status"] == "needs_approval"
        assert body["reason"] == "OVER_TRANSFER_CAP"
        request_id = body["request_id"]
        assert chain.balance(SAM_ARC) == 0

        await deliver()  # what QuickNode does after the block
        requested = ctx.bus.latest("approval.requested")
        assert requested is not None
        assert requested.data["request_id"] == request_id
        assert requested.data["amount_usd"] == 250
        row = ctx.approvals.get(request_id)
        assert row is not None and row.status == "pending"

        result = await ctx.approvals.approve(request_id, Decimal(250), row.challenge)
        assert result.ok and result.status == "approved"
        assert chain.balance(SAM_ARC) == 250
        assert ctx.bus.latest("transfer.sent") is not None  # the planner's, not the Stream's

        await deliver()
        resolved = ctx.bus.latest("approval.resolved")
        assert resolved is not None
        assert resolved.data == {"request_id": request_id, "approved": True, "via": "chain"}


async def test_a_cross_chain_plan_moves_money_vault_to_agent_to_burn(
    chain: Chain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The money flow end to end, on a local chain with a mock TokenMessenger: the vault pays the
    agent wallet (never the Sepolia-looking payee on Arc), and the agent burns it."""
    art = _artifact("MockTokenMessenger.sol/MockTokenMessenger.json")
    factory = chain.w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"]["object"])
    owner = chain.w3.eth.account.from_key(KEYS["owner"])
    tx = factory.constructor().build_transaction(
        {"from": owner.address, "nonce": chain.w3.eth.get_transaction_count(owner.address)}
    )
    receipt = chain.w3.eth.wait_for_transaction_receipt(
        chain.w3.eth.send_raw_transaction(owner.sign_transaction(tx).raw_transaction)
    )
    messenger = str(receipt["contractAddress"])
    monkeypatch.setattr(transfer, "TOKEN_MESSENGER", messenger)
    # In the wallet book, on eth-sepolia.
    sam_eth = str(Web3.to_checksum_address("0x5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a"))
    chain.owner_send(chain.vault.functions.setPayeeAllowed(chain.agent, chain.agent, True))
    chain.owner_send(chain.vault.functions.setPayeeAllowed(chain.agent, sam_eth, True))
    chain.owner_send(chain.usdc.functions.mint(chain.agent, 1 * MUSD))  # the agent's fee float

    settings = chain.settings(
        use_p1_stubs=False,
        use_real_transfers=True,
        iris_url="https://iris.test",
        usdc_address=chain.usdc.address,
    )
    application = create_app(settings, Database(None))
    ctx: AppContext = application.state.ctx
    fees = [{"finalityThreshold": 2000, "minimumFee": 0, "forwardFee": {"med": 60000}}]
    done = {
        "messages": [
            {
                "status": "complete",
                "forwardState": "COMPLETE",
                "destinationMintTxHash": "0x" + "cd" * 32,
            }
        ]
    }
    try:
        with respx.mock:
            respx.get("https://iris.test/v2/burn/USDC/fees/26/0").mock(
                return_value=httpx.Response(200, json=fees)
            )
            respx.get("https://iris.test/v2/messages/26").mock(
                return_value=httpx.Response(200, json=done)
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://localhost"
            ) as http:
                plan = (
                    await http.post(
                        "/api/intent",
                        json={"text": "send 10 from my arc wallet to sam's ethereum wallet"},
                    )
                ).json()
                assert plan["preview"]["outcome"] == "pay"
                assert {s["rail"] for r in plan["routes"] for s in r["steps"]} == {"cctp-standard"}
                body = (await http.post(f"/api/plans/{plan['plan_id']}/execute", json={})).json()
                assert body["status"] == "executing"
                await asyncio.gather(
                    *[t for t in asyncio.all_tasks() if t.get_name().startswith("cctp-")]
                )
        assert chain.balance(sam_eth) == 0  # never paid on Arc
        assert chain.balance(messenger) == Decimal("10.06")  # 10 from the vault, 0.06 float
        assert chain.balance(chain.agent) == Decimal("0.94")
        assert chain.vault.functions.spentInWindow(chain.agent).call() == 10 * MUSD
        sent = ctx.bus.latest("transfer.sent")
        assert sent is not None and sent.data["tx_hash"].startswith("0x")
        assert sent.data["to_chain"] == "eth-sepolia"
        arrived = ctx.bus.latest("transfer.arrived")
        assert arrived is not None and arrived.data["tx_hash"] == "0x" + "cd" * 32
        assert ctx.bus.latest("transfer.attested") is not None
    finally:
        await ctx.aclose()
