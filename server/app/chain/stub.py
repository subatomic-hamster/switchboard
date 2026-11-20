"""STUB. Stands in for P1's `chain/` modules until they exist.

Nothing here touches a chain, a wallet or a key. `StubVault` applies the same policy rules the
vault contract will (per-transfer cap, rolling 24-hour budget, payee allowlist, freeze) in
memory, and publishes the events the QuickNode Stream webhook will publish. `StubTransfers`
pretends a transfer was sent and, a few seconds later, that it arrived.

P1: replace by providing objects that satisfy `app.ports.VaultPort` / `TransferPort` and a
router with the same three routes, then set USE_P1_STUBS=false.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from app.config import Settings
from app.context import AppContext, get_ctx
from app.db import utcnow
from app.events import EventBus
from app.ports import SpendResult
from app.schemas import (
    ApprovalRequestedData,
    ApprovalResolvedData,
    ChainTransferRequest,
    ChainTransferResponse,
    FrozenData,
    JsonObject,
    OkResponse,
    Policy,
    PolicyData,
    Reason,
    StreamStaleData,
    StreamToggleRequest,
    TransferArrivedData,
    TransferSentData,
)

STUB_AGENT = "0xa6e1700000000000000000000000000000000001"

Spawner = Callable[[Coroutine[Any, Any, Any], str], object]


@dataclass
class _Permit:
    payee: str
    amount: Decimal
    redeemed: bool = False


@dataclass
class _Pending:
    payee: str
    amount: Decimal
    reason: Reason
    resolved: bool = False


class StubVault:
    """STUB of the vault contract's behaviour. In memory; resets when the server restarts."""

    def __init__(
        self, bus: EventBus, settings: Settings, allowlist: list[str], first_id: int = 1
    ) -> None:
        self._bus = bus
        self._policy = Policy(
            transfer_cap_usd=settings.policy_transfer_cap_usd,
            daily_budget_usd=settings.policy_daily_budget_usd,
            allowlist=list(allowlist),
            approval_expiry_s=settings.approval_expiry_s,
        )
        self._frozen = False
        # `first_id` continues past every earlier run on the same database. Starting again
        # at 1 after a restart would reuse a request number the server has already seen.
        self._ids = itertools.count(first_id)
        self._permits: dict[str, _Permit] = {}
        self._pending: dict[str, _Pending] = {}
        self._spends: list[tuple[datetime, Decimal]] = []

    def _check(self, payee: str, amount: Decimal) -> Reason | None:
        if amount > self._policy.transfer_cap_usd:
            return Reason.OVER_TRANSFER_CAP
        if self._spent_24h() + amount > self._policy.daily_budget_usd:
            return Reason.OVER_DAILY_BUDGET
        if payee.lower() not in {a.lower() for a in self._policy.allowlist}:
            return Reason.PAYEE_NOT_ALLOWED
        return None

    def _spent_24h(self) -> Decimal:
        cutoff = utcnow() - timedelta(hours=24)
        self._spends = [(ts, amount) for ts, amount in self._spends if ts >= cutoff]
        return sum((amount for _, amount in self._spends), Decimal(0))

    async def request_spend(self, payee: str, amount_usd: Decimal, purpose: str) -> SpendResult:
        if self._frozen:
            # The contract reverts spends while frozen; nothing is queued for approval.
            return SpendResult(reason=Reason.FROZEN)
        reason = self._check(payee, amount_usd)
        next_id = str(next(self._ids))
        if reason is None:
            self._permits[next_id] = _Permit(payee=payee, amount=amount_usd)
            return SpendResult(permit_id=next_id)
        self._pending[next_id] = _Pending(payee=payee, amount=amount_usd, reason=reason)
        await self._bus.publish(
            "approval.requested",
            ApprovalRequestedData(
                request_id=next_id,
                agent=STUB_AGENT,
                payee=payee,
                amount_usd=amount_usd,
                reason=reason,
            ),
        )
        return SpendResult(request_id=next_id, reason=reason)

    async def redeem(self, permit_id: str) -> None:
        permit = self._permits.get(permit_id)
        if permit is None:
            raise ValueError("unknown permit")
        if permit.redeemed:
            raise ValueError("permit already redeemed")
        if self._frozen:
            raise ValueError("vault is frozen")
        permit.redeemed = True
        self._spends.append((utcnow(), permit.amount))

    async def approve(self, request_id: str) -> str:
        pending = self._open(request_id)
        pending.resolved = True
        permit_id = str(next(self._ids))
        self._permits[permit_id] = _Permit(payee=pending.payee, amount=pending.amount)
        await self._bus.publish(
            "approval.resolved",
            ApprovalResolvedData(request_id=request_id, approved=True, via="approver"),
        )
        return permit_id

    async def deny(self, request_id: str, via: str = "") -> None:
        pending = self._open(request_id)
        pending.resolved = True
        await self._bus.publish(
            "approval.resolved",
            ApprovalResolvedData(request_id=request_id, approved=False, via=via or "approver"),
        )

    async def expire(self, request_id: str) -> None:
        await self.deny(request_id, via="expiry")

    def _open(self, request_id: str) -> _Pending:
        pending = self._pending.get(request_id)
        if pending is None:
            raise ValueError("unknown request")
        if pending.resolved:
            raise ValueError("request already resolved")
        return pending

    async def freeze(self, frozen: bool, via: str = "") -> None:
        self._frozen = frozen
        await self._bus.publish("frozen", FrozenData(frozen=frozen, via=via))

    async def set_policy(self, policy: Policy, via: str = "", rule_text: str = "") -> None:
        self._policy = policy.model_copy(deep=True)
        await self._bus.publish(
            "policy.updated",
            PolicyData(
                transfer_cap_usd=policy.transfer_cap_usd,
                daily_budget_usd=policy.daily_budget_usd,
                allowlist_size=len(policy.allowlist),
                via=via,
                rule_text=rule_text,
            ),
        )

    async def get_policy(self) -> Policy:
        return self._policy.model_copy(deep=True)

    async def spent_today(self) -> Decimal:
        return self._spent_24h()

    async def is_frozen(self) -> bool:
        return self._frozen


class StubTransfers:
    """STUB of `chain/transfer.py`. Publishes `transfer.sent`, then `transfer.arrived` later."""

    def __init__(
        self, spawn: Spawner, bus: EventBus, settings: Settings, first_id: int = 1
    ) -> None:
        self._spawn = spawn
        self._bus = bus
        self._scale = settings.demo_time_scale
        self._ids = itertools.count(first_id)

    async def start(
        self,
        request: ChainTransferRequest,
        *,
        plan_id: str | None = None,
        payee_label: str = "",
        route_id: str = "",
        eta_s: int = 0,
    ) -> str:
        transfer_id = f"t_{next(self._ids)}"
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
            ),
        )
        delay = min(max(eta_s / self._scale, 0.05), 8.0)
        self._spawn(self._arrive(transfer_id, request, delay), f"arrive-{transfer_id}")
        return transfer_id

    async def _arrive(self, transfer_id: str, request: ChainTransferRequest, delay: float) -> None:
        await asyncio.sleep(delay)
        await self._bus.publish(
            "transfer.arrived",
            TransferArrivedData(
                transfer_id=transfer_id,
                to_chain=request.to_chain,
                amount_usd=request.amount_usd,
                tx_hash=f"0xstub{transfer_id}",
            ),
        )


class StubAnchor:
    """STUB of an onchain commitment. Keeps commitments in memory and returns a fake reference."""

    def __init__(self) -> None:
        self.commitments: list[tuple[str, str]] = []

    async def commit(self, commitment_hex: str, label: str) -> str:
        self.commitments.append((commitment_hex, label))
        return f"stub-anchor:{len(self.commitments)}"


router = APIRouter(prefix="/api", tags=["chain (STUB)"])
Ctx = Annotated[AppContext, Depends(get_ctx)]


@router.post("/chain/transfer", response_model=ChainTransferResponse)
async def chain_transfer(body: ChainTransferRequest, ctx: Ctx) -> ChainTransferResponse:
    """STUB. Pretends to send a transfer; emits `transfer.sent`, then `transfer.arrived`."""
    transfer_id = await ctx.transfers.start(body)
    return ChainTransferResponse(transfer_id=transfer_id)


# Separate so the real Stream webhook (chain/stream.py) can take its path when stubs are off.
hook_router = APIRouter(prefix="/api", tags=["chain (STUB)"])


@hook_router.post("/hooks/quicknode", response_model=OkResponse)
async def quicknode_hook(body: JsonObject) -> OkResponse:
    """STUB. P1's real handler verifies the HMAC, decodes vault events and publishes them."""
    return OkResponse(ok=True, status="stub: delivery ignored")


@router.post("/chain/stub/stream", response_model=OkResponse)
async def stub_stream(body: StreamToggleRequest, ctx: Ctx) -> OkResponse:
    """STUB, demo only. Simulates the Stream going quiet ("kill the stream") or recovering."""
    if not ctx.settings.use_p1_stubs:
        raise HTTPException(status_code=404, detail="stub routes are disabled")
    await ctx.bus.publish(
        "stream.stale",
        StreamStaleData(stale=body.stale, seconds_since_delivery=120 if body.stale else 0),
    )
    return OkResponse(ok=True, status="stale" if body.stale else "live")
