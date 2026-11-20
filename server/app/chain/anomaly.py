"""An anomaly baseline: flag a spend that departs from the agent's own history.

The vault's caps are fixed numbers. A hijacked agent can still stay under them, for example
fifty dollars to a new address at 3 a.m. after weeks of two-dollar payments. This scores each
proposed spend against what the agent has actually done, and when it looks wrong a spend the
vault would allow becomes an "ask": `HoldingVault` raises `approval.requested` and the phone
rings, with the reason below as the risk officer's objection. It can only make the owner be
asked more often; it never approves or loosens anything.

History is what was really sent: every `transfer.sent` (amount, payee, time), seeded at start
from the plans the database recorded, so a restart keeps the baseline. With fewer than
`ANOMALY_MIN_SAMPLES` spends (default 8) nothing is flagged: there is no baseline yet.

Rules, each explainable in one sentence (any one flags):
  amount  at least ANOMALY_AMOUNT_FACTOR (5) times the median spend, and, when the history
          varies, a robust z-score (0.6745 * (x - median) / MAD) of at least 3.5, so a noisy
          history needs a bigger jump. MAD is the median absolute deviation.
  payee   an address this agent has never paid.
  burst   this would be the Nth spend inside ANOMALY_BURST_WINDOW_S (default 4 in 10 minutes)
          and more than any burst in the history.
  hour    UTC hour of day at least 3 hours from every hour the agent has spent in.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlmodel import col, select

from app.config import Settings
from app.db import Database, PlanRow, as_utc, utcnow
from app.events import EventBus, StoredEvent
from app.planner import say_money
from app.schemas import TransferRequest

log = logging.getLogger("switchboard.chain")

MAX_HISTORY = 500
ROBUST_Z = Decimal("3.5")
MAD_SCALE = Decimal("0.6745")
HOUR_GAP = 3


@dataclass(frozen=True)
class Spend:
    ts: datetime
    amount: Decimal
    payee: str


@dataclass(frozen=True)
class Anomaly:
    reasons: tuple[str, ...]

    @property
    def text(self) -> str:
        """What the operator says on the call. Built from numbers, never from outside text."""
        return " ".join(self.reasons) + " If you did not expect it, say deny."


def _hour_gap(a: int, b: int) -> int:
    return min((a - b) % 24, (b - a) % 24)


def _max_burst(times: list[datetime], window: timedelta) -> int:
    """The most spends the history ever made inside one window."""
    times = sorted(times)
    best, start = 0, 0
    for end, ts in enumerate(times):
        while ts - times[start] > window:
            start += 1
        best = max(best, end - start + 1)
    return best


class AnomalyDetector:
    def __init__(self, settings: Settings, db: Database, bus: EventBus) -> None:
        self._settings = settings
        self._db = db
        self.history: deque[Spend] = deque(maxlen=MAX_HISTORY)
        self._seed()
        bus.on("transfer.sent", self._on_sent)

    # ------------------------------------------------------------------ history

    def _seed(self) -> None:
        with self._db.session() as session:
            rows = session.exec(
                select(PlanRow)
                .where(col(PlanRow.transfer_id).is_not(None))
                .order_by(col(PlanRow.id))
            ).all()
        for row in rows:
            self._record_plan(row)

    def _record_plan(self, row: PlanRow) -> None:
        try:
            request = TransferRequest.model_validate_json(row.request_json)
        except ValueError:
            return
        if request.destination.address:
            self.history.append(
                Spend(as_utc(row.created), request.amount_usd, request.destination.address.lower())
            )

    async def _on_sent(self, event: StoredEvent) -> None:
        plan_id = str(event.data.get("plan_id") or "")
        if not plan_id[2:].isdigit():
            return
        with self._db.session() as session:
            row = session.get(PlanRow, int(plan_id[2:]))
        if row is not None:
            self._record_plan(row)

    # ------------------------------------------------------------------ scoring

    def assess(self, payee: str, amount: Decimal, now: datetime | None = None) -> Anomaly | None:
        settings = self._settings
        history = list(self.history)
        if len(history) < settings.anomaly_min_samples:
            return None
        now = now or utcnow()
        reasons: list[str] = []

        amounts = [spend.amount for spend in history]
        median = statistics.median(amounts)
        mad = statistics.median(abs(a - median) for a in amounts)
        if median > 0 and amount >= settings.anomaly_amount_factor * median:
            robust_z = MAD_SCALE * (amount - median) / mad if mad > 0 else None
            if robust_z is None or robust_z >= ROBUST_Z:
                times = round(amount / median)
                reasons.append(f"This is {times} times your usual transfer of {say_money(median)}.")

        if payee.lower() not in {spend.payee for spend in history}:
            reasons.append("This agent has never paid this address before.")

        window = timedelta(seconds=settings.anomaly_burst_window_s)
        recent = sum(1 for spend in history if timedelta(0) <= now - spend.ts <= window) + 1
        if recent >= settings.anomaly_burst_count and recent > _max_burst(
            [spend.ts for spend in history], window
        ):
            reasons.append(
                f"That would be payment number {recent} in {settings.anomaly_burst_window_s // 60} "
                "minutes, faster than this agent has ever paid."
            )

        hour = as_utc(now).hour
        if all(_hour_gap(hour, spend.ts.hour) >= HOUR_GAP for spend in history):
            reasons.append("It is an hour of the day when this agent has never spent.")

        if not reasons:
            return None
        log.info("anomaly: %s", json.dumps(reasons))
        return Anomaly(tuple(reasons))
