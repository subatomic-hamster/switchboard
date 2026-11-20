"""Escrow for a recipient with no wallet, and payment requests: `contracts/src/Escrow.sol`.

Flow chosen (simplified): the agent wallet deposits USDC directly. The owner funds the agent
wallet through the vault as usual (the agent address is a vault payee); this module then
approves the Escrow for the amount and calls `deposit`. Escrow itself must NOT be allowlisted as
a vault payee: the vault would transfer USDC to it without a `deposit` call, and that money would
have no claim hash and could never be claimed or refunded. A per-transfer cap is checked in the
route against the vault's policy, because a deposit does not pass through the vault's own check.

Claim links. `create` returns a random 32-byte secret; only `keccak256(secret)` goes onchain.
The link is `<PUBLIC_BASE_URL>/claim#<id>:<secret>`; the fragment is never sent to a server.
The recipient (or their page) posts the secret and a destination address to `/api/escrow/claim`.

Front-run defence. The contract claims in two transactions (commit a hash of id, secret and
destination, then claim in a later block), so the secret appearing in the mempool cannot be used
to redirect the funds. See `Escrow.sol`. This server relays both transactions with the agent key
(it pays the gas) and checks the secret itself before sending anything, so a wrong secret costs
no gas.

No bus events here: the Stream webhook (`app/chain/stream.py`) publishes them from the logs.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from web3 import Web3
from web3.exceptions import ContractLogicError, Web3RPCError

from app.chain.contract import Client, ContractRevert
from app.chain.vault import _hex, from_units, is_evm, to_units
from app.config import Settings
from app.context import AppContext, get_ctx
from app.schemas import Money

log = logging.getLogger("switchboard.chain")

ZERO = "0x" + "00" * 20
DEFAULT_EXPIRY_S = 7 * 24 * 3600
# `claim` is sent without gas estimation (see Client.send); it uses well under this.
CLAIM_GAS = 200_000
ERC20_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "approve",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "spender", "type": "address"}, {"name": "value", "type": "uint256"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "type": "function",
        "name": "allowance",
        "stateMutability": "view",
        "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EscrowCreated(_Out):
    escrow_id: str
    secret: str = Field(description="Shown once. Anyone with it can claim until expiry.")
    claim_link: str
    claim_hash: str
    expiry: datetime
    tx_hash: str


class EscrowStatus(_Out):
    escrow_id: str
    amount_usd: Money
    refund_to: str
    expiry: datetime
    settled: bool


class TxResult(_Out):
    tx_hash: str


class PaymentRequestCreated(_Out):
    request_id: str
    tx_hash: str


class ChainEscrow:
    """Create, claim and refund escrow deposits; make and pay payment requests."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = Client(settings, "Escrow", settings.escrow_address)

    # ------------------------------------------------------------------ plumbing

    def _usdc(self) -> Any:
        w3, _ = self._client.web3()
        return w3.eth.contract(
            address=Web3.to_checksum_address(self._settings.usdc_address), abi=ERC20_ABI
        )

    def _ensure_allowance(self, units: int) -> None:
        usdc = self._usdc()
        owner = self._client.agent_address()
        spender = Web3.to_checksum_address(self._settings.escrow_address)
        if usdc.functions.allowance(owner, spender).call() < units:
            self._client.send(usdc.functions.approve(spender, units))

    def _call(self, fn: Any) -> Any:
        try:
            return fn.call()
        except (ContractLogicError, Web3RPCError) as exc:
            raise ContractRevert(
                "Escrow", self._client.artifact.revert_name(exc), str(exc)
            ) from exc

    def _id_from(self, receipt: dict[str, Any], event: str) -> str:
        for decoded in self._client.events(receipt):
            if decoded["event"] == event:
                return str(decoded["id"])
        raise RuntimeError(f"transaction produced no {event} event")

    # ------------------------------------------------------------------ escrow

    async def create(
        self, amount_usd: Decimal, expiry_s: int = DEFAULT_EXPIRY_S, refund_to: str | None = None
    ) -> EscrowCreated:
        return await asyncio.to_thread(self._create, amount_usd, expiry_s, refund_to)

    def _create(self, amount_usd: Decimal, expiry_s: int, refund_to: str | None) -> EscrowCreated:
        w3, contract = self._client.web3()
        units = to_units(amount_usd)
        secret = secrets.token_bytes(32)
        claim_hash = Web3.keccak(secret)  # == keccak256(abi.encode(bytes32 secret))
        refund = Web3.to_checksum_address(refund_to or self._client.agent_address())
        expiry = int(w3.eth.get_block("latest")["timestamp"]) + expiry_s
        self._ensure_allowance(units)
        receipt = self._client.send(contract.functions.deposit(claim_hash, units, expiry, refund))
        escrow_id = self._id_from(receipt, "Deposited")
        base = self._settings.public_base_url.rstrip("/")
        return EscrowCreated(
            escrow_id=escrow_id,
            secret=_hex(secret),
            claim_link=f"{base}/claim#{escrow_id}:{_hex(secret)}",
            claim_hash=_hex(claim_hash),
            expiry=datetime.fromtimestamp(expiry, UTC),
            tx_hash=_hex(receipt["transactionHash"]),
        )

    async def claim(self, escrow_id: str, secret_hex: str, to: str) -> TxResult:
        return await asyncio.to_thread(self._claim, int(escrow_id), secret_hex, to)

    def _claim(self, escrow_id: int, secret_hex: str, to: str) -> TxResult:
        _, contract = self._client.web3()
        secret = bytes.fromhex(secret_hex.removeprefix("0x"))
        if len(secret) != 32:
            raise ValueError("the secret is 32 bytes")
        to = Web3.to_checksum_address(to)
        deposit = self._call(contract.functions.getDeposit(escrow_id))
        if Web3.keccak(secret) != bytes(deposit[4]):
            # Checked before any transaction: commitClaim does not look at the secret, and a
            # wrong guess must not cost the relayer gas.
            raise ContractRevert("Escrow", "WrongSecret")
        commitment = self._call(contract.functions.commitmentFor(escrow_id, secret, to))
        # Two transactions in two blocks: the commitment must be older than the claim.
        self._client.send(contract.functions.commitClaim(escrow_id, commitment))
        receipt = self._client.send(contract.functions.claim(escrow_id, secret, to), gas=CLAIM_GAS)
        return TxResult(tx_hash=_hex(receipt["transactionHash"]))

    async def refund(self, escrow_id: str) -> TxResult:
        return await asyncio.to_thread(self._refund, int(escrow_id))

    def _refund(self, escrow_id: int) -> TxResult:
        _, contract = self._client.web3()
        receipt = self._client.send(contract.functions.refund(escrow_id))
        return TxResult(tx_hash=_hex(receipt["transactionHash"]))

    async def status(self, escrow_id: str) -> EscrowStatus:
        return await asyncio.to_thread(self._status, int(escrow_id))

    def _status(self, escrow_id: int) -> EscrowStatus:
        _, contract = self._client.web3()
        refund_to, amount, expiry, settled, _hash = self._call(
            contract.functions.getDeposit(escrow_id)
        )
        return EscrowStatus(
            escrow_id=str(escrow_id),
            amount_usd=from_units(amount),
            refund_to=refund_to,
            expiry=datetime.fromtimestamp(expiry, UTC),
            settled=settled,
        )

    # ------------------------------------------------------------------ payment requests

    async def request(
        self, amount_usd: Decimal, payer: str | None = None, memo: str = ""
    ) -> PaymentRequestCreated:
        return await asyncio.to_thread(self._request, amount_usd, payer, memo)

    def _request(self, amount_usd: Decimal, payer: str | None, memo: str) -> PaymentRequestCreated:
        _, contract = self._client.web3()
        who = Web3.to_checksum_address(payer or ZERO)
        receipt = self._client.send(contract.functions.request(who, to_units(amount_usd), memo))
        return PaymentRequestCreated(
            request_id=self._id_from(receipt, "PaymentRequested"),
            tx_hash=_hex(receipt["transactionHash"]),
        )

    async def pay(self, request_id: str, cap_usd: Decimal) -> TxResult:
        """The agent wallet pays a request with its own USDC, up to `cap_usd` (the vault's
        per-transfer limit: this payment does not pass through the vault)."""
        return await asyncio.to_thread(self._pay, int(request_id), cap_usd)

    def _pay(self, request_id: int, cap_usd: Decimal) -> TxResult:
        _, contract = self._client.web3()
        _requester, _payer, amount, _paid = self._call(contract.functions.getRequest(request_id))
        if from_units(amount) > cap_usd:
            raise ValueError(f"over the per-transfer limit of ${cap_usd}; ask for approval")
        self._ensure_allowance(amount)
        receipt = self._client.send(contract.functions.pay(request_id))
        return TxResult(tx_hash=_hex(receipt["transactionHash"]))


# --------------------------------------------------------------------------- routes

router = APIRouter(prefix="/api/escrow", tags=["escrow"])
Ctx = Annotated[AppContext, Depends(get_ctx)]


class CreateEscrow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_usd: Money = Field(gt=0)
    expiry_s: int = Field(default=DEFAULT_EXPIRY_S, ge=60, le=90 * 24 * 3600)
    refund_to: str | None = None


class ClaimEscrow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    escrow_id: str = Field(pattern=r"^[0-9]{1,18}$")
    secret: str = Field(pattern=r"^(0x)?[0-9a-fA-F]{64}$")
    to: str


class CreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_usd: Money = Field(gt=0)
    payer: str | None = None
    memo: str = Field(default="", max_length=200)


def _escrow(ctx: AppContext) -> ChainEscrow:
    if ctx.escrow is None:
        raise HTTPException(status_code=503, detail="escrow is not configured")
    return ctx.escrow


async def _spending_allowed(ctx: AppContext) -> None:
    """Escrow deposits and request payments spend the agent wallet without going through the
    vault, so they honour the kill switch here: nothing while frozen or while the Stream is
    stale (see `chain/watchdog.py`). Claims and refunds return money that is already escrowed,
    so they are not held up."""
    if await ctx.vault.is_frozen():
        raise HTTPException(status_code=409, detail="the vault is frozen, so spending is paused")
    if ctx.stream_stale:
        raise HTTPException(
            status_code=409, detail="the event stream is stale, so spending is paused"
        )


def _evm(address: str | None, what: str) -> None:
    if address is not None and not is_evm(address):
        raise HTTPException(status_code=422, detail=f"{what} must be an EVM address")


def _refused(exc: ValueError) -> HTTPException:
    """A contract refusal (`ContractRevert`) or a bad value, as a 409 the caller can read."""
    return HTTPException(status_code=409, detail=str(exc))


@router.post("/create", response_model=EscrowCreated)
async def create(body: CreateEscrow, ctx: Ctx) -> EscrowCreated:
    """Local only. Hold USDC for a recipient with no wallet; returns the claim secret and link."""
    escrow = _escrow(ctx)
    _evm(body.refund_to, "refund_to")
    await _spending_allowed(ctx)
    cap = (await ctx.vault.get_policy()).transfer_cap_usd
    if body.amount_usd > cap:
        raise HTTPException(
            status_code=409, detail=f"over the per-transfer limit of ${cap}; ask for approval"
        )
    try:
        return await escrow.create(body.amount_usd, body.expiry_s, body.refund_to)
    except ValueError as exc:
        raise _refused(exc) from exc


@router.post("/claim", response_model=TxResult)
async def claim(body: ClaimEscrow, ctx: Ctx) -> TxResult:
    """Public: the secret is the credential. Pays `to` through the commit-reveal claim."""
    escrow = _escrow(ctx)
    _evm(body.to, "to")
    try:
        return await escrow.claim(body.escrow_id, body.secret, body.to)
    except ValueError as exc:
        raise _refused(exc) from exc


@router.post("/requests", response_model=PaymentRequestCreated)
async def create_request(body: CreateRequest, ctx: Ctx) -> PaymentRequestCreated:
    """Local only. Ask `payer` (or anyone) to pay the agent wallet."""
    _evm(body.payer, "payer")
    try:
        return await _escrow(ctx).request(body.amount_usd, body.payer, body.memo)
    except ValueError as exc:
        raise _refused(exc) from exc


@router.post("/requests/{request_id}/pay", response_model=TxResult)
async def pay_request(request_id: str, ctx: Ctx) -> TxResult:
    """Local only. The agent wallet pays a request, within the per-transfer limit."""
    escrow = _escrow(ctx)
    if not request_id.isdigit():
        raise HTTPException(status_code=422, detail="request ids are numbers")
    await _spending_allowed(ctx)
    try:
        cap = (await ctx.vault.get_policy()).transfer_cap_usd
        return await escrow.pay(request_id, cap)
    except ValueError as exc:
        raise _refused(exc) from exc


@router.post("/{escrow_id}/refund", response_model=TxResult)
async def refund(escrow_id: str, ctx: Ctx) -> TxResult:
    """Local only. Return an unclaimed deposit to its refund address after expiry."""
    escrow = _escrow(ctx)
    if not escrow_id.isdigit():
        raise HTTPException(status_code=422, detail="escrow ids are numbers")
    try:
        return await escrow.refund(escrow_id)
    except ValueError as exc:
        raise _refused(exc) from exc


@router.get("/{escrow_id}", response_model=EscrowStatus)
async def status(escrow_id: str, ctx: Ctx) -> EscrowStatus:
    """Local only. Amount, expiry and whether it is settled; never the secret."""
    escrow = _escrow(ctx)
    if not escrow_id.isdigit():
        raise HTTPException(status_code=422, detail="escrow ids are numbers")
    try:
        return await escrow.status(escrow_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
