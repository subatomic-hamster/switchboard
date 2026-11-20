"""The real cross-chain transfer: `TransferPort` over Circle CCTP V2, burning on Arc testnet.

Money flow (see RAILS.md). The vault pays the agent wallet on Arc, never the payee, whenever the
payee is on another chain (`ChainVault.request_spend(chain=...)`). `start` then burns that USDC
from the agent wallet on Arc:

    vault -> agent (Arc)  ->  depositForBurn on Arc  ->  Circle attests  ->  mint on the destination

By default the burn carries Circle's Forwarding Service hook, so Circle submits the destination
mint and the agent needs no gas on Sepolia or Solana. Arc-to-Arc needs nothing from here: the
vault already paid the payee, so `start` only publishes the two events.

Published events: `transfer.sent` (with the real burn hash), `transfer.attested` (Circle signed
the burn, mint not seen yet), `transfer.arrived` (with the real mint hash).

Importing this module needs no environment and no network. web3 clients are built on first use.
web3.py is synchronous, so every chain call runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from decimal import ROUND_CEILING, Decimal
from typing import Any

import httpx
from web3 import Web3
from web3.exceptions import ContractLogicError, Web3RPCError

from app.chain.stub import Spawner
from app.chain.vault import from_units, is_evm, to_units
from app.config import Settings
from app.events import EventBus
from app.schemas import (
    Chain,
    ChainTransferRequest,
    TransferArrivedData,
    TransferAttestedData,
    TransferSentData,
)

log = logging.getLogger("switchboard.transfer")

# Circle's domain ids. Source: developers.circle.com/cctp/concepts/supported-chains-and-domains
DOMAIN: dict[Chain, int] = {Chain.ETH_SEPOLIA: 0, Chain.SOLANA_DEVNET: 5, Chain.ARC_TESTNET: 26}

# CCTP V2 testnet contracts, identical on Arc Testnet and Ethereum Sepolia.
# Source: developers.circle.com/cctp/references/contract-addresses
TOKEN_MESSENGER = "0x8FE6B999Dc680CcFDD5Bf7EB0974218be2542DAA"  # noqa: S105 - a public address
MESSAGE_TRANSMITTER = "0xE737e5cEBEEBa77EFE34D4aa090756590b1CE275"
SOLANA_USDC_MINT = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"

# Arc finalises in under a second and offers no Fast Transfer as a source: standard attestation.
MIN_FINALITY_STANDARD = 2000
# "cctp-forward", version 0, no extra data (developers.circle.com/cctp/concepts/forwarding-service).
FORWARD_MAGIC = b"cctp-forward".ljust(24, b"\0")
FORWARD_HOOK = FORWARD_MAGIC + (0).to_bytes(4, "big") + (0).to_bytes(4, "big")

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"  # noqa: S105 - a public address
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"

_ERC20_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "approve",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "type": "function",
        "name": "balanceOf",
        "stateMutability": "view",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]
_BURN_INPUTS = [
    {"name": "amount", "type": "uint256"},
    {"name": "destinationDomain", "type": "uint32"},
    {"name": "mintRecipient", "type": "bytes32"},
    {"name": "burnToken", "type": "address"},
    {"name": "destinationCaller", "type": "bytes32"},
    {"name": "maxFee", "type": "uint256"},
    {"name": "minFinalityThreshold", "type": "uint32"},
]
_TOKEN_MESSENGER_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "depositForBurn",
        "stateMutability": "nonpayable",
        "inputs": _BURN_INPUTS,
        "outputs": [],
    },
    {
        "type": "function",
        "name": "depositForBurnWithHook",
        "stateMutability": "nonpayable",
        "inputs": [*_BURN_INPUTS, {"name": "hookData", "type": "bytes"}],
        "outputs": [],
    },
]
_TRANSMITTER_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "receiveMessage",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "message", "type": "bytes"}, {"name": "attestation", "type": "bytes"}],
        "outputs": [{"name": "success", "type": "bool"}],
    }
]


class TransferError(ValueError):
    """The transfer cannot be started. The message is safe to show."""


# --------------------------------------------------------------------------- Solana addresses

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_P = 2**255 - 19
_D = -121665 * pow(121666, -1, _P) % _P


def b58decode(text: str) -> bytes:
    number = 0
    for char in text:
        index = _B58.find(char)
        if index < 0:
            raise ValueError(f"not base58: {char!r}")
        number = number * 58 + index
    body = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return b"\0" * (len(text) - len(text.lstrip("1"))) + body


def b58encode(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    out = ""
    while number:
        number, rem = divmod(number, 58)
        out = _B58[rem] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def _on_curve(point: bytes) -> bool:
    """True when the 32 bytes decompress to an ed25519 point (so they are not a valid PDA)."""
    y = int.from_bytes(point, "little") & (2**255 - 1)
    if y >= _P:
        return False
    u, v = (y * y - 1) % _P, (_D * y * y + 1) % _P
    x2 = u * pow(v, -1, _P) % _P
    return x2 == 0 or pow(x2, (_P - 1) // 2, _P) == 1


def solana_pubkey(address: str) -> bytes:
    try:
        raw = b58decode(address)
    except ValueError as exc:
        raise TransferError(f"{address!r} is not a Solana address") from exc
    if len(raw) != 32:
        raise TransferError(f"{address!r} is not a Solana address (it does not decode to 32 bytes)")
    return raw


def solana_ata(owner: bytes, mint: str = SOLANA_USDC_MINT) -> bytes:
    """The owner's associated USDC token account: the program address CCTP must mint to."""
    seeds = owner + b58decode(TOKEN_PROGRAM) + b58decode(mint)
    program = b58decode(ATA_PROGRAM)
    for bump in range(255, -1, -1):
        candidate = hashlib.sha256(seeds + bytes([bump]) + program + b"ProgramDerivedAddress")
        if not _on_curve(candidate.digest()):
            return candidate.digest()
    raise TransferError("no program address found")  # pragma: no cover - ~2^-256


def mint_target(chain: Chain, address: str) -> tuple[bytes, bytes]:
    """(mintRecipient bytes32, hook data) for a destination. Raises TransferError for a bad
    address, before anything is spent."""
    if chain is Chain.SOLANA_DEVNET:
        owner = solana_pubkey(address)
        # Ask the Forwarding Service to create the USDC token account if it is missing
        # (flag 1 + owner, 33 bytes of Circle hook data). The fee quote must ask for it too.
        hook = FORWARD_MAGIC + (0).to_bytes(4, "big") + (33).to_bytes(4, "big") + b"\x01" + owner
        return solana_ata(owner), hook
    if not is_evm(address):
        raise TransferError(f"{address!r} is not an Ethereum address")
    return bytes(12) + bytes.fromhex(address[2:]), FORWARD_HOOK


# --------------------------------------------------------------------------- Iris


class Iris:
    """Circle's attestation API (sandbox by default). Read-only GETs, no key needed."""

    def __init__(self, base_url: str, http: httpx.AsyncClient | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._http = http

    async def _get(self, path: str) -> httpx.Response:
        if self._http is not None:
            return await self._http.get(f"{self._base}{path}", timeout=20)
        async with httpx.AsyncClient() as client:
            return await client.get(f"{self._base}{path}", timeout=20)

    async def fee(
        self, dest_domain: int, *, forward: bool, solana_setup: bool
    ) -> tuple[Decimal, int]:
        """(CCTP protocol fee in basis points, forwarding fee in USDC units) for a standard
        transfer out of Arc. The forwarding fee is the `med` tier, as Circle recommends."""
        query = "?forward=true" if forward else ""
        if forward and solana_setup:
            query += "&includeRecipientSetup=true"
        response = await self._get(
            f"/v2/burn/USDC/fees/{DOMAIN[Chain.ARC_TESTNET]}/{dest_domain}{query}"
        )
        if response.status_code != 200:
            raise TransferError(f"Circle's fee quote failed ({response.status_code})")
        for entry in response.json():
            if entry["finalityThreshold"] == MIN_FINALITY_STANDARD:
                forward_fee = int(entry["forwardFee"]["med"]) if forward else 0
                return Decimal(str(entry["minimumFee"])), forward_fee
        raise TransferError("Circle's fee quote has no standard-transfer entry")

    async def message(self, burn_tx: str) -> dict[str, Any] | None:
        """The attested message for a burn on Arc, or None while Circle has none yet."""
        response = await self._get(
            f"/v2/messages/{DOMAIN[Chain.ARC_TESTNET]}?transactionHash={burn_tx}"
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        messages = response.json().get("messages") or []
        return dict(messages[0]) if messages else None


# --------------------------------------------------------------------------- the transfer


class _Transfer:
    def __init__(self, transfer_id: str, request: ChainTransferRequest, burn_tx: str) -> None:
        self.id = transfer_id
        self.request = request
        self.burn_tx = burn_tx
        self.attested = False


Sleep = Callable[[float], Awaitable[None]]


class CctpTransfers:
    """`TransferPort` on CCTP V2. Needs the Arc RPC URL and the agent key."""

    def __init__(
        self,
        spawn: Spawner,
        bus: EventBus,
        settings: Settings,
        first_id: int = 1,
        *,
        http: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._spawn = spawn
        self._bus = bus
        self._settings = settings
        self._ids = itertools.count(first_id)
        self._iris = Iris(settings.iris_url, http)
        self._sleep = sleep
        self._clock = clock
        self._tx_lock = threading.Lock()  # one transaction at a time per key, so nonces never clash
        self._arc: Any = None
        self._sepolia: Any = None

    # ------------------------------------------------------------------ plumbing

    def _web3(self, chain: Chain) -> Any:
        s = self._settings
        if chain is Chain.ARC_TESTNET:
            if self._arc is None:
                self._arc = Web3(Web3.HTTPProvider(s.arc_rpc_url, request_kwargs={"timeout": 30}))
            return self._arc
        if not s.eth_sepolia_rpc_url:
            raise TransferError("ETH_SEPOLIA_RPC_URL is not set")
        if self._sepolia is None:
            self._sepolia = Web3(
                Web3.HTTPProvider(s.eth_sepolia_rpc_url, request_kwargs={"timeout": 30})
            )
        return self._sepolia

    def _send(self, w3: Any, chain_id: int, fn: Any) -> str:
        """Sign and send one contract call as the agent; return the transaction hash."""
        account = w3.eth.account.from_key(self._settings.agent_private_key)
        with self._tx_lock:
            try:
                tx = fn.build_transaction(  # eth_estimateGas runs here: a revert shows up here
                    {
                        "from": account.address,
                        "nonce": w3.eth.get_transaction_count(account.address, "pending"),
                        "chainId": chain_id,
                    }
                )
            except (ContractLogicError, Web3RPCError) as exc:
                raise TransferError(f"the chain refused the call: {exc}") from exc
            tx_hash = w3.eth.send_raw_transaction(account.sign_transaction(tx).raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt["status"] != 1:
            raise TransferError(f"transaction 0x{bytes(tx_hash).hex()} failed")
        return "0x" + bytes(tx_hash).hex()

    def _burn(
        self, total_units: int, dest_domain: int, recipient: bytes, max_fee: int, hook: bytes | None
    ) -> str:
        """Approve the TokenMessenger and burn. Returns the burn transaction hash."""
        s = self._settings
        w3 = self._web3(Chain.ARC_TESTNET)
        agent = w3.eth.account.from_key(s.agent_private_key).address
        usdc = w3.eth.contract(address=Web3.to_checksum_address(s.usdc_address), abi=_ERC20_ABI)
        held = usdc.functions.balanceOf(agent).call()
        if held < total_units:
            raise TransferError(
                f"the agent wallet holds {from_units(held)} USDC on Arc but the burn needs "
                f"{from_units(total_units)}"
            )
        messenger_address = Web3.to_checksum_address(TOKEN_MESSENGER)
        self._send(w3, s.arc_chain_id, usdc.functions.approve(messenger_address, total_units))
        messenger = w3.eth.contract(address=messenger_address, abi=_TOKEN_MESSENGER_ABI)
        args = (
            total_units,
            dest_domain,
            recipient,
            Web3.to_checksum_address(s.usdc_address),
            bytes(32),  # destinationCaller: anyone may submit the mint
            max_fee,
            MIN_FINALITY_STANDARD,
        )
        fn = (
            messenger.functions.depositForBurn(*args)
            if hook is None
            else messenger.functions.depositForBurnWithHook(*args, hook)
        )
        return self._send(w3, s.arc_chain_id, fn)

    def _receive(self, message: str, attestation: str) -> str:
        """Mint on Ethereum Sepolia ourselves (CCTP_FORWARD=false). Needs Sepolia ETH."""
        w3 = self._web3(Chain.ETH_SEPOLIA)
        transmitter = w3.eth.contract(
            address=Web3.to_checksum_address(MESSAGE_TRANSMITTER), abi=_TRANSMITTER_ABI
        )
        fn = transmitter.functions.receiveMessage(
            bytes.fromhex(message[2:]), bytes.fromhex(attestation[2:])
        )
        return self._send(w3, self._settings.eth_sepolia_chain_id, fn)

    # ------------------------------------------------------------------ TransferPort

    async def start(
        self,
        request: ChainTransferRequest,
        *,
        plan_id: str | None = None,
        payee_label: str = "",
        route_id: str = "",
        eta_s: int = 0,
    ) -> str:
        if request.from_chain is not Chain.ARC_TESTNET:
            # The vault, and so the money, is on Arc. Rails with another source are not live.
            raise TransferError("real transfers start from Arc: the vault and the agent are there")
        transfer_id = f"t_{next(self._ids)}"
        burn_tx = ""
        if request.to_chain is not Chain.ARC_TESTNET:
            burn_tx = await self._burn_for(request)

        await self._bus.publish(
            "transfer.sent",
            TransferSentData(
                transfer_id=transfer_id,
                plan_id=plan_id,
                from_chain=request.from_chain,
                to_chain=request.to_chain,
                amount_usd=request.amount_usd,
                payee_label=payee_label,
                route_id=route_id,
                eta_s=eta_s,
                tx_hash=burn_tx or None,
            ),
        )
        if request.to_chain is Chain.ARC_TESTNET:
            # The vault already paid the payee on Arc (redeem, or the approve transaction).
            await self._arrived(transfer_id, request, "")
        else:
            transfer = _Transfer(transfer_id, request, burn_tx)
            self._spawn(self._follow(transfer), f"cctp-{transfer_id}")
        return transfer_id

    async def _burn_for(self, request: ChainTransferRequest) -> str:
        s = self._settings
        forward = s.cctp_forward
        recipient, hook = mint_target(request.to_chain, request.to)
        if not forward and request.to_chain is not Chain.ETH_SEPOLIA:
            log.warning(
                "no server-side mint for %s: the transfer will stop at attested", request.to_chain
            )
        bps, forward_fee = await self._iris.fee(
            DOMAIN[request.to_chain],
            forward=forward,
            solana_setup=request.to_chain is Chain.SOLANA_DEVNET,
        )
        amount = to_units(request.amount_usd)
        protocol_fee = int(
            (Decimal(amount) * bps / Decimal(10_000)).to_integral_value(ROUND_CEILING)
        )
        max_fee = forward_fee + protocol_fee
        # The recipient gets `amount`: the fees are burned on top, out of the agent's own float.
        burn = await asyncio.to_thread(
            self._burn,
            amount + max_fee,
            DOMAIN[request.to_chain],
            recipient,
            max_fee,
            hook if forward else None,
        )
        log.info(
            "burned %s USDC on Arc for %s: %s", from_units(amount + max_fee), request.to_chain, burn
        )
        return burn

    # ------------------------------------------------------------------ the attestation watch

    async def _arrived(self, transfer_id: str, request: ChainTransferRequest, tx_hash: str) -> None:
        await self._bus.publish(
            "transfer.arrived",
            TransferArrivedData(
                transfer_id=transfer_id,
                to_chain=request.to_chain,
                amount_usd=request.amount_usd,
                tx_hash=tx_hash,
            ),
        )

    async def _attested(self, t: _Transfer, detail: str) -> None:
        await self._bus.publish(
            "transfer.attested",
            TransferAttestedData(
                transfer_id=t.id,
                to_chain=t.request.to_chain,
                amount_usd=t.request.amount_usd,
                tx_hash=t.burn_tx,
                forwarding=self._settings.cctp_forward,
                detail=detail,
            ),
        )

    async def _follow(self, t: _Transfer) -> None:
        """Poll Iris until the mint is seen, then publish `transfer.arrived`.

        States: no message yet -> attested (Circle signed) -> forwarded/minted. Publishes
        `transfer.attested` once, and again with the reason if the forward fails or the wait
        runs out. Stops at attested when nothing here can mint (no forwarding, no Sepolia key).
        """
        s = self._settings
        deadline = self._clock() + s.cctp_timeout_s
        while self._clock() < deadline:
            try:
                msg = await self._iris.message(t.burn_tx)
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("attestation poll for %s failed, will retry: %s", t.id, exc)
                msg = None
            if msg is not None and msg.get("status") == "complete":
                if not t.attested:
                    t.attested = True
                    where = t.request.to_chain.value
                    await self._attested(
                        t,
                        f"Burned on Arc and attested by Circle; waiting to mint on {where}.",
                    )
                if s.cctp_forward:
                    state = str(msg.get("forwardState") or "").upper()
                    mint = msg.get("destinationMintTxHash") or (
                        msg.get("forwardTxHash") if state == "COMPLETE" else None
                    )
                    if state == "COMPLETE" and mint:
                        await self._arrived(t.id, t.request, str(mint))
                        return
                    if "FAIL" in state:
                        await self._attested(t, f"Circle's forwarding failed ({state}).")
                        return
                elif t.request.to_chain is Chain.ETH_SEPOLIA and s.eth_sepolia_rpc_url:
                    await self._mint_ourselves(t, msg)
                    return
                else:
                    await self._attested(
                        t, f"Attested; nothing here can mint on {t.request.to_chain.value}."
                    )
                    return
            await self._sleep(s.cctp_poll_interval_s)
        await self._attested(t, "Still waiting for the mint after the time limit.")

    async def _mint_ourselves(self, t: _Transfer, msg: dict[str, Any]) -> None:
        try:
            mint = await asyncio.to_thread(
                self._receive, str(msg["message"]), str(msg["attestation"])
            )
        except (TransferError, OSError, ValueError) as exc:
            log.warning("mint for %s failed: %s", t.id, exc)
            await self._attested(t, f"Attested, but the mint on Sepolia failed: {exc}")
            return
        await self._arrived(t.id, t.request, mint)
