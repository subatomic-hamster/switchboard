"""`CctpTransfers`: Iris mocked with respx, the attestation watch on a fake clock, and the burn
call against a mock TokenMessenger on a local anvil node (skipped when anvil is not on PATH).
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import time
from collections.abc import Coroutine, Iterator
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest
import respx
from eth_typing import HexStr
from web3 import Web3

from app.chain import transfer
from app.chain.transfer import (
    FORWARD_HOOK,
    CctpTransfers,
    Iris,
    TransferError,
    b58decode,
    b58encode,
    mint_target,
    solana_ata,
)
from app.events import EventBus
from app.schemas import Chain, ChainTransferRequest
from tests.conftest import make_settings
from tests.test_chain_vault import KEYS, MUSD, _artifact

IRIS = "https://iris.test"
ARC_ADDRESS = "0x554f14F1Ee51E747fAe8B2095b917b703dDD4198"
SEPOLIA_PAYEE = "0x5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a"
BURN_TX = "0x" + "ab" * 32
MINT_TX = "0x" + "cd" * 32
FEES = [
    {"finalityThreshold": 1000, "minimumFee": 0, "forwardFee": {"low": 1, "med": 60000, "high": 9}},
    {"finalityThreshold": 2000, "minimumFee": 0, "forwardFee": {"low": 1, "med": 60000, "high": 9}},
]

# Reference values from @solana/spl-token getAssociatedTokenAddressSync(devnet USDC, owner, true).
ATA_VECTORS = {
    "CCTPV2vPZJS2u2BBsUoscuikbYjnpFmbFsvVuJdgUMQe": "H71qMXP7PXSH48PSdbsXTAuSfiqnQwSHcVSUfzjPnqXV",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "542nduv7U4GjL4Yei5WF5TVkDZFuaoCzmsvayYXB3sWj",
    "4Nd1mBQtrMJVYVfKf2PJy9NZUZdTAsp7D4xWLs4gDB4T": "624mo8iacoaXLdedsvtqcNN2QCYsiWt7SXzELE6ryQSj",
}


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def publish(self, type_: str, data: Any) -> None:
        self.events.append((type_, data))

    def types(self) -> list[str]:
        return [t for t, _ in self.events]


class FakeClock:
    """`sleep` advances `now`, so a watch with a 1800 second limit runs instantly."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps = 0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.sleeps += 1


def spawn(coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
    return asyncio.get_running_loop().create_task(coro, name=name)


def make_transfers(bus: FakeBus, clock: FakeClock | None = None, **settings: Any) -> CctpTransfers:
    clock = clock or FakeClock()
    return CctpTransfers(
        spawn,
        cast(EventBus, bus),
        make_settings(iris_url=IRIS, **settings),
        clock=clock.clock,
        sleep=clock.sleep,
    )


def request(
    to_chain: Chain, to: str, amount: str = "5", from_chain: Chain = Chain.ARC_TESTNET
) -> ChainTransferRequest:
    return ChainTransferRequest(
        from_chain=from_chain, to_chain=to_chain, to=to, amount_usd=Decimal(amount)
    )


def message(status: str = "complete", state: str | None = None, **extra: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"message": "0x01", "attestation": "0x02", "status": status}
    if state:
        entry["forwardState"] = state
    return {"messages": [{**entry, **extra}]}


def iris_sequence(*replies: httpx.Response | Exception) -> respx.Route:
    return respx.get(f"{IRIS}/v2/messages/26").mock(side_effect=list(replies))


def ok(body: Any) -> httpx.Response:
    return httpx.Response(200, json=body)


NOT_FOUND = httpx.Response(404, json={"error": "Message not found for provided parameters"})


async def watch(
    transfers: CctpTransfers, to_chain: Chain = Chain.ETH_SEPOLIA, to: str = SEPOLIA_PAYEE
) -> None:
    t = transfer._Transfer("t_1", request(to_chain, to), BURN_TX)
    await transfers._follow(t)


# --------------------------------------------------------------------------- addresses


def test_base58_round_trips() -> None:
    for raw in (bytes(32), b"\0\0\1\2\3", bytes(range(32))):
        assert b58decode(b58encode(raw)) == raw
    assert b58encode(bytes(32)) == "1" * 32


def test_solana_ata_matches_the_reference_implementation() -> None:
    for owner, expected in ATA_VECTORS.items():
        assert b58encode(solana_ata(b58decode(owner))) == expected


def test_mint_target_for_each_chain() -> None:
    recipient, hook = mint_target(Chain.ETH_SEPOLIA, ARC_ADDRESS)
    assert recipient == bytes(12) + bytes.fromhex(ARC_ADDRESS[2:])
    assert hook == FORWARD_HOOK
    assert FORWARD_HOOK.hex() == "636374702d666f72776172640000000000000000000000000000000000000000"

    owner = "4Nd1mBQtrMJVYVfKf2PJy9NZUZdTAsp7D4xWLs4gDB4T"
    recipient, hook = mint_target(Chain.SOLANA_DEVNET, owner)
    assert b58encode(recipient) == ATA_VECTORS[owner]
    # magic, version 0, length 33, flag 1, owner
    assert hook[:24].rstrip(b"\0") == b"cctp-forward"
    assert hook[24:32] == (0).to_bytes(4, "big") + (33).to_bytes(4, "big")
    assert hook[32:] == b"\x01" + b58decode(owner)


def test_bad_destination_addresses_are_refused_before_anything_is_spent() -> None:
    with pytest.raises(TransferError, match="Solana"):
        mint_target(Chain.SOLANA_DEVNET, "Sam11111111111111111111111111111111111111111")
    with pytest.raises(TransferError, match="Solana"):
        mint_target(Chain.SOLANA_DEVNET, ARC_ADDRESS)
    with pytest.raises(TransferError, match="Ethereum"):
        mint_target(Chain.ETH_SEPOLIA, "4Nd1mBQtrMJVYVfKf2PJy9NZUZdTAsp7D4xWLs4gDB4T")


# --------------------------------------------------------------------------- Iris


@respx.mock
async def test_fee_quote_reads_the_standard_entry() -> None:
    forward = respx.get(f"{IRIS}/v2/burn/USDC/fees/26/0", params={"forward": "true"}).mock(
        return_value=ok(FEES)
    )
    assert await Iris(IRIS).fee(0, forward=True, solana_setup=False) == (Decimal(0), 60000)
    assert forward.called
    plain = respx.get(f"{IRIS}/v2/burn/USDC/fees/26/0").mock(return_value=ok(FEES))
    assert await Iris(IRIS).fee(0, forward=False, solana_setup=False) == (Decimal(0), 0)
    assert plain.called


@respx.mock
async def test_fee_quote_for_solana_asks_for_the_token_account() -> None:
    route = respx.get(
        f"{IRIS}/v2/burn/USDC/fees/26/5",
        params={"forward": "true", "includeRecipientSetup": "true"},
    ).mock(return_value=ok(FEES))
    await Iris(IRIS).fee(5, forward=True, solana_setup=True)
    assert route.called


@respx.mock
async def test_a_failed_fee_quote_stops_the_transfer() -> None:
    respx.get(f"{IRIS}/v2/burn/USDC/fees/26/0").mock(return_value=httpx.Response(500))
    with pytest.raises(TransferError, match="fee quote"):
        await Iris(IRIS).fee(0, forward=False, solana_setup=False)


# --------------------------------------------------------------------------- start (no chain)


async def test_arc_to_arc_publishes_both_events_and_touches_no_chain() -> None:
    bus = FakeBus()
    transfers = make_transfers(bus)  # no RPC URL: any chain call would raise
    transfer_id = await transfers.start(
        request(Chain.ARC_TESTNET, ARC_ADDRESS), plan_id="p_1", payee_label="Sam"
    )
    assert bus.types() == ["transfer.sent", "transfer.arrived"]
    assert bus.events[1][1].transfer_id == transfer_id
    assert bus.events[0][1].tx_hash is None


async def test_a_transfer_from_another_chain_is_refused() -> None:
    bus = FakeBus()
    with pytest.raises(TransferError, match="start from Arc"):
        await make_transfers(bus).start(
            request(Chain.ARC_TESTNET, ARC_ADDRESS, from_chain=Chain.ETH_SEPOLIA)
        )
    assert bus.events == []


# --------------------------------------------------------------------------- the watch


@respx.mock
async def test_forwarded_transfer_goes_pending_attested_arrived() -> None:
    bus, clock = FakeBus(), FakeClock()
    route = iris_sequence(
        NOT_FOUND,
        ok(message("pending_confirmations")),
        ok(message(state="PENDING", forwardTxHash=BURN_TX)),  # a hash before COMPLETE is not a mint
        ok(message(state="PENDING")),
        ok(message(state="COMPLETE", forwardTxHash=MINT_TX, destinationMintTxHash=MINT_TX)),
    )
    await watch(make_transfers(bus, clock, cctp_poll_interval_s=2))
    assert route.call_count == 5
    assert route.calls[0].request.url.params["transactionHash"] == BURN_TX
    assert bus.types() == ["transfer.attested", "transfer.arrived"]  # attested once, not per poll
    attested, arrived = bus.events[0][1], bus.events[1][1]
    assert attested.forwarding is True
    assert "waiting to mint on eth-sepolia" in attested.detail
    assert arrived.tx_hash == MINT_TX
    assert clock.now == 8  # four sleeps of two seconds


@respx.mock
async def test_the_forward_hash_alone_is_enough_once_complete() -> None:
    bus = FakeBus()
    iris_sequence(ok(message(state="COMPLETE", forwardTxHash=MINT_TX)))
    await watch(make_transfers(bus))
    assert bus.events[-1][1].tx_hash == MINT_TX


@respx.mock
async def test_a_failed_forward_is_reported_and_stops() -> None:
    bus = FakeBus()
    route = iris_sequence(ok(message(state="PENDING")), ok(message(state="FAILED")))
    await watch(make_transfers(bus))
    assert route.call_count == 2
    assert bus.types() == ["transfer.attested", "transfer.attested"]
    assert "forwarding failed" in bus.events[1][1].detail


@respx.mock
async def test_the_watch_gives_up_at_the_time_limit() -> None:
    bus, clock = FakeBus(), FakeClock()
    route = respx.get(f"{IRIS}/v2/messages/26").mock(return_value=NOT_FOUND)
    await watch(make_transfers(bus, clock, cctp_poll_interval_s=10, cctp_timeout_s=100))
    assert route.call_count == 10
    assert clock.now == 100
    assert bus.types() == ["transfer.attested"]
    assert "time limit" in bus.events[0][1].detail


@respx.mock
async def test_a_transient_api_error_does_not_end_the_watch() -> None:
    bus = FakeBus()
    iris_sequence(
        httpx.Response(500),
        httpx.ConnectError("boom"),
        ok(message(state="COMPLETE", destinationMintTxHash=MINT_TX, forwardTxHash=MINT_TX)),
    )
    await watch(make_transfers(bus))
    assert bus.types() == ["transfer.attested", "transfer.arrived"]


@respx.mock
async def test_without_forwarding_or_a_sepolia_key_it_stops_at_attested() -> None:
    bus = FakeBus()
    route = iris_sequence(ok(message()))
    await watch(make_transfers(bus, cctp_forward=False), Chain.SOLANA_DEVNET, "x")
    assert route.call_count == 1
    assert bus.types() == ["transfer.attested", "transfer.attested"]
    assert bus.events[0][1].forwarding is False
    assert "nothing here can mint" in bus.events[1][1].detail


@respx.mock
async def test_without_forwarding_the_server_mints_on_sepolia(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = FakeBus()
    iris_sequence(ok(message()))
    seen: list[tuple[str, str]] = []

    def receive(self: CctpTransfers, msg: str, attestation: str) -> str:
        seen.append((msg, attestation))
        return MINT_TX

    monkeypatch.setattr(CctpTransfers, "_receive", receive)
    await watch(make_transfers(bus, cctp_forward=False, eth_sepolia_rpc_url="http://sepolia.test"))
    assert seen == [("0x01", "0x02")]
    assert bus.types() == ["transfer.attested", "transfer.arrived"]
    assert bus.events[1][1].tx_hash == MINT_TX


@respx.mock
async def test_a_failed_self_mint_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = FakeBus()
    iris_sequence(ok(message()))

    def receive(self: CctpTransfers, msg: str, attestation: str) -> str:
        raise TransferError("no gas")

    monkeypatch.setattr(CctpTransfers, "_receive", receive)
    await watch(make_transfers(bus, cctp_forward=False, eth_sepolia_rpc_url="http://sepolia.test"))
    assert bus.types() == ["transfer.attested", "transfer.attested"]
    assert "no gas" in bus.events[1][1].detail


# --------------------------------------------------------------------------- the burn on anvil


class Anvil:
    def __init__(self, url: str, w3: Web3, usdc: Any, messenger: Any) -> None:
        self.url, self.w3, self.usdc, self.messenger = url, w3, usdc, messenger
        self.agent = w3.eth.account.from_key(KEYS["agent"]).address

    def balance(self, address: str) -> Decimal:
        return Decimal(self.usdc.functions.balanceOf(address).call()) / MUSD


@pytest.fixture(scope="module")
def anvil() -> Iterator[Anvil]:
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

        def deploy(art: dict[str, Any]) -> Any:
            factory = w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"]["object"])
            tx = factory.constructor().build_transaction(
                {"from": owner.address, "nonce": w3.eth.get_transaction_count(owner.address)}
            )
            receipt = w3.eth.wait_for_transaction_receipt(
                w3.eth.send_raw_transaction(owner.sign_transaction(tx).raw_transaction)
            )
            return w3.eth.contract(address=receipt["contractAddress"], abi=art["abi"])

        usdc = deploy(_artifact("Vault.t.sol/MockUSDC.json"))
        messenger = deploy(_artifact("MockTokenMessenger.sol/MockTokenMessenger.json"))
        result = Anvil(url, w3, usdc, messenger)
        tx = usdc.functions.mint(result.agent, 20 * MUSD).build_transaction(
            {"from": owner.address, "nonce": w3.eth.get_transaction_count(owner.address)}
        )
        w3.eth.wait_for_transaction_receipt(
            w3.eth.send_raw_transaction(owner.sign_transaction(tx).raw_transaction)
        )
        yield result
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture(autouse=True)
def _revert(request: pytest.FixtureRequest) -> Iterator[None]:
    """Each anvil test starts from the same chain state."""
    if "anvil" not in request.fixturenames:
        yield
        return
    chain = request.getfixturevalue("anvil")
    snapshot = chain.w3.provider.make_request("evm_snapshot", [])["result"]
    yield
    chain.w3.provider.make_request("evm_revert", [snapshot])


def anvil_transfers(
    chain: Anvil, bus: FakeBus, monkeypatch: pytest.MonkeyPatch, **extra: Any
) -> CctpTransfers:
    monkeypatch.setattr(transfer, "TOKEN_MESSENGER", chain.messenger.address)
    return make_transfers(
        bus,
        arc_rpc_url=chain.url,
        arc_chain_id=31337,
        agent_private_key=KEYS["agent"],
        usdc_address=chain.usdc.address,
        **extra,
    )


def burn_event(chain: Anvil, tx_hash: str) -> Any:
    receipt = chain.w3.eth.get_transaction_receipt(HexStr(tx_hash))
    (entry,) = [e for e in receipt["logs"] if e["address"] == chain.messenger.address]
    return chain.messenger.events.Burn().process_log(entry)["args"]


@pytest.mark.anvil
@pytest.mark.skipif(shutil.which("anvil") is None, reason="anvil is not on PATH")
@respx.mock
async def test_burn_to_ethereum_carries_the_forwarding_hook(
    anvil: Anvil, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.get(f"{IRIS}/v2/burn/USDC/fees/26/0", params={"forward": "true"}).mock(
        return_value=ok(FEES)
    )
    iris_sequence(ok(message(state="COMPLETE", destinationMintTxHash=MINT_TX)))
    bus = FakeBus()
    transfers = anvil_transfers(anvil, bus, monkeypatch)
    transfer_id = await transfers.start(
        request(Chain.ETH_SEPOLIA, SEPOLIA_PAYEE, "5"), plan_id="p_9"
    )

    sent = bus.events[0][1]
    assert sent.transfer_id == transfer_id
    assert sent.tx_hash.startswith("0x") and len(sent.tx_hash) == 66
    args = burn_event(anvil, sent.tx_hash)
    # The recipient is owed 5; the 0.06 forwarding fee is burned on top, from the agent's float.
    assert args["amount"] == 5_060_000
    assert args["maxFee"] == 60_000
    assert args["destinationDomain"] == 0
    assert args["mintRecipient"] == bytes(12) + bytes.fromhex(SEPOLIA_PAYEE[2:])
    assert args["burnToken"] == anvil.usdc.address
    assert args["destinationCaller"] == bytes(32)
    assert args["minFinalityThreshold"] == 2000
    assert args["hookData"] == FORWARD_HOOK
    assert anvil.balance(anvil.agent) == Decimal("14.94")
    assert anvil.usdc.functions.allowance(anvil.agent, anvil.messenger.address).call() == 0

    # Let the background watch finish: the mint hash is the one Circle reported.
    await asyncio.gather(*[t for t in asyncio.all_tasks() if t.get_name() == f"cctp-{transfer_id}"])
    assert bus.types() == ["transfer.sent", "transfer.attested", "transfer.arrived"]
    assert bus.events[2][1].tx_hash == MINT_TX


@pytest.mark.anvil
@pytest.mark.skipif(shutil.which("anvil") is None, reason="anvil is not on PATH")
@respx.mock
async def test_burn_to_solana_mints_to_the_token_account(
    anvil: Anvil, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = "4Nd1mBQtrMJVYVfKf2PJy9NZUZdTAsp7D4xWLs4gDB4T"
    respx.get(
        f"{IRIS}/v2/burn/USDC/fees/26/5",
        params={"forward": "true", "includeRecipientSetup": "true"},
    ).mock(return_value=ok(FEES))
    iris_sequence(NOT_FOUND)
    bus = FakeBus()
    transfers = anvil_transfers(anvil, bus, monkeypatch, cctp_timeout_s=1)
    transfer_id = await transfers.start(request(Chain.SOLANA_DEVNET, owner, "1"))
    args = burn_event(anvil, bus.events[0][1].tx_hash)
    assert args["destinationDomain"] == 5
    assert b58encode(bytes(args["mintRecipient"])) == ATA_VECTORS[owner]
    assert bytes(args["hookData"])[32:] == b"\x01" + b58decode(owner)
    await asyncio.gather(*[t for t in asyncio.all_tasks() if t.get_name() == f"cctp-{transfer_id}"])


@pytest.mark.anvil
@pytest.mark.skipif(shutil.which("anvil") is None, reason="anvil is not on PATH")
@respx.mock
async def test_without_forwarding_it_uses_the_plain_burn(
    anvil: Anvil, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.get(f"{IRIS}/v2/burn/USDC/fees/26/0").mock(return_value=ok(FEES))
    iris_sequence(NOT_FOUND)
    bus = FakeBus()
    transfers = anvil_transfers(anvil, bus, monkeypatch, cctp_forward=False, cctp_timeout_s=1)
    transfer_id = await transfers.start(request(Chain.ETH_SEPOLIA, SEPOLIA_PAYEE, "2"))
    args = burn_event(anvil, bus.events[0][1].tx_hash)
    assert (args["amount"], args["maxFee"], args["hookData"]) == (2_000_000, 0, b"")
    await asyncio.gather(*[t for t in asyncio.all_tasks() if t.get_name() == f"cctp-{transfer_id}"])


@pytest.mark.anvil
@pytest.mark.skipif(shutil.which("anvil") is None, reason="anvil is not on PATH")
@respx.mock
async def test_a_burn_the_wallet_cannot_cover_is_refused_before_approving(
    anvil: Anvil, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.get(f"{IRIS}/v2/burn/USDC/fees/26/0", params={"forward": "true"}).mock(
        return_value=ok(FEES)
    )
    bus = FakeBus()
    transfers = anvil_transfers(anvil, bus, monkeypatch)
    nonce_before = anvil.w3.eth.get_transaction_count(anvil.agent)
    with pytest.raises(TransferError, match="holds 20 USDC"):
        await transfers.start(request(Chain.ETH_SEPOLIA, SEPOLIA_PAYEE, "20"))
    assert bus.events == []
    assert anvil.w3.eth.get_transaction_count(anvil.agent) == nonce_before
