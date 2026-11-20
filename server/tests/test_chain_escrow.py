"""`ChainAnchor` and `ChainEscrow` against the real `Ledger.sol` and `Escrow.sol` on anvil.

Skipped when `anvil` is not on PATH (put `~/.foundry/bin` there). Needs `forge build` in
`contracts/` first. The keys are anvil's public development keys.
"""

from __future__ import annotations

import hashlib
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
from fastapi import FastAPI
from web3 import Web3
from web3.exceptions import ContractCustomError

from app.chain.anchor import ChainAnchor
from app.chain.contract import ContractRevert
from app.chain.escrow import ChainEscrow
from app.context import AppContext
from app.db import Database
from app.main import create_app
from tests.test_chain_vault import KEYS, MUSD, PAYEE, Chain, _artifact
from tests.test_markets_desk import NBA, THREE_HIGH, use_models
from tests.test_stream import sign

pytestmark = [
    pytest.mark.anvil,
    pytest.mark.skipif(shutil.which("anvil") is None, reason="anvil is not on PATH"),
]

ATTACKER_KEY = "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba"
TUNNEL = {"Host": "random-words.trycloudflare.com", "Cf-Connecting-Ip": "203.0.113.9"}


class Deployed:
    def __init__(self, chain: Chain, ledger: Any, escrow: Any) -> None:
        self.chain, self.ledger, self.escrow = chain, ledger, escrow

    def settings(self, **extra: object) -> Any:
        return self.chain.settings(
            usdc_address=self.chain.usdc.address,
            ledger_address=self.ledger.address,
            escrow_address=self.escrow.address,
            **extra,
        )


@pytest.fixture(scope="module")
def deployed() -> Iterator[Deployed]:
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
        ledger = deploy(_artifact("Ledger.sol/Ledger.json"), owner.address)
        escrow = deploy(_artifact("Escrow.sol/Escrow.json"), usdc.address)
        chain = Chain(url, w3, usdc, vault)
        chain.owner_send(vault.functions.setPolicy(agent.address, 100 * MUSD, 500 * MUSD, 3600))
        chain.owner_send(ledger.functions.setWriter(agent.address, True))
        chain.owner_send(usdc.functions.mint(agent.address, 1_000 * MUSD))
        yield Deployed(chain, ledger, escrow)
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture(autouse=True)
def _revert_chain(deployed: Deployed) -> Iterator[None]:
    snapshot = deployed.chain.rpc("evm_snapshot")
    yield
    deployed.chain.rpc("evm_revert", snapshot)


@pytest.fixture
def anchor(deployed: Deployed) -> ChainAnchor:
    return ChainAnchor(deployed.settings())


@pytest.fixture
def escrow(deployed: Deployed) -> ChainEscrow:
    return ChainEscrow(deployed.settings())


def now(chain: Chain) -> int:
    return int(chain.w3.eth.get_block("latest")["timestamp"])


def travel(chain: Chain, seconds: int) -> None:
    chain.rpc("evm_increaseTime", seconds)
    chain.rpc("evm_mine")


# ---------------------------------------------------------------------------------- anchor


async def test_anchor_commit_then_reveal_a_forecast_style_commitment(
    deployed: Deployed, anchor: ChainAnchor
) -> None:
    preimage = b'{"salt":"ab12","slug":"nba"}'
    commitment = hashlib.sha256(preimage).hexdigest()  # as P2's forecast module hashes it
    ref = await anchor.commit(commitment, "forecast:nba")
    prefix, ledger_id, tx_hash = ref.split(":")
    assert (prefix, ledger_id) == ("ledger", "1")
    assert tx_hash.startswith("0x") and len(tx_hash) == 66
    entry = deployed.ledger.functions.getEntry(1).call()
    assert bytes(entry[3]).hex() == commitment and entry[2] is False

    await anchor.reveal(ref, preimage)
    assert deployed.ledger.functions.getEntry(1).call()[2] is True
    logs = deployed.ledger.events.Revealed().get_logs(from_block=0)
    assert bytes(logs[0]["args"]["preimage"]) == preimage

    with pytest.raises(ContractRevert) as revert:
        await anchor.reveal(ref, preimage)
    assert revert.value.error == "AlreadyRevealed"


async def test_anchor_refuses_a_wrong_preimage_and_ignores_stub_refs(
    anchor: ChainAnchor,
) -> None:
    ref = await anchor.commit("0x" + hashlib.sha256(b"right").hexdigest(), "trade:1")
    with pytest.raises(ContractRevert) as revert:
        await anchor.reveal(ref, b"wrong")
    assert revert.value.error == "PreimageMismatch"
    await anchor.reveal("stub-anchor:3", b"anything")  # not ours: a no-op, no error
    with pytest.raises(ValueError, match="32 bytes"):
        await anchor.commit("abcd", "short")


async def test_forecast_commit_and_reveal_reach_the_ledger(deployed: Deployed) -> None:
    settings = deployed.settings(use_p1_stubs=False)
    app = create_app(settings, Database(None))
    ctx: AppContext = app.state.ctx
    try:
        assert isinstance(ctx.anchor, ChainAnchor)
        await use_models(ctx, THREE_HIGH)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as http:
            opinion = (await http.post("/api/forecasts", json={"slug": NBA, "qty": 4})).json()
            assert opinion["anchor_ref"].startswith("ledger:1:")
            ledger = deployed.ledger.functions
            assert bytes(ledger.getEntry(1).call()[3]).hex() == opinion["commitment"]
            revealed = await http.post("/api/forecasts/f_1/reveal", json={"outcome": True})
            assert revealed.status_code == 200
        assert ledger.getEntry(1).call()[2] is True
        preimage = bytes(
            deployed.ledger.events.Revealed().get_logs(from_block=0)[0]["args"]["preimage"]
        )
        assert hashlib.sha256(preimage).hexdigest() == opinion["commitment"]
        assert json.loads(preimage)["slug"] == NBA
    finally:
        await ctx.aclose()


# ---------------------------------------------------------------------------------- escrow


async def test_deposit_then_claim_pays_the_recipient(
    deployed: Deployed, escrow: ChainEscrow
) -> None:
    chain = deployed.chain
    created = await escrow.create(Decimal("25.5"), expiry_s=3600)
    assert created.escrow_id == "1"
    assert created.claim_link.endswith(f"/claim#1:{created.secret}")
    assert chain.balance(deployed.escrow.address) == Decimal("25.5")
    assert chain.balance(chain.agent) == 1_000 - Decimal("25.5")
    # The secret itself never goes onchain, only its hash.
    assert deployed.escrow.functions.getDeposit(1).call()[4].hex() == created.claim_hash[2:]
    assert (await escrow.status("1")).settled is False

    result = await escrow.claim(created.escrow_id, created.secret, PAYEE)
    assert result.tx_hash.startswith("0x")
    assert chain.balance(PAYEE) == Decimal("25.5")
    assert chain.balance(deployed.escrow.address) == 0
    assert (await escrow.status("1")).settled is True

    with pytest.raises(ContractRevert) as again:
        await escrow.claim(created.escrow_id, created.secret, PAYEE)
    assert again.value.error == "AlreadySettled"
    assert isinstance(again.value, ValueError)


async def test_wrong_secret_is_refused_before_any_transaction(
    deployed: Deployed, escrow: ChainEscrow
) -> None:
    chain = deployed.chain
    created = await escrow.create(Decimal("10"), expiry_s=3600)
    blocks = chain.w3.eth.block_number
    with pytest.raises(ContractRevert) as revert:
        await escrow.claim(created.escrow_id, "0x" + "ab" * 32, PAYEE)
    assert revert.value.error == "WrongSecret"
    assert chain.w3.eth.block_number == blocks  # no gas spent on a guess
    assert chain.balance(PAYEE) == 0


async def test_copying_the_secret_from_the_mempool_cannot_redirect_the_funds(
    deployed: Deployed, escrow: ChainEscrow
) -> None:
    """The recipient has committed. Their claim sits in the mempool; an attacker reads the secret
    from it and, with a higher fee, tries to claim to themselves in the same block."""
    chain, w3 = deployed.chain, deployed.chain.w3
    contract = deployed.escrow
    created = await escrow.create(Decimal("40"), expiry_s=3600)
    secret = bytes.fromhex(created.secret[2:])
    agent = w3.eth.account.from_key(KEYS["agent"])
    attacker = w3.eth.account.from_key(ATTACKER_KEY)

    def send(account: Any, fn: Any, gas_price: int) -> Any:
        tx = fn.build_transaction(
            {
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address, "pending"),
                "gas": 300_000,
                "gasPrice": gas_price,
            }
        )
        return w3.eth.send_raw_transaction(account.sign_transaction(tx).raw_transaction)

    def mine_one(tx_hash: Any) -> None:
        w3.eth.wait_for_transaction_receipt(tx_hash)

    # An attacker who has only the secret cannot claim at all without a prior commitment.
    selector = Web3.keccak(text="ClaimNotCommitted()")[:4].hex()
    with pytest.raises(ContractCustomError, match=selector):
        contract.functions.claim(1, secret, attacker.address).call({"from": attacker.address})

    # Step 1, the recipient's commitment, is mined (automine is on).
    commitment = contract.functions.commitmentFor(1, secret, PAYEE).call()
    mine_one(send(agent, contract.functions.commitClaim(1, commitment), 10**9))

    chain.rpc("evm_setAutomine", False)
    try:
        victim = send(agent, contract.functions.claim(1, secret, PAYEE), 10**9)
        # The attacker now knows the secret and outbids the victim.
        evil_commit = contract.functions.commitmentFor(1, secret, attacker.address).call()
        a_commit = send(attacker, contract.functions.commitClaim(1, evil_commit), 5 * 10**9)
        a_claim = send(attacker, contract.functions.claim(1, secret, attacker.address), 4 * 10**9)
        chain.rpc("evm_mine")
    finally:
        chain.rpc("evm_setAutomine", True)

    assert w3.eth.get_transaction_receipt(victim)["status"] == 1
    assert w3.eth.get_transaction_receipt(a_commit)["status"] == 1  # a commit alone is harmless
    assert w3.eth.get_transaction_receipt(a_claim)["status"] == 0  # same block as its commit
    assert chain.balance(PAYEE) == 40
    assert chain.balance(attacker.address) == 0
    assert chain.balance(contract.address) == 0


async def test_refund_after_expiry_and_not_before(deployed: Deployed, escrow: ChainEscrow) -> None:
    chain = deployed.chain
    created = await escrow.create(Decimal("30"), expiry_s=3600)
    assert chain.balance(chain.agent) == 970
    with pytest.raises(ContractRevert) as early:
        await escrow.refund(created.escrow_id)
    assert early.value.error == "NotExpired"

    travel(chain, 3601)
    with pytest.raises(ContractRevert) as late:  # claiming is closed once it has expired
        await escrow.claim(created.escrow_id, created.secret, PAYEE)
    assert late.value.error == "Expired"

    await escrow.refund(created.escrow_id)
    assert chain.balance(chain.agent) == 1_000  # refund_to defaults to the agent wallet
    assert chain.balance(deployed.escrow.address) == 0
    with pytest.raises(ContractRevert) as again:
        await escrow.refund(created.escrow_id)
    assert again.value.error == "AlreadySettled"


async def test_refund_goes_to_the_named_address(deployed: Deployed, escrow: ChainEscrow) -> None:
    chain = deployed.chain
    created = await escrow.create(Decimal("5"), expiry_s=3600, refund_to=PAYEE)
    travel(chain, 3601)
    await escrow.refund(created.escrow_id)
    assert chain.balance(PAYEE) == 5


async def test_payment_request_and_pay(deployed: Deployed, escrow: ChainEscrow) -> None:
    chain = deployed.chain
    # The owner asks the agent wallet for 12.5; the agent wallet pays it.
    chain.owner_send(deployed.escrow.functions.request(chain.agent, 12_500_000, "lunch"))
    owner = chain.w3.eth.account.from_key(KEYS["owner"]).address
    before = chain.balance(owner)
    with pytest.raises(ValueError, match="per-transfer limit"):
        await escrow.pay("1", Decimal("10"))
    await escrow.pay("1", Decimal("100"))
    assert chain.balance(owner) - before == Decimal("12.5")
    with pytest.raises(ContractRevert) as again:
        await escrow.pay("1", Decimal("100"))
    assert again.value.error == "AlreadyPaid"

    made = await escrow.request(Decimal("3"), memo="coffee")
    assert made.request_id == "2"
    assert deployed.escrow.functions.getRequest(2).call()[2] == 3 * MUSD


# ---------------------------------------------------------------------------------- routes


@pytest.fixture
async def real_app(deployed: Deployed) -> AsyncIterator[FastAPI]:
    application = create_app(deployed.settings(use_p1_stubs=False), Database(None))
    yield application
    await application.state.ctx.aclose()


async def test_routes_create_locally_claim_publicly_and_events_follow(
    deployed: Deployed, real_app: FastAPI
) -> None:
    chain, ctx = deployed.chain, real_app.state.ctx
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app), base_url="http://localhost"
    ) as http:
        # Create and refund are for this machine only.
        refused = await http.post("/api/escrow/create", json={"amount_usd": 5}, headers=TUNNEL)
        assert refused.status_code == 403
        assert (await http.post("/api/escrow/1/refund", headers=TUNNEL)).status_code == 403
        assert (
            await http.post("/api/escrow/requests", json={"amount_usd": 1}, headers=TUNNEL)
        ).status_code == 403
        # Spending waits for the Stream's first delivery (the dead man's switch).
        beat = b'{"heartbeat": 1}'
        await http.post("/api/hooks/quicknode", content=beat, headers=sign(beat))
        over = await http.post("/api/escrow/create", json={"amount_usd": 150})
        assert over.status_code == 409 and "per-transfer limit" in over.json()["detail"]

        created = (await http.post("/api/escrow/create", json={"amount_usd": 7.25})).json()
        assert created["escrow_id"] == "1"
        assert (await http.get("/api/escrow/1")).json()["settled"] is False

        # The recipient claims through the tunnel with only the secret.
        body = {"escrow_id": "1", "secret": created["secret"], "to": PAYEE}
        bad = await http.post(
            "/api/escrow/claim", json={**body, "secret": "0x" + "cd" * 32}, headers=TUNNEL
        )
        assert bad.status_code == 409 and "WrongSecret" in bad.json()["detail"]
        paid = await http.post("/api/escrow/claim", json=body, headers=TUNNEL)
        assert paid.status_code == 200, paid.text
        assert chain.balance(PAYEE) == Decimal("7.25")

        # The Stream delivers the Escrow's logs.
        logs = [
            {
                "address": entry["address"],
                "topics": ["0x" + bytes(t).hex() for t in entry["topics"]],
                "data": "0x" + bytes(entry["data"]).hex(),
                "transactionHash": "0x" + bytes(entry["transactionHash"]).hex(),
                "logIndex": hex(entry["logIndex"] + 1000 * entry["blockNumber"]),
            }
            for entry in chain.w3.eth.get_logs({"address": deployed.escrow.address, "fromBlock": 0})
        ]
        payload = json.dumps({"logs": logs}).encode()
        response = await http.post("/api/hooks/quicknode", content=payload, headers=sign(payload))
        assert response.status_code == 200
        assert response.json()["status"] == "published 2"  # Deposited and Claimed, not the commit
    statuses = [e.data["status"] for e in ctx.bus.history() if e.type == "escrow.updated"]
    assert statuses == ["deposited", "claimed"]


async def test_peek_shows_the_amount_only_to_the_holder_of_the_secret(
    real_app: FastAPI,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app), base_url="http://localhost"
    ) as http:
        beat = b'{"heartbeat": 1}'
        await http.post("/api/hooks/quicknode", content=beat, headers=sign(beat))
        created = (await http.post("/api/escrow/create", json={"amount_usd": 4.5})).json()
        body = {"escrow_id": created["escrow_id"], "secret": created["secret"]}
        seen = await http.post("/api/escrow/peek", json=body, headers=TUNNEL)  # via the tunnel
        assert seen.status_code == 200
        assert seen.json()["amount_usd"] == 4.5 and seen.json()["settled"] is False
        assert set(seen.json()) == {"amount_usd", "expiry", "settled"}  # no refund address
        wrong = await http.post(
            "/api/escrow/peek", json={**body, "secret": "0x" + "cd" * 32}, headers=TUNNEL
        )
        assert wrong.status_code == 409 and "WrongSecret" in wrong.json()["detail"]
        nope = await http.post("/api/escrow/peek", json={**body, "escrow_id": "99"}, headers=TUNNEL)
        assert nope.status_code == 409 and "UnknownDeposit" in nope.json()["detail"]
        await http.post("/api/escrow/claim", json={**body, "to": PAYEE}, headers=TUNNEL)
        assert (await http.post("/api/escrow/peek", json=body)).json()["settled"] is True
