"""What the Grok Bot routines can reach: read-only facts, and a place to leave notes.

Four routines (their text and setup are in `bots/routines.md`):

- missed-call escalation, triggered by a webhook this server sends when an approval call is
  not answered;
- the daily spoken summary, on a schedule;
- equivalence research with X search, which posts what it found about a pair of markets;
- ledger reconciliation, on a schedule.

Bots cannot move money. Every route here either reads, or stores a note. Notes are outside
content: they are shown to the owner labelled as unverified, and nothing in the server acts on
them. In particular a research note never changes the equivalence verdict, which comes only
from the five-field check in code. Bot routes take their own bearer token (BOT_TOKEN), which
is not accepted by the voice tools, and the voice token is not accepted here.
"""

from __future__ import annotations

import hmac
import logging
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import col, select

from app.context import AppContext, get_ctx
from app.db import ApprovalRow, BotNoteRow, LedgerRow, as_utc, from_micro, utcnow
from app.planner import say_money
from app.schemas import Money, OkResponse

log = logging.getLogger("switchboard.bots")

NOTE_LIMIT = 2000


class LedgerLine(BaseModel):
    ts: datetime
    kind: str
    ref: str
    from_place: str
    to_place: str
    amount_usd: Money
    cost_usd: Money
    payee_label: str
    status: str
    note: str


class LedgerResponse(BaseModel):
    lines: list[LedgerLine]
    sent_total_usd: Money
    vault_spent_24h_usd: Money
    consistent: bool = Field(
        description="True when transfers in the ledger for the last 24 hours add up to what "
        "the vault says was spent."
    )


class DailySummary(BaseModel):
    hours: int
    transfers: int
    sent_total_usd: Money
    fees_total_usd: Money
    approvals_asked: int
    approvals_approved: int
    approvals_denied: int
    blocked: dict[str, int]
    calls: int
    missed_calls: int
    dry_run_trades: int
    frozen: bool
    stream_stale: bool
    spoken: str


class MissedCall(BaseModel):
    request_id: str
    ts: datetime
    amount_usd: Money
    payee_label: str
    reason: str
    outcome: str
    how: str


class MissedCallList(BaseModel):
    missed: list[MissedCall]


class BotNoteIn(BaseModel):
    subject: str = Field(default="", max_length=200)
    body: str = Field(min_length=1, max_length=NOTE_LIMIT)


class BotNote(BaseModel):
    ts: datetime
    routine: str
    subject: str
    body: str = Field(description="Written by a bot from outside sources. Unverified.")


class BotNoteList(BaseModel):
    notes: list[BotNote]


class Bots:
    def __init__(self, ctx: AppContext, http: httpx.AsyncClient | None = None) -> None:
        self._ctx = ctx
        self._http = http
        ctx.approvals.missed_call_hooks.append(self._escalate)

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    # ------------------------------------------------------------------ missed calls

    async def _escalate(self, row: ApprovalRow, call_status: str) -> None:
        """Tell the missed-call routine. The bot follows up by message; it can do no more."""
        url = self._ctx.settings.grok_missed_call_webhook_url
        if not url:
            log.info("missed call for request %s; no escalation webhook is set", row.request_id)
            return
        if not url.startswith("https://"):
            log.warning("GROK_MISSED_CALL_WEBHOOK_URL is not https; not sending")
            return
        amount = from_micro(row.amount_micro)
        payload = {
            "event": "missed_approval_call",
            "request_id": row.request_id,
            "amount_usd": float(amount),
            "payee_label": row.payee_label,
            "reason": row.reason,
            "outcome": "denied",
            "how": call_status,
            "message": (
                f"Switchboard called about {say_money(amount)} to {row.payee_label} and got no "
                "decision, so the request was denied. Nothing was sent. If you still want it, "
                "ask again."
            ),
        }
        if self._http is None:
            self._http = httpx.AsyncClient()
        try:
            response = await self._http.post(url, json=payload, timeout=15.0)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("missed-call escalation failed: %s", type(exc).__name__)

    def missed(self, hours: int) -> list[MissedCall]:
        cutoff = utcnow() - timedelta(hours=hours)
        with self._ctx.db.session() as session:
            rows = session.exec(
                select(ApprovalRow)
                .where(
                    col(ApprovalRow.created) >= cutoff,
                    col(ApprovalRow.resolved_via).in_(
                        ("expiry", "no_answer", "call_ended", "restart", "call_cap")
                    ),
                )
                .order_by(col(ApprovalRow.created).desc())
            ).all()
        return [
            MissedCall(
                request_id=row.request_id,
                ts=as_utc(row.created),
                amount_usd=from_micro(row.amount_micro),
                payee_label=row.payee_label,
                reason=row.reason,
                outcome=row.status,
                how=row.resolved_via,
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ ledger

    def _ledger_rows(self, hours: int) -> list[LedgerRow]:
        cutoff = utcnow() - timedelta(hours=hours)
        with self._ctx.db.session() as session:
            return list(
                session.exec(
                    select(LedgerRow)
                    .where(col(LedgerRow.ts) >= cutoff)
                    .order_by(col(LedgerRow.id).desc())
                ).all()
            )

    async def ledger(self, hours: int) -> LedgerResponse:
        rows = self._ledger_rows(hours)
        transfers = [row for row in rows if row.kind == "transfer"]
        sent_total = sum((from_micro(row.amount_micro) for row in transfers), Decimal(0))
        day = [row for row in self._ledger_rows(24) if row.kind == "transfer"]
        day_total = sum((from_micro(row.amount_micro) for row in day), Decimal(0))
        vault_spent = await self._ctx.vault.spent_today()
        return LedgerResponse(
            lines=[
                LedgerLine(
                    ts=as_utc(row.ts),
                    kind=row.kind,
                    ref=row.ref,
                    from_place=row.from_place,
                    to_place=row.to_place,
                    amount_usd=from_micro(row.amount_micro),
                    cost_usd=from_micro(row.cost_micro),
                    payee_label=row.payee_label,
                    status=row.status,
                    note=row.note,
                )
                for row in rows
            ],
            sent_total_usd=sent_total,
            vault_spent_24h_usd=vault_spent,
            consistent=day_total == vault_spent,
        )

    # ------------------------------------------------------------------ summary

    async def summary(self, hours: int) -> DailySummary:
        ctx = self._ctx
        cutoff = utcnow() - timedelta(hours=hours)
        events = ctx.bus.history(since=cutoff)
        counts = Counter(event.type for event in events)
        blocked = Counter(
            str(event.data.get("reason")) for event in events if event.type == "blocked"
        )
        resolved = [event for event in events if event.type == "approval.resolved"]
        approved = sum(1 for event in resolved if event.data.get("approved"))
        rows = self._ledger_rows(hours)
        transfers = [row for row in rows if row.kind == "transfer"]
        sent = sum((from_micro(row.amount_micro) for row in transfers), Decimal(0))
        fees = sum((from_micro(row.cost_micro) for row in transfers), Decimal(0))
        missed = len(self.missed(hours))
        frozen = await ctx.vault.is_frozen()

        parts = [
            f"In the last {hours} hours Switchboard sent {len(transfers)} "
            f"transfer{'s' if len(transfers) != 1 else ''} totalling {say_money(sent)}"
            + (f", paying {say_money(fees)} in fees." if transfers else ".")
        ]
        asked = counts["approval.requested"]
        if asked:
            parts.append(
                f"It asked for your approval {asked} time{'s' if asked != 1 else ''}: "
                f"{approved} approved, {len(resolved) - approved} denied."
            )
        if missed:
            parts.append(
                f"{missed} request{'s were' if missed != 1 else ' was'} denied because no "
                "decision was given."
            )
        if blocked:
            listed = ", ".join(f"{count} for {reason}" for reason, count in blocked.items())
            parts.append(f"It blocked {sum(blocked.values())} without asking: {listed}.")
        if counts["trade.filled"]:
            parts.append("Position moves ran as dry runs only; no order was placed.")
        if frozen:
            parts.append("The vault is frozen right now.")
        if ctx.stream_stale:
            parts.append("The event stream is stale, so spending is paused.")
        if len(parts) == 1 and not transfers:
            parts = [f"Nothing moved in the last {hours} hours, and nothing needed your say."]

        return DailySummary(
            hours=hours,
            transfers=len(transfers),
            sent_total_usd=sent,
            fees_total_usd=fees,
            approvals_asked=asked,
            approvals_approved=approved,
            approvals_denied=len(resolved) - approved,
            blocked=dict(blocked),
            calls=counts["call.started"],
            missed_calls=missed,
            dry_run_trades=counts["trade.filled"],
            frozen=frozen,
            stream_stale=ctx.stream_stale,
            spoken=" ".join(parts),
        )

    # ------------------------------------------------------------------ notes

    def add_note(self, routine: str, subject: str, body: str) -> None:
        with self._ctx.db.session() as session:
            session.add(
                BotNoteRow(
                    ts=utcnow(),
                    routine=routine,
                    subject=" ".join(subject.split())[:200],
                    body=body.strip()[:NOTE_LIMIT],
                )
            )
            session.commit()

    def notes(self, routine: str, subject: str | None = None, limit: int = 10) -> list[BotNote]:
        statement = select(BotNoteRow).where(BotNoteRow.routine == routine)
        if subject is not None:
            statement = statement.where(BotNoteRow.subject == subject)
        with self._ctx.db.session() as session:
            rows = session.exec(statement.order_by(col(BotNoteRow.id).desc()).limit(limit)).all()
        return [
            BotNote(ts=as_utc(row.ts), routine=row.routine, subject=row.subject, body=row.body)
            for row in rows
        ]


Ctx = Annotated[AppContext, Depends(get_ctx)]
Hours = Annotated[int, Query(ge=1, le=24 * 7)]


def require_bot_token(ctx: Ctx, authorization: Annotated[str | None, Header()] = None) -> None:
    expected = ctx.settings.bot_token
    if not expected:
        raise HTTPException(status_code=503, detail="BOT_TOKEN is not set on the server")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not authorization or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid bot token")


router = APIRouter(prefix="/api/bots", tags=["bots"], dependencies=[Depends(require_bot_token)])


@router.get("/summary", response_model=DailySummary)
async def bot_summary(ctx: Ctx, hours: Hours = 24) -> DailySummary:
    """For the daily summary routine. `spoken` is ready to post or read out."""
    return await ctx.bots.summary(hours)


@router.get("/ledger", response_model=LedgerResponse)
async def bot_ledger(ctx: Ctx, hours: Hours = 24) -> LedgerResponse:
    """For the reconciliation routine: every movement, and whether it adds up."""
    return await ctx.bots.ledger(hours)


@router.get("/missed-calls", response_model=MissedCallList)
async def bot_missed_calls(ctx: Ctx, hours: Hours = 24) -> MissedCallList:
    """Approval requests that were denied because nobody decided."""
    return MissedCallList(missed=ctx.bots.missed(hours))


@router.post("/reconciliation", response_model=OkResponse)
async def bot_reconciliation(body: BotNoteIn, ctx: Ctx) -> OkResponse:
    """The reconciliation routine reports what it found. Stored as a note; nothing acts on it."""
    ctx.bots.add_note("reconciliation", body.subject, body.body)
    return OkResponse(ok=True, status="noted")


@router.post("/equivalence", response_model=OkResponse)
async def bot_equivalence(body: BotNoteIn, ctx: Ctx) -> OkResponse:
    """The research routine posts what it found about a market (subject = the market slug).
    Shown to the owner as unverified. It never changes the verdict of the equivalence check."""
    ctx.bots.add_note("equivalence", body.subject, body.body)
    return OkResponse(ok=True, status="noted")


dashboard = APIRouter(prefix="/api", tags=["bots"])


@dashboard.get("/summary", response_model=DailySummary)
async def summary(ctx: Ctx, hours: Hours = 24) -> DailySummary:
    """The same summary, for the dashboard's "hear today's summary"."""
    return await ctx.bots.summary(hours)


@dashboard.get("/notes", response_model=BotNoteList)
async def notes(ctx: Ctx, routine: str = "equivalence", subject: str | None = None) -> BotNoteList:
    """Notes left by bot routines, for display as unverified outside content."""
    return BotNoteList(notes=ctx.bots.notes(routine, subject))
