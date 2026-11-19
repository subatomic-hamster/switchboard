"""The approval call: outbound call, the four voice tools, and the rules for saying yes.

Flow. The vault emits `ApprovalRequested`; P1's Stream webhook publishes `approval.requested`
on the bus; this module reacts: it stores the request with a one-time challenge word, then
places an outbound ElevenLabs call through Twilio (or logs it, in dry-run). On the call the
agent uses the tools below. The agent prompt and tool definitions are in `app/agent_config.py`.

What it takes to approve:

- the amount said back must equal the pending amount exactly;
- the one-time word shown on the owner's dashboard must be said. The word is never sent to the
  voice agent, so someone who only hears the call cannot learn it;
- above PASSKEY_THRESHOLD_USD, a passkey confirmation on the dashboard as well;
- three wrong attempts deny the request;
- no answer, or no decision before the expiry, denies the request.

Nothing here authenticates by voice. A cloned voice gains nothing: calls go only to
OWNER_PHONE_NUMBER, and approval needs the word from the owner's screen.

Dry-run. Unless CALLS_ENABLED=true and the ElevenLabs agent id, phone-number id and owner
number are all set, no call is placed: the request that would be sent is logged, and the
dashboard offers a labelled "simulate the call" form that goes through the same checks.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from decimal import Decimal
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlmodel import col, func, select

from app.context import AppContext, get_ctx
from app.db import ApprovalRow, CallRow, PlanRow, as_utc, from_micro, to_micro, utcnow
from app.events import StoredEvent
from app.planner import say_money, say_time
from app.schemas import (
    ApprovalList,
    ApprovalStats,
    ApprovalView,
    BlockedData,
    CallData,
    OkResponse,
    Reason,
    Route,
    SimulatedVoiceRequest,
    VoiceApproveRequest,
    VoiceDenyRequest,
    VoiceFreezeRequest,
    VoiceRequestInfo,
)

log = logging.getLogger("switchboard.voice")

ELEVENLABS_API = "https://api.elevenlabs.io"
OUTBOUND_CALL_PATH = "/v1/convai/twilio/outbound-call"
MAX_ATTEMPTS = 3
RATE_WARNING_MIN_RESOLVED = 3
WEBHOOK_TOLERANCE_S = 30 * 60

# Short, common, distinct-sounding words. One is picked per request and shown on the dashboard.
CHALLENGE_WORDS = (
    "harbor", "lantern", "maple", "orbit", "pebble", "quartz", "river", "saddle", "timber",
    "velvet", "walnut", "anchor", "basil", "candle", "dolphin", "ember", "falcon", "garden",
    "hammer", "island", "jacket", "kettle", "ladder", "meadow", "nickel", "olive", "parcel",
    "rabbit", "silver", "tunnel", "umbrella", "violin", "window", "yellow", "zipper", "bridge",
    "copper", "desert", "engine", "forest", "glacier", "helmet", "igloo", "jungle", "kitten",
    "lemon", "magnet", "napkin", "ocean", "pillow", "rocket", "sunset", "ticket", "valley",
    "wagon", "button", "cactus", "dragon", "feather", "guitar", "hazel", "marble", "pepper",
    "thunder",
)  # fmt: skip

REASON_PHRASES: dict[Reason, str] = {
    Reason.OVER_TRANSFER_CAP: "it is over your per-transfer limit",
    Reason.OVER_DAILY_BUDGET: "it would take you past your daily budget",
    Reason.PAYEE_NOT_ALLOWED: "the destination is one you have never paid before",
    Reason.FROZEN: "the vault is frozen",
}

MissedCallHook = Callable[[ApprovalRow, str], Awaitable[None]]


def mask_number(number: str) -> str:
    return f"{number[:2]}{'*' * max(len(number) - 6, 0)}{number[-4:]}" if number else ""


def normalise_word(word: str) -> str:
    return "".join(ch for ch in word.lower() if ch.isalpha())


class Approvals:
    def __init__(self, ctx: AppContext, http: httpx.AsyncClient | None = None) -> None:
        self._ctx = ctx
        self._http = http
        self.missed_call_hooks: list[MissedCallHook] = []
        ctx.bus.on("approval.requested", self._on_requested)
        ctx.bus.on("approval.resolved", self._on_resolved)

    # ------------------------------------------------------------------ storage helpers

    def get(self, request_id: str) -> ApprovalRow | None:
        with self._ctx.db.session() as session:
            return session.get(ApprovalRow, request_id)

    def _save(self, row: ApprovalRow) -> None:
        with self._ctx.db.session() as session:
            session.add(row)
            session.commit()

    def calls_in_last_day(self) -> int:
        cutoff = utcnow() - timedelta(hours=24)
        with self._ctx.db.session() as session:
            return int(
                session.exec(
                    select(func.count()).select_from(CallRow).where(col(CallRow.ts) >= cutoff)
                ).one()
            )

    # ------------------------------------------------------------------ new request

    async def _on_requested(self, event: StoredEvent) -> None:
        """A pending request exists onchain. Store it, then call the owner (or hit the cap)."""
        ctx = self._ctx
        data = event.data
        request_id = str(data["request_id"])
        if self.get(request_id) is not None:
            return  # the Stream may deliver an event more than once
        amount = Decimal(str(data["amount_usd"]))
        reason = Reason(data["reason"])
        payee = str(data["payee"])
        label = ctx.wallets.label_for(payee)
        policy = await ctx.vault.get_policy()
        spent = await ctx.vault.spent_today()
        now = utcnow()
        row = ApprovalRow(
            request_id=request_id,
            amount_micro=to_micro(amount),
            payee_label=label,
            payee_address=payee,
            reason=reason.value,
            challenge=secrets.choice(CHALLENGE_WORDS),
            created=now,
            expires_at=now + timedelta(seconds=policy.approval_expiry_s),
            passkey_required=amount > ctx.settings.passkey_threshold_usd,
            case_for="The agent asked for this spend. No sentence of yours is on record for it.",
            case_against=self._case_against(
                reason, amount, label, spent, policy.transfer_cap_usd, policy.daily_budget_usd
            ),
        )
        self._save(row)
        plan = ctx.planner.plan_for_request(request_id)
        if plan is not None:
            self.attach_plan(request_id, plan)

        if self.calls_in_last_day() >= ctx.settings.max_calls_per_day:
            # Anti-rubber-stamping: past the daily cap the phone does not ring; the answer is no.
            row = self._require(request_id)
            row.call_status = "capped"
            self._save(row)
            await ctx.bus.publish(
                "blocked",
                BlockedData(
                    reason=Reason.CALL_CAP_REACHED,
                    plan_id=f"p_{row.plan_id}" if row.plan_id else None,
                    amount_usd=amount,
                    payee_label=label,
                    detail=(
                        f"Daily limit of {ctx.settings.max_calls_per_day} approval calls "
                        "reached. Denied without calling."
                    ),
                ),
            )
            await self.deny(request_id, via="call_cap")
            return

        await self.start_approval_call(request_id)
        ctx.tasks.spawn(
            self._expire_later(request_id, policy.approval_expiry_s), f"expire-{request_id}"
        )

    @staticmethod
    def _case_against(
        reason: Reason,
        amount: Decimal,
        label: str,
        spent: Decimal,
        cap: Decimal,
        budget: Decimal,
    ) -> str:
        """The risk officer's one objection. Built from numbers, never from outside text."""
        if reason == Reason.OVER_TRANSFER_CAP:
            return (
                f"It is {say_money(amount - cap)} over your {say_money(cap)} transfer limit. "
                "You set that limit so that one mistake stays small."
            )
        if reason == Reason.OVER_DAILY_BUDGET:
            return (
                f"You have already spent {say_money(spent)} today. This would take the day to "
                f"{say_money(spent + amount)}, past your {say_money(budget)} budget."
            )
        if reason == Reason.PAYEE_NOT_ALLOWED:
            return (
                f"You have never paid {label} before. If the address is wrong, "
                "the money cannot be recalled."
            )
        return "The vault flagged this request."

    def attach_plan(self, request_id: str, plan: PlanRow) -> None:
        """Link the request to the owner's sentence, which is the case for approving it."""
        row = self.get(request_id)
        if row is None or plan.id is None:
            return
        row.plan_id = plan.id
        row.source = plan.source
        routes = [Route.model_validate(item) for item in json.loads(plan.routes_json or "[]")]
        route = next((r for r in routes if r.id == plan.route_id), routes[0] if routes else None)
        if plan.source == "hostile":
            row.case_for = (
                "Nothing supports this. It did not come from a sentence you spoke; it was "
                "triggered by outside content the agent read."
            )
        else:
            cost = (
                f" The {route.id} route costs {say_money(route.cost_usd)} and takes "
                f"{say_time(route.eta_s)}."
                if route
                else ""
            )
            row.case_for = f'You asked for this yourself: "{plan.text.strip()}".{cost}'
        self._save(row)

    # ------------------------------------------------------------------ the call

    def call_variables(self, row: ApprovalRow) -> dict[str, Any]:
        """Per-call variables for the agent. The challenge word is deliberately absent."""
        reason = Reason(row.reason)
        amount = from_micro(row.amount_micro)
        return {
            "request_id": row.request_id,
            "amount_usd": f"{amount.normalize():f}",
            "amount_words": say_money(amount),
            "payee_label": row.payee_label,
            "reason_code": reason.value,
            "reason": REASON_PHRASES.get(reason, "the vault flagged it"),
            "case_for": row.case_for,
            "case_against": row.case_against,
            "requires_passkey": "yes" if row.passkey_required else "no",
        }

    async def start_approval_call(self, request_id: str) -> CallRow:
        """Call the owner about a pending request. Dry-run unless calls are fully enabled."""
        ctx, settings = self._ctx, self._ctx.settings
        row = self._require(request_id)
        spent = await ctx.vault.spent_today()
        variables = {**self.call_variables(row), "spent_today_usd": f"{spent.normalize():f}"}
        body = {
            "agent_id": settings.elevenlabs_agent_id,
            "agent_phone_number_id": settings.elevenlabs_phone_number_id,
            # Only ever the registered owner number; never a number from request data.
            "to_number": settings.owner_phone_number,
            "conversation_initiation_client_data": {"dynamic_variables": variables},
        }
        call = CallRow(
            request_id=request_id, ts=utcnow(), dry_run=not settings.calls_live, status=""
        )

        if not settings.calls_live:
            call.status = "started"
            call.detail = "dry run: no call placed"
            log.info(
                "DRY RUN approval call for request %s to %s: %s",
                request_id,
                mask_number(settings.owner_phone_number) or "(no owner number set)",
                json.dumps(variables),
            )
        else:
            try:
                response = await self._client().post(
                    f"{ELEVENLABS_API}{OUTBOUND_CALL_PATH}",
                    headers={"xi-api-key": settings.elevenlabs_api_key},
                    json=body,
                    timeout=20.0,
                )
                response.raise_for_status()
                payload = response.json()
                if not payload.get("success", False):
                    raise RuntimeError(str(payload.get("message", "call was not started")))
                call.status = "started"
                call.conversation_id = str(payload.get("conversation_id") or "")
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                call.status = "failed"
                call.detail = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("approval call for request %s failed: %s", request_id, call.detail)

        with ctx.db.session() as session:
            session.add(call)
            session.commit()
        row.call_status = call.status
        row.conversation_id = call.conversation_id
        self._save(row)
        await ctx.bus.publish(
            "call.started" if call.status == "started" else "call.ended",
            CallData(
                request_id=request_id,
                dry_run=call.dry_run,
                status=call.status,
                detail=call.detail,
            ),
        )
        return call

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient()
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    # ------------------------------------------------------------------ voice tools

    async def info(self, request_id: str) -> VoiceRequestInfo:
        row = self._require(request_id)
        return VoiceRequestInfo(
            amount_usd=from_micro(row.amount_micro),
            payee_label=row.payee_label,
            reason=Reason(row.reason),
            spent_today_usd=await self._ctx.vault.spent_today(),
            case_for=row.case_for,
            case_against=row.case_against,
            status=row.status,
            requires_passkey=row.passkey_required,
        )

    async def approve(
        self, request_id: str, amount_spoken: Decimal, challenge: str, via: str = "voice"
    ) -> OkResponse:
        row = self.get(request_id)
        if row is None:
            return OkResponse(ok=False, error="unknown_request")
        if row.status != "pending":
            return OkResponse(ok=False, status=row.status, error="already_resolved")
        if utcnow() > as_utc(row.expires_at):
            await self._expire(request_id)
            return OkResponse(ok=False, status="expired", error="expired")

        amount_ok = amount_spoken == from_micro(row.amount_micro)
        word_ok = hmac.compare_digest(normalise_word(challenge), normalise_word(row.challenge))
        if not (amount_ok and word_ok):
            row.attempts += 1
            self._save(row)
            if row.attempts >= MAX_ATTEMPTS:
                await self.deny(request_id, via="too_many_attempts")
                return OkResponse(ok=False, status="denied", error="too_many_attempts")
            return OkResponse(
                ok=False,
                status="pending",
                error="amount_mismatch" if not amount_ok else "challenge_mismatch",
            )

        row.voice_ok = True
        self._save(row)
        if row.passkey_required and not row.passkey_ok:
            return OkResponse(ok=True, status="awaiting_passkey")
        return await self._finalise(request_id, via)

    async def confirm_passkey(self, request_id: str) -> OkResponse:
        """Called by the passkey module after a verified assertion for this request."""
        row = self.get(request_id)
        if row is None:
            return OkResponse(ok=False, error="unknown_request")
        if row.status != "pending":
            return OkResponse(ok=False, status=row.status, error="already_resolved")
        row.passkey_ok = True
        self._save(row)
        if not row.voice_ok:
            return OkResponse(ok=True, status="awaiting_voice")
        return await self._finalise(request_id, "voice+passkey")

    async def _finalise(self, request_id: str, via: str) -> OkResponse:
        row = self._require(request_id)
        row.status = "approved"
        row.resolved_via = via
        self._save(row)
        try:
            permit_id = await self._ctx.vault.approve(request_id)
        except Exception as exc:
            log.exception("vault refused to approve request %s", request_id)
            row.status = "pending"
            row.resolved_via = ""
            self._save(row)
            return OkResponse(ok=False, status="pending", error=f"vault_error: {exc}"[:120])
        await self._ctx.planner.continue_after_approval(request_id, permit_id)
        return OkResponse(ok=True, status="approved")

    async def deny(self, request_id: str, via: str = "voice") -> OkResponse:
        row = self.get(request_id)
        if row is None:
            return OkResponse(ok=False, error="unknown_request")
        if row.status != "pending":
            return OkResponse(ok=False, status=row.status, error="already_resolved")
        row.status = "denied"
        row.resolved_via = via
        self._save(row)
        try:
            await self._ctx.vault.deny(request_id, via=via)
        except ValueError:
            log.info("request %s was already resolved in the vault", request_id)
        return OkResponse(ok=True, status="denied")

    async def freeze(self, via: str = "voice") -> OkResponse:
        await self._ctx.vault.freeze(True, via=via)
        return OkResponse(ok=True, status="frozen")

    # ------------------------------------------------------------------ expiry and no answer

    async def _expire_later(self, request_id: str, seconds: float) -> None:
        await asyncio.sleep(seconds)
        await self._expire(request_id)

    async def _expire(self, request_id: str) -> None:
        row = self.get(request_id)
        if row is None or row.status != "pending":
            return
        await self._no_decision(row, status="expired", via="expiry", call_status="no_answer")

    async def _no_decision(
        self, row: ApprovalRow, *, status: str, via: str, call_status: str
    ) -> None:
        """No answer means deny. Resolve the request, close the call record, tell the bots."""
        ctx = self._ctx
        row.status = status
        row.resolved_via = via
        row.call_status = call_status
        self._save(row)
        try:
            if via == "expiry":
                await ctx.vault.expire(row.request_id)
            else:
                await ctx.vault.deny(row.request_id, via=via)
        except ValueError:
            log.info("request %s was already resolved in the vault", row.request_id)
        await ctx.bus.publish(
            "call.ended",
            CallData(
                request_id=row.request_id,
                dry_run=not ctx.settings.calls_live,
                status=call_status,
                detail="No decision was given, so the request was denied.",
            ),
        )
        for hook in self.missed_call_hooks:
            try:
                await hook(row, call_status)
            except Exception:
                log.exception("missed-call hook failed for request %s", row.request_id)

    async def resume(self) -> None:
        """At start-up: requests left pending by a previous run are past saving; expire them."""
        with self._ctx.db.session() as session:
            pending = list(
                session.exec(select(ApprovalRow).where(ApprovalRow.status == "pending")).all()
            )
        for row in pending:
            await self._no_decision(row, status="expired", via="restart", call_status="no_answer")

    async def _on_resolved(self, event: StoredEvent) -> None:
        """Keep our record in step if the request was resolved onchain by someone else."""
        row = self.get(str(event.data.get("request_id")))
        if row is None or row.status != "pending":
            return
        row.status = "approved" if event.data.get("approved") else "denied"
        row.resolved_via = str(event.data.get("via") or "onchain")
        self._save(row)

    # ------------------------------------------------------------------ post-call webhook

    def verify_webhook(self, raw_body: bytes, signature_header: str | None) -> bool:
        """ElevenLabs signs webhooks as `t=<unix>,v0=<hex>` over `"<t>.<body>"` (HMAC-SHA256)."""
        secret = self._ctx.settings.elevenlabs_webhook_secret
        if not secret or not signature_header:
            return False
        parts = dict(part.split("=", 1) for part in signature_header.split(",") if "=" in part)
        timestamp, signature = parts.get("t", ""), parts.get("v0", "")
        if not timestamp.isdigit() or abs(time.time() - int(timestamp)) > WEBHOOK_TOLERANCE_S:
            return False
        expected = hmac.new(
            secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    async def handle_webhook(self, payload: dict[str, Any]) -> str:
        """`call_initiation_failure` (busy, no answer) and `post_call_transcription`."""
        kind = str(payload.get("type", ""))
        data = payload.get("data") or {}
        conversation_id = str(data.get("conversation_id") or "")
        row = self._row_for_conversation(conversation_id, data)
        if row is None:
            return "ignored: no matching request"
        if kind == "call_initiation_failure":
            if row.status == "pending":
                reason = str(data.get("failure_reason") or "unknown")
                await self._no_decision(
                    row, status="denied", via="no_answer", call_status=f"no_answer ({reason})"
                )
            return "denied: call was not answered"
        if kind == "post_call_transcription":
            if row.status == "pending" and not row.voice_ok:
                await self._no_decision(
                    row, status="denied", via="call_ended", call_status="ended_without_decision"
                )
                return "denied: call ended without a decision"
            row.call_status = "ended"
            self._save(row)
            await self._ctx.bus.publish(
                "call.ended",
                CallData(
                    request_id=row.request_id,
                    dry_run=False,
                    status="ended",
                    detail=f"Request is {row.status}.",
                ),
            )
            return "recorded"
        return "ignored: unhandled type"

    def _row_for_conversation(
        self, conversation_id: str, data: dict[str, Any]
    ) -> ApprovalRow | None:
        with self._ctx.db.session() as session:
            if conversation_id:
                row = session.exec(
                    select(ApprovalRow).where(ApprovalRow.conversation_id == conversation_id)
                ).first()
                if row is not None:
                    return row
        variables = (data.get("conversation_initiation_client_data") or {}).get(
            "dynamic_variables"
        ) or {}
        request_id = variables.get("request_id")
        return self.get(str(request_id)) if request_id else None

    # ------------------------------------------------------------------ dashboard views

    def _require(self, request_id: str) -> ApprovalRow:
        row = self.get(request_id)
        if row is None:
            raise KeyError(request_id)
        return row

    def views(self, limit: int = 20) -> list[ApprovalView]:
        with self._ctx.db.session() as session:
            rows = session.exec(
                select(ApprovalRow).order_by(col(ApprovalRow.created).desc()).limit(limit)
            ).all()
        return [
            ApprovalView(
                request_id=row.request_id,
                plan_id=f"p_{row.plan_id}" if row.plan_id else None,
                amount_usd=from_micro(row.amount_micro),
                payee_label=row.payee_label,
                reason=Reason(row.reason),
                status=row.status,
                challenge=row.challenge if row.status == "pending" else None,
                expires_at=as_utc(row.expires_at),
                requires_passkey=row.passkey_required,
                voice_ok=row.voice_ok,
                passkey_ok=row.passkey_ok,
                case_for=row.case_for,
                case_against=row.case_against,
                call_status=row.call_status,
                source=row.source,
            )
            for row in rows
        ]

    def stats(self) -> ApprovalStats:
        with self._ctx.db.session() as session:
            rows = session.exec(
                select(ApprovalRow.status, func.count())
                .where(ApprovalRow.status != "pending")
                .group_by(ApprovalRow.status)
            ).all()
        counts = {status: int(count) for status, count in rows}
        approved = counts.get("approved", 0)
        total = sum(counts.values())
        rate = (
            (Decimal(approved) / Decimal(total) * 100).quantize(Decimal("0.1")) if total else None
        )
        warning = ""
        if total >= RATE_WARNING_MIN_RESOLVED and approved == total:
            warning = (
                f"You have approved all {total} requests. Approving everything is the same as "
                "having no limit. Deny one, or tighten your policy."
            )
        return ApprovalStats(
            total=total, approved=approved, denied=total - approved, rate_pct=rate, warning=warning
        )


# --------------------------------------------------------------------------- routes

Ctx = Annotated[AppContext, Depends(get_ctx)]


def require_voice_token(ctx: Ctx, authorization: Annotated[str | None, Header()] = None) -> None:
    """Voice tools accept only `Authorization: Bearer VOICE_TOKEN`."""
    expected = ctx.settings.voice_token
    if not expected:
        raise HTTPException(status_code=503, detail="VOICE_TOKEN is not set on the server")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not authorization or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid voice token")


router = APIRouter(prefix="/api", tags=["voice tools"])
voice_auth = [Depends(require_voice_token)]


@router.get("/voice/request/{request_id}", response_model=VoiceRequestInfo, dependencies=voice_auth)
async def voice_request(request_id: str, ctx: Ctx) -> VoiceRequestInfo:
    """What the operator and the risk officer need to state the request and argue it."""
    try:
        return await ctx.approvals.info(request_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown request") from exc


@router.post("/voice/approve", response_model=OkResponse, dependencies=voice_auth)
async def voice_approve(body: VoiceApproveRequest, ctx: Ctx) -> OkResponse:
    """Approve only if the amount said back and the word from the owner's screen both match.
    A refusal is a normal response (`ok: false` with an `error`), so the agent can say why."""
    return await ctx.approvals.approve(body.request_id, body.amount_spoken, body.challenge)


@router.post("/voice/deny", response_model=OkResponse, dependencies=voice_auth)
async def voice_deny(body: VoiceDenyRequest, ctx: Ctx) -> OkResponse:
    return await ctx.approvals.deny(body.request_id)


@router.post("/voice/freeze", response_model=OkResponse, dependencies=voice_auth)
async def voice_freeze(body: VoiceFreezeRequest, ctx: Ctx) -> OkResponse:
    """The kill switch. Freezing needs no challenge: stopping all spending is always safe."""
    return await ctx.approvals.freeze()


@router.post("/voice/hooks/elevenlabs", response_model=OkResponse, tags=["webhooks"])
async def elevenlabs_webhook(
    request: Request,
    ctx: Ctx,
    elevenlabs_signature: Annotated[str | None, Header()] = None,
) -> OkResponse:
    """ElevenLabs post-call webhook. A call that was not answered denies the request."""
    raw = await request.body()
    if not ctx.settings.elevenlabs_webhook_secret:
        raise HTTPException(status_code=503, detail="ELEVENLABS_WEBHOOK_SECRET is not set")
    if not ctx.approvals.verify_webhook(raw, elevenlabs_signature):
        raise HTTPException(status_code=401, detail="bad signature")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="body is not JSON") from exc
    return OkResponse(ok=True, status=await ctx.approvals.handle_webhook(payload))


dashboard = APIRouter(prefix="/api", tags=["approvals"])


@dashboard.get("/approvals", response_model=ApprovalList)
async def approvals(ctx: Ctx) -> ApprovalList:
    """Recent approval requests for the owner's dashboard, including the challenge word for
    any that are pending."""
    return ApprovalList(approvals=ctx.approvals.views())


@dashboard.post("/approvals/{request_id}/simulate-voice", response_model=OkResponse)
async def simulate_voice(request_id: str, body: SimulatedVoiceRequest, ctx: Ctx) -> OkResponse:
    """Dry-run only: stand in for the phone call from the dashboard. Goes through exactly the
    same checks as the voice tool. Refused when real calls are enabled."""
    if ctx.settings.calls_live:
        raise HTTPException(
            status_code=403, detail="calls are live; approve on the phone, not here"
        )
    return await ctx.approvals.approve(
        request_id, body.amount_spoken, body.challenge, via="simulated_call"
    )


@dashboard.post("/approvals/{request_id}/deny", response_model=OkResponse)
async def dashboard_deny(request_id: str, ctx: Ctx) -> OkResponse:
    """Deny from the dashboard. Always allowed: saying no never needs proof."""
    return await ctx.approvals.deny(request_id, via="dashboard")
