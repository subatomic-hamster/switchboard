"""Requests the server holds for the owner before the vault is asked at all.

The vault contract knows one agent key and one set of limits. A connected assistant can be
given a tighter limit of its own ("asks every time", "up to 30 dollars without asking"). A
spend that the vault would allow, but that is over that assistant's limit, is held here: the
owner is asked exactly as for a vault request (same event, same phone call, same amount and
word), and only after a yes is the vault asked to spend.

`HoldingVault` wraps whatever satisfies `VaultPort` (the stub today, P1's module later) and
passes everything else straight through, so the frozen interface is unchanged. A hold can
only ever make the owner be asked more often. It cannot approve anything: after the owner
says yes the spend still goes through the vault's own checks, and if the vault would not
allow it by then, nothing is sent.

Holds live in memory, like the stub vault's pending requests. After a restart the approval
record for a hold is expired by `Approvals.resume`, which is the safe direction.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from app.events import EventBus
from app.ports import SpendResult, VaultPort
from app.schemas import ApprovalRequestedData, ApprovalResolvedData, Chain, Policy, Reason

if TYPE_CHECKING:
    from app.chain.anomaly import AnomalyDetector

HOLD_PREFIX = "h"


@dataclass
class _Hold:
    payee: str
    amount: Decimal
    reason: Reason
    chain: Chain | None = None
    resolved: bool = False


class HoldingVault:
    def __init__(
        self,
        inner: VaultPort,
        bus: EventBus,
        first_id: int = 1,
        anomaly: AnomalyDetector | None = None,
    ) -> None:
        self._inner = inner
        self._bus = bus
        self._anomaly = anomaly
        # Numbered above everything an earlier run recorded, for the same reason as the stubs.
        self._ids = itertools.count(first_id)
        self._holds: dict[str, _Hold] = {}
        self._notes: dict[str, str] = {}

    # ------------------------------------------------------------------ holds

    def note(self, request_id: str) -> str:
        """The objection a hold carries (the anomaly baseline's), for the approval call."""
        return self._notes.get(request_id, "")

    async def hold(
        self,
        payee: str,
        amount_usd: Decimal,
        reason: Reason,
        note: str = "",
        chain: Chain | None = None,
    ) -> SpendResult:
        """Ask the owner first. Publishes `approval.requested`, so the phone rings."""
        request_id = f"{HOLD_PREFIX}{next(self._ids)}"
        self._holds[request_id] = _Hold(payee=payee, amount=amount_usd, reason=reason, chain=chain)
        if note:
            self._notes[request_id] = note  # before the event, which the call is built from
        await self._bus.publish(
            "approval.requested",
            ApprovalRequestedData(
                request_id=request_id, payee=payee, amount_usd=amount_usd, reason=reason
            ),
        )
        return SpendResult(request_id=request_id, reason=reason)

    def _open(self, request_id: str) -> _Hold:
        hold = self._holds.get(request_id)
        if hold is None:
            raise ValueError("unknown request")
        if hold.resolved:
            raise ValueError("request already resolved")
        return hold

    async def _vault_objection(self, hold: _Hold) -> str | None:
        """Why the vault would not simply permit this spend right now, if it would not."""
        if await self._inner.is_frozen():
            return "the vault is frozen"
        policy = await self._inner.get_policy()
        if hold.amount > policy.transfer_cap_usd:
            return "it is now over the vault's per-transfer limit"
        if await self._inner.spent_today() + hold.amount > policy.daily_budget_usd:
            return "it would now go past the vault's daily budget"
        if hold.payee.lower() not in {allowed.lower() for allowed in policy.allowlist}:
            return "the payee is no longer on the vault's list"
        return None

    # ------------------------------------------------------------------ VaultPort

    async def request_spend(
        self, payee: str, amount_usd: Decimal, purpose: str, chain: Chain | None = None
    ) -> SpendResult:
        if self._anomaly is not None:
            anomaly = self._anomaly.assess(payee, amount_usd)
            # Only a spend the vault would allow is turned into an ask: if the vault would
            # refuse it, the vault's own (stronger) reason and request stand.
            if (
                anomaly is not None
                and await self._vault_objection(
                    _Hold(payee, amount_usd, Reason.UNUSUAL_SPEND, chain=chain)
                )
                is None
            ):
                return await self.hold(
                    payee, amount_usd, Reason.UNUSUAL_SPEND, anomaly.text, chain=chain
                )
        return await self._inner.request_spend(payee, amount_usd, purpose, chain)

    async def redeem(self, permit_id: str) -> None:
        await self._inner.redeem(permit_id)

    async def approve(self, request_id: str) -> str:
        if not request_id.startswith(HOLD_PREFIX):
            return await self._inner.approve(request_id)
        hold = self._open(request_id)
        # Check before asking, so a spend that has drifted outside the vault's limits since
        # it was held does not raise a second request and ring the phone again.
        objection = await self._vault_objection(hold)
        if objection is not None:
            raise ValueError(objection)
        result = await self._inner.request_spend(
            hold.payee, hold.amount, f"hold {request_id}", hold.chain
        )
        if result.permit_id is None:
            if result.request_id is not None:
                await self._inner.deny(result.request_id, via="superseded")
            raise ValueError("the vault did not permit it")
        hold.resolved = True
        await self._bus.publish(
            "approval.resolved",
            ApprovalResolvedData(request_id=request_id, approved=True, via="approver"),
        )
        return result.permit_id

    async def deny(self, request_id: str, via: str = "") -> None:
        if not request_id.startswith(HOLD_PREFIX):
            await self._inner.deny(request_id, via=via)
            return
        self._open(request_id).resolved = True
        await self._bus.publish(
            "approval.resolved",
            ApprovalResolvedData(request_id=request_id, approved=False, via=via or "approver"),
        )

    async def expire(self, request_id: str) -> None:
        if not request_id.startswith(HOLD_PREFIX):
            await self._inner.expire(request_id)
            return
        await self.deny(request_id, via="expiry")

    async def freeze(self, frozen: bool, via: str = "") -> None:
        await self._inner.freeze(frozen, via=via)

    async def set_policy(self, policy: Policy, via: str = "", rule_text: str = "") -> None:
        await self._inner.set_policy(policy, via=via, rule_text=rule_text)

    async def get_policy(self) -> Policy:
        return await self._inner.get_policy()

    async def spent_today(self) -> Decimal:
        return await self._inner.spent_today()

    async def is_frozen(self) -> bool:
        return await self._inner.is_frozen()
