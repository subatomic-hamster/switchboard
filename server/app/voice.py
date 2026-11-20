"""The approval call: outbound call, the four voice tools, and the rules for saying yes.

Flow. The vault emits `ApprovalRequested`; P1's Stream webhook publishes `approval.requested`
on the bus; this module reacts: it stores the request with a one-time challenge word, then
calls the owner through the provider named in CALL_PROVIDER (or logs the call, in dry-run).

- `agentphone`: `POST https://api.agentphone.ai/v1/calls` in webhook mode. AgentPhone posts
  each thing the owner says to `/api/voice/hooks/agentphone`, and this server decides what
  is said back, following the fixed script in `app/call_script.py`. No language model is on
  the call.
- `elevenlabs_twilio`: an ElevenLabs agent called through Twilio. The agent uses the four
  voice tools below; its prompt and tool definitions are in `app/agent_config.py`. The same
  agent can run in the ElevenLabs browser widget when no phone line is available.
- `dry_run` (the default): no call.

Every path ends in the same `approve`, `deny` and `freeze` methods, so the rules are the same.

What it takes to approve:

- the amount said back must equal the pending amount exactly;
- the one-time word shown on the owner's dashboard must be said. The word is never sent to the
  voice agent, so someone who only hears the call cannot learn it;
- above PASSKEY_THRESHOLD_USD, a passkey confirmation on the dashboard as well;
- three wrong attempts deny the request;
- no answer, or no decision before the expiry, denies the request.

Nothing here authenticates by voice. A cloned voice gains nothing: calls go only to
OWNER_PHONE_NUMBER, and approval needs the word from the owner's screen.

Dry-run. Unless CALLS_ENABLED=true, CALL_PROVIDER names a provider, and everything that
provider needs is set (`Settings.call_blockers` lists what is missing), no call is placed:
the request that would be sent is logged, and the dashboard offers a labelled "simulate the
call" form that goes through the same checks.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from decimal import Decimal
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlmodel import col, func, select

from app import call_script
from app.connectors import CONNECTOR_SOURCE
from app.context import AppContext, get_ctx
from app.db import (
    ApprovalRow,
    CallRow,
    PhoneSessionRow,
    PlanRow,
    as_utc,
    from_micro,
    to_micro,
    utcnow,
)
from app.events import StoredEvent
from app.planner import say_money, say_time
from app.schemas import (
    ApprovalList,
    ApprovalResolvedData,
    ApprovalStats,
    ApprovalView,
    ApprovalWidget,
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
# The AgentPhone key is sent to this host and to no other. It is a constant, not a setting,
# so no configuration can point the key somewhere else.
AGENTPHONE_API = "https://api.agentphone.ai"
AGENTPHONE_CALLS_PATH = "/v1/calls"
AGENTPHONE_TOLERANCE_S = 5 * 60
# A call that has not reached a decision after this many caller turns is denied.
MAX_PHONE_TURNS = 12
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
    Reason.UNUSUAL_SPEND: "it is unlike this agent's usual spending",
    Reason.OVER_AGENT_LIMIT: (
        "it came from a connected assistant and is more than that assistant may send "
        "without asking you"
    ),
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
            # A server-side hold may carry its own objection (the anomaly baseline's).
            case_against=ctx.holds.note(request_id)
            or self._case_against(
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
        elif plan.source.startswith(CONNECTOR_SOURCE):
            # Not the owner's sentence: an outside assistant asked. Say so, and by name.
            row.case_for, objection = self._ctx.connectors.cases(plan, row)
            if objection:
                row.case_against = objection
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

    def _elevenlabs_body(self, row: ApprovalRow, spent: Decimal) -> dict[str, Any]:
        settings = self._ctx.settings
        variables = {**self.call_variables(row), "spent_today_usd": f"{spent.normalize():f}"}
        return {
            "agent_id": settings.elevenlabs_agent_id,
            "agent_phone_number_id": settings.elevenlabs_phone_number_id,
            # Only ever the registered owner number; never a number from request data.
            "to_number": settings.owner_phone_number,
            "conversation_initiation_client_data": {"dynamic_variables": variables},
        }

    def _agentphone_body(self, row: ApprovalRow) -> dict[str, Any]:
        """Webhook mode: no `systemPrompt`, so every caller turn comes to this server. The
        greeting is the operator stating the request. The challenge word is not in it."""
        settings = self._ctx.settings
        reason = Reason(row.reason)
        return {
            "agentId": settings.agentphone_agent_id,
            # Only ever the registered owner number; never a number from request data.
            "toNumber": settings.owner_phone_number,
            "fromNumberId": settings.agentphone_number_id,
            "initialGreeting": call_script.greeting(
                from_micro(row.amount_micro),
                row.payee_label,
                REASON_PHRASES.get(reason, "the vault flagged it"),
            ),
        }

    async def _place_elevenlabs(self, body: dict[str, Any]) -> str:
        response = await self._client().post(
            f"{ELEVENLABS_API}{OUTBOUND_CALL_PATH}",
            headers={"xi-api-key": self._ctx.settings.elevenlabs_api_key},
            json=body,
            timeout=20.0,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success", False):
            raise RuntimeError(str(payload.get("message", "call was not started")))
        return str(payload.get("conversation_id") or "")

    async def _place_agentphone(self, request_id: str, body: dict[str, Any]) -> str:
        response = await self._client().post(
            f"{AGENTPHONE_API}{AGENTPHONE_CALLS_PATH}",
            headers={"Authorization": f"Bearer {self._ctx.settings.agentphone_api_key}"},
            json=body,
            timeout=20.0,
        )
        if response.is_error:
            # AgentPhone explains a refusal in the body (no payment method, number still
            # activating, a call already in progress). Keep that for the dashboard.
            raise RuntimeError(f"AgentPhone answered {response.status_code}: {response.text[:200]}")
        call_id = str(response.json().get("id") or "")
        if not call_id:
            raise RuntimeError("AgentPhone returned no call id")
        with self._ctx.db.session() as session:
            session.add(PhoneSessionRow(call_id=call_id, request_id=request_id, created=utcnow()))
            session.commit()
        return call_id

    async def start_approval_call(self, request_id: str) -> CallRow:
        """Call the owner about a pending request, through the provider in CALL_PROVIDER.
        Dry-run unless calls are fully enabled for that provider."""
        ctx, settings = self._ctx, self._ctx.settings
        row = self._require(request_id)
        provider = settings.call_provider
        if provider == "agentphone":
            body = self._agentphone_body(row)
        else:
            body = self._elevenlabs_body(row, await ctx.vault.spent_today())
        call = CallRow(
            request_id=request_id, ts=utcnow(), dry_run=not settings.calls_live, status=""
        )

        if not settings.calls_live:
            call.status = "started"
            call.detail = "dry run: no call placed"
            loggable = {
                key: value for key, value in body.items() if key not in ("to_number", "toNumber")
            }
            log.info(
                "DRY RUN approval call (provider %s; missing %s) for request %s to %s: %s",
                provider,
                ", ".join(settings.call_blockers) or "nothing",
                request_id,
                mask_number(settings.owner_phone_number) or "(no owner number set)",
                json.dumps(loggable),
            )
        else:
            try:
                if provider == "agentphone":
                    call.conversation_id = await self._place_agentphone(request_id, body)
                else:
                    call.conversation_id = await self._place_elevenlabs(body)
                call.status = "started"
                call.detail = f"placed through {provider}"
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
        if self._ctx.stream_stale:
            # Approving releases money, so it waits for the Stream like any other spend (see
            # chain/watchdog.py). The request stays pending; saying the word again after the
            # Stream recovers works, and if not it expires. Denying is never blocked.
            return OkResponse(ok=False, status="pending", error="stream_stale")
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
            if via == "restart":
                # The vault no longer knows this request (the stub's state does not survive a
                # restart), so nothing else will record the outcome. Keep the event log whole.
                await ctx.bus.publish(
                    "approval.resolved",
                    ApprovalResolvedData(request_id=row.request_id, approved=False, via=via),
                )
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

    # ------------------------------------------------------------------ AgentPhone webhook

    def verify_agentphone(
        self, raw_body: bytes, signature: str | None, timestamp: str | None
    ) -> bool:
        """`X-Webhook-Signature: sha256=<hex>` is HMAC-SHA256 of `"<timestamp>." + body` with
        the webhook secret. A timestamp more than five minutes away is refused, so a recorded
        delivery cannot be replayed later."""
        secret = self._ctx.settings.agentphone_webhook_secret
        if not secret or not signature or not timestamp:
            return False
        if not (timestamp.isascii() and timestamp.isdigit() and len(timestamp) <= 12):
            return False
        if abs(time.time() - int(timestamp)) > AGENTPHONE_TOLERANCE_S:
            return False
        expected = hmac.new(
            secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(f"sha256={expected}".encode(), signature.strip().encode())

    def _phone_session(self, data: dict[str, Any]) -> PhoneSessionRow | None:
        """The session for a call this server placed. Anything else gets no session, so an
        inbound call to the number, or a call placed by someone else, can approve nothing."""
        call_id = str(data.get("callId") or "")
        if not call_id:
            return None
        settings = self._ctx.settings
        with self._ctx.db.session() as session:
            found = session.get(PhoneSessionRow, call_id)
            if found is not None:
                return found
            # AgentPhone's documents do not promise that the id in a webhook equals the id
            # returned when the call was placed. If it differs, adopt the call only when it
            # is outbound, to the owner's number, and exactly one placed call is waiting.
            if not (
                str(data.get("direction") or "") == "outbound"
                and settings.owner_phone_number
                and str(data.get("to") or "") == settings.owner_phone_number
            ):
                return None
            cutoff = utcnow() - timedelta(seconds=settings.approval_expiry_s)
            waiting: list[PhoneSessionRow] = []
            for candidate in session.exec(
                select(PhoneSessionRow).where(
                    col(PhoneSessionRow.created) >= cutoff, col(PhoneSessionRow.turns) == 0
                )
            ).all():
                approval = session.get(ApprovalRow, candidate.request_id)
                if approval is not None and approval.status == "pending":
                    waiting.append(candidate)
            if len(waiting) != 1:
                return None
            log.warning(
                "AgentPhone call id %s was not the id returned at placement; matched it to "
                "request %s by direction and number",
                call_id,
                waiting[0].request_id,
            )
            # Move the session to the id the webhook uses, so only one call can hold it.
            adopted = PhoneSessionRow(
                call_id=call_id, request_id=waiting[0].request_id, created=utcnow()
            )
            session.delete(waiting[0])
            session.add(adopted)
            session.commit()
            return adopted

    def _save_session(self, phone: PhoneSessionRow) -> None:
        with self._ctx.db.session() as session:
            session.add(phone)
            session.commit()

    async def phone_turn(self, data: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """One thing the owner said on an AgentPhone call. Yields what to say back, as the
        chunks of an NDJSON reply: `interim` chunks first, then one final chunk.

        The script is fixed: the greeting stated the request; the first reply is where it
        came from, the risk officer's objection, and the ask; after that the amount and the
        word are collected and handed to `approve`, which is the only thing that can say yes."""
        phone = self._phone_session(data)
        if phone is None:
            yield {"text": call_script.NOT_OUR_CALL, "hangup": True}
            return
        row = self._require(phone.request_id)
        request_id = row.request_id
        amount = from_micro(row.amount_micro)
        reason_phrase = REASON_PHRASES.get(Reason(row.reason), "the vault flagged it")
        phone.turns += 1
        self._save_session(phone)

        def closing(text: str) -> dict[str, Any]:
            phone.state = "done"
            self._save_session(phone)
            return {"text": text, "hangup": True}

        if row.status != "pending":
            yield closing(call_script.already_resolved(row.status))
            return
        if row.voice_ok:
            yield closing(call_script.AWAITING_PASSKEY)
            return
        if utcnow() > as_utc(row.expires_at):
            await self._expire(request_id)
            yield closing(call_script.EXPIRED)
            return
        if phone.turns > MAX_PHONE_TURNS:
            await self.deny(request_id, via="call_too_long")
            yield closing(call_script.TOO_LONG)
            return

        heard = call_script.hear(str(data.get("transcript") or ""), CHALLENGE_WORDS)
        if heard.intent == "freeze":
            yield {"text": call_script.CHECKING, "interim": True}
            await self.freeze(via="voice")
            await self.deny(request_id, via="voice")
            yield closing(call_script.FROZEN)
            return
        if heard.intent == "deny":
            await self.deny(request_id, via="voice")
            yield closing(call_script.DENIED)
            return

        if phone.state == "stated":
            # Whatever was said, the objection comes before anything can be approved.
            phone.state = "asking"
            phone.amount_heard = phone.word_heard = ""
            self._save_session(phone)
            yield {"text": call_script.case_for(row.case_for), "interim": True}
            yield {"text": call_script.objection(row.case_against), "interim": True}
            yield {"text": call_script.ask(row.passkey_required)}
            return

        if heard.intent == "repeat":
            yield {
                "text": call_script.restatement(amount, row.payee_label, reason_phrase),
                "interim": True,
            }
            yield {"text": call_script.objection(row.case_against), "interim": True}
            yield {"text": call_script.ask(row.passkey_required)}
            return

        if len(heard.words) > 1:
            # Listing several words in one breath is a guess at the word, not the word.
            phone.amount_heard = phone.word_heard = ""
            self._save_session(phone)
            result = await self.approve(request_id, amount, "", via="voice")
            if result.error == "too_many_attempts":
                yield closing(call_script.TOO_MANY_ATTEMPTS)
                return
            tries_left = MAX_ATTEMPTS - self._require(request_id).attempts
            yield {"text": call_script.TOO_MANY_WORDS, "interim": True}
            yield {"text": call_script.mismatch("challenge_mismatch", tries_left)}
            return

        if heard.amount is not None:
            phone.amount_heard = f"{heard.amount:f}"
        if heard.words:
            phone.word_heard = heard.words[0]
        self._save_session(phone)
        if not (phone.amount_heard and phone.word_heard):
            if heard.amount_unclear:
                yield {"text": call_script.AMOUNT_UNCLEAR}
            elif phone.amount_heard:
                yield {"text": call_script.need_word(Decimal(phone.amount_heard))}
            elif phone.word_heard:
                yield {"text": call_script.NEED_AMOUNT}
            else:
                yield {"text": call_script.NEED_BOTH}
            return

        said_amount, said_word = Decimal(phone.amount_heard), phone.word_heard
        phone.amount_heard = phone.word_heard = ""
        self._save_session(phone)
        yield {"text": call_script.CHECKING, "interim": True}
        result = await self.approve(request_id, said_amount, said_word, via="voice")
        if result.ok and result.status == "approved":
            yield closing(call_script.approved(amount, row.payee_label))
        elif result.ok and result.status == "awaiting_passkey":
            yield closing(call_script.AWAITING_PASSKEY)
        elif result.error in ("amount_mismatch", "challenge_mismatch"):
            tries_left = MAX_ATTEMPTS - self._require(request_id).attempts
            yield {"text": call_script.mismatch(result.error, tries_left)}
        elif result.error == "too_many_attempts":
            yield closing(call_script.TOO_MANY_ATTEMPTS)
        elif result.error == "expired":
            yield closing(call_script.EXPIRED)
        elif result.error == "already_resolved":
            yield closing(call_script.already_resolved(result.status or "resolved"))
        else:
            yield closing(call_script.NOT_RECORDED)

    async def phone_call_ended(self, data: dict[str, Any]) -> str:
        """`agent.call_ended`. A call that ends without an approval denies the request."""
        phone = self._phone_session(data)
        if phone is None:
            return "ignored: not a call this server placed"
        row = self.get(phone.request_id)
        if row is None:
            return "ignored: no matching request"
        answered = phone.turns > 0
        phone.state = "done"
        self._save_session(phone)
        if row.status == "pending" and not row.voice_ok:
            if answered:
                await self._no_decision(
                    row, status="denied", via="call_ended", call_status="ended_without_decision"
                )
                return "denied: call ended without a decision"
            reason = str(data.get("disconnectionReason") or data.get("status") or "unknown")[:60]
            await self._no_decision(
                row, status="denied", via="no_answer", call_status=f"no_answer ({reason})"
            )
            return "denied: call was not answered"
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


async def _ndjson(chunks: AsyncIterator[dict[str, Any]]) -> AsyncIterator[bytes]:
    """One JSON object per line. If the script fails part-way, say so and hang up: a caller
    left in silence might assume the request went through."""
    try:
        async for chunk in chunks:
            yield json.dumps(chunk).encode() + b"\n"
    except Exception:
        log.exception("the approval call script failed")
        yield json.dumps({"text": call_script.FAILED, "hangup": True}).encode() + b"\n"


@router.post("/voice/hooks/agentphone", tags=["webhooks"], response_model=None)
async def agentphone_webhook(
    request: Request,
    ctx: Ctx,
    x_webhook_signature: Annotated[str | None, Header()] = None,
    x_webhook_timestamp: Annotated[str | None, Header()] = None,
) -> Response:
    """AgentPhone webhook. A voice turn (`agent.message` on the `voice` channel) is answered
    with what to say next, as NDJSON. `agent.call_ended` closes the call: ended without an
    approval means denied. Every delivery must carry a valid signature."""
    raw = await request.body()
    if not ctx.settings.agentphone_webhook_secret:
        raise HTTPException(status_code=503, detail="AGENTPHONE_WEBHOOK_SECRET is not set")
    if not ctx.approvals.verify_agentphone(raw, x_webhook_signature, x_webhook_timestamp):
        raise HTTPException(status_code=401, detail="bad signature")
    try:
        event = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="body is not JSON") from exc
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="body is not a JSON object")
    kind, channel = event.get("event"), event.get("channel")
    data = event.get("data")
    if not isinstance(data, dict):
        data = {}
    expected_agent = ctx.settings.agentphone_agent_id
    if event.get("test") or (expected_agent and event.get("agentId") != expected_agent):
        return JSONResponse({"ok": True, "status": "ignored"})
    if kind == "agent.message" and channel == "voice":
        return StreamingResponse(
            _ndjson(ctx.approvals.phone_turn(data)), media_type="application/x-ndjson"
        )
    if kind == "agent.call_ended":
        return JSONResponse({"ok": True, "status": await ctx.approvals.phone_call_ended(data)})
    return JSONResponse({"ok": True, "status": "ignored"})


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


@dashboard.get("/approvals/{request_id}/widget", response_model=ApprovalWidget)
async def approval_widget(request_id: str, ctx: Ctx) -> ApprovalWidget:
    """The fallback when no phone line is available: the same ElevenLabs agent, run in its
    browser widget on the dashboard. Returns the agent id and the per-call variables the
    phone call would have carried. 404 until ELEVENLABS_AGENT_ID is set."""
    if not ctx.settings.elevenlabs_agent_id:
        raise HTTPException(status_code=404, detail="ELEVENLABS_AGENT_ID is not set")
    row = ctx.approvals.get(request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown request")
    spent = await ctx.vault.spent_today()
    variables = {**ctx.approvals.call_variables(row), "spent_today_usd": f"{spent.normalize():f}"}
    return ApprovalWidget(
        agent_id=ctx.settings.elevenlabs_agent_id,
        dynamic_variables={key: str(value) for key, value in variables.items()},
    )


@dashboard.post("/approvals/{request_id}/deny", response_model=OkResponse)
async def dashboard_deny(request_id: str, ctx: Ctx) -> OkResponse:
    """Deny from the dashboard. Always allowed: saying no never needs proof."""
    return await ctx.approvals.deny(request_id, via="dashboard")
