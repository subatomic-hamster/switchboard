"""The approval call: voice tools, the challenge word, expiry, the call cap, the outbound call."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
import respx
from fastapi import FastAPI

from app.agent_config import agent_payload, market_tool_payloads, system_prompt, tool_payloads
from app.context import AppContext
from app.db import Database
from app.main import create_app
from app.voice import CHALLENGE_WORDS, ELEVENLABS_API, OUTBOUND_CALL_PATH, mask_number
from tests.conftest import VOICE_TOKEN, make_settings

OVER_CAP = "send 250 from my arc wallet to sam's arc wallet"
NEW_ADDRESS = "0x9c1e8a4f2b7d6c5a3e1f0b9d8c7a6e5f4d3c4f2c"


async def _pending(client: httpx.AsyncClient, text: str = OVER_CAP) -> str:
    plan = (await client.post("/api/intent", json={"text": text})).json()
    body = (await client.post(f"/api/plans/{plan['plan_id']}/execute", json={})).json()
    assert body["status"] == "needs_approval", body
    return str(body["request_id"])


def _challenge(ctx: AppContext, request_id: str) -> str:
    row = ctx.approvals.get(request_id)
    assert row is not None
    return row.challenge


def _types(ctx: AppContext) -> list[str]:
    return [event.type for event in ctx.bus.history()]


# --------------------------------------------------------------------------- auth


async def test_voice_tools_need_the_bearer_token(client: httpx.AsyncClient) -> None:
    request_id = await _pending(client)
    assert (await client.get(f"/api/voice/request/{request_id}")).status_code == 401
    wrong = {"Authorization": "Bearer nope"}
    assert (await client.get(f"/api/voice/request/{request_id}", headers=wrong)).status_code == 401
    for path in ("/api/voice/approve", "/api/voice/deny", "/api/voice/freeze"):
        assert (await client.post(path, json={})).status_code == 401


async def test_voice_tools_are_off_when_no_token_is_configured() -> None:
    application = create_app(make_settings(voice_token=""), Database(None))
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        response = await http.post(
            "/api/voice/freeze", json={}, headers={"Authorization": "Bearer "}
        )
    assert response.status_code == 503
    await application.state.ctx.aclose()


# --------------------------------------------------------------------------- the request


async def test_request_details_match_the_frozen_shape(
    client: httpx.AsyncClient, ctx: AppContext, voice_headers: dict[str, str]
) -> None:
    request_id = await _pending(client)
    response = await client.get(f"/api/voice/request/{request_id}", headers=voice_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["amount_usd"] == 250
    assert body["payee_label"] == "Sam's Arc wallet"
    assert body["reason"] == "OVER_TRANSFER_CAP"
    assert body["spent_today_usd"] == 0
    assert body["case_for"].startswith('You asked for this yourself: "send 250')
    assert "50 dollars over your 200 dollars transfer limit" in body["case_against"]
    # The challenge word is never given to the voice agent.
    assert _challenge(ctx, request_id) not in json.dumps(body)
    assert "challenge" not in body

    missing = await client.get("/api/voice/request/999", headers=voice_headers)
    assert missing.status_code == 404


async def test_new_payee_objection_names_the_address(
    client: httpx.AsyncClient, voice_headers: dict[str, str]
) -> None:
    request_id = await _pending(client, f"send 20 from my arc wallet to {NEW_ADDRESS}")
    body = (await client.get(f"/api/voice/request/{request_id}", headers=voice_headers)).json()
    assert body["payee_label"] == "new address ending 4f2c"
    assert body["reason"] == "PAYEE_NOT_ALLOWED"
    assert "never paid new address ending 4f2c before" in body["case_against"]


async def test_dry_run_call_is_recorded_and_no_request_leaves(
    client: httpx.AsyncClient, ctx: AppContext
) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.post(f"{ELEVENLABS_API}{OUTBOUND_CALL_PATH}")
        request_id = await _pending(client)
        assert route.call_count == 0
    started = ctx.bus.latest("call.started")
    assert started is not None
    assert started.data == {
        "request_id": request_id,
        "dry_run": True,
        "status": "started",
        "detail": "dry run: no call placed",
    }
    assert _types(ctx) == ["approval.requested", "call.started"]


# --------------------------------------------------------------------------- approving


async def test_approval_needs_the_amount_and_the_word(
    client: httpx.AsyncClient, ctx: AppContext, voice_headers: dict[str, str]
) -> None:
    request_id = await _pending(client)
    word = _challenge(ctx, request_id)
    wrong_word = next(w for w in CHALLENGE_WORDS if w != word)

    async def approve(amount: object, challenge: str) -> dict[str, object]:
        response = await client.post(
            "/api/voice/approve",
            json={"request_id": request_id, "amount_spoken": amount, "challenge": challenge},
            headers=voice_headers,
        )
        assert response.status_code == 200
        return dict(response.json())

    assert await approve(200, word) == {
        "ok": False,
        "status": "pending",
        "error": "amount_mismatch",
    }
    assert await approve(250, wrong_word) == {
        "ok": False,
        "status": "pending",
        "error": "challenge_mismatch",
    }
    assert await ctx.vault.spent_today() == Decimal("0")

    assert await approve(250, f" {word.upper()}. ") == {
        "ok": True,
        "status": "approved",
        "error": "",
    }
    await asyncio.sleep(0.2)
    assert _types(ctx) == [
        "approval.requested",
        "call.started",
        "approval.resolved",
        "transfer.sent",
        "transfer.arrived",
    ]
    assert await ctx.vault.spent_today() == Decimal("250")

    assert (await approve(250, word))["error"] == "already_resolved"


async def test_three_wrong_attempts_deny_the_request(
    client: httpx.AsyncClient, ctx: AppContext, voice_headers: dict[str, str]
) -> None:
    request_id = await _pending(client)
    word = _challenge(ctx, request_id)
    body: dict[str, object] = {}
    for _ in range(3):
        body = (
            await client.post(
                "/api/voice/approve",
                json={"request_id": request_id, "amount_spoken": 1, "challenge": "wrong"},
                headers=voice_headers,
            )
        ).json()
    assert body == {"ok": False, "status": "denied", "error": "too_many_attempts"}
    resolved = ctx.bus.latest("approval.resolved")
    assert resolved is not None and resolved.data["approved"] is False
    late = await client.post(
        "/api/voice/approve",
        json={"request_id": request_id, "amount_spoken": 250, "challenge": word},
        headers=voice_headers,
    )
    assert late.json()["error"] == "already_resolved"
    assert await ctx.vault.spent_today() == Decimal("0")


async def test_deny_resolves_the_request_and_sends_nothing(
    client: httpx.AsyncClient, ctx: AppContext, voice_headers: dict[str, str]
) -> None:
    request_id = await _pending(client)
    response = await client.post(
        "/api/voice/deny", json={"request_id": request_id}, headers=voice_headers
    )
    assert response.json() == {"ok": True, "status": "denied", "error": ""}
    resolved = ctx.bus.latest("approval.resolved")
    assert resolved is not None
    assert resolved.data == {"request_id": request_id, "approved": False, "via": "voice"}
    assert "transfer.sent" not in _types(ctx)
    plan = ctx.planner.plan_for_request(request_id)
    assert plan is not None and plan.status == "denied"


async def test_freeze_by_voice_blocks_the_next_transfer(
    client: httpx.AsyncClient, ctx: AppContext, voice_headers: dict[str, str]
) -> None:
    response = await client.post(
        "/api/voice/freeze", json={"source": "phone"}, headers=voice_headers
    )
    assert response.json()["status"] == "frozen"
    frozen = ctx.bus.latest("frozen")
    assert frozen is not None and frozen.data == {"frozen": True, "via": "voice"}
    plan = (await client.post("/api/intent", json={"text": "send 20 to sam"})).json()
    body = (await client.post(f"/api/plans/{plan['plan_id']}/execute", json={})).json()
    assert body["status"] == "blocked" and body["reason"] == "FROZEN"


# --------------------------------------------------------------------------- no answer, caps


async def test_no_answer_means_deny() -> None:
    application = create_app(make_settings(approval_expiry_s=0), Database(None))
    ctx: AppContext = application.state.ctx
    missed: list[tuple[str, str]] = []

    async def hook(row: object, status: str) -> None:
        missed.append((getattr(row, "request_id", ""), status))

    ctx.approvals.missed_call_hooks.append(hook)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        request_id = await _pending(http)
        await asyncio.sleep(0.2)
        row = ctx.approvals.get(request_id)
        assert row is not None
        assert (row.status, row.resolved_via, row.call_status) == ("expired", "expiry", "no_answer")
        resolved = ctx.bus.latest("approval.resolved")
        assert resolved is not None
        assert resolved.data == {"request_id": request_id, "approved": False, "via": "expiry"}
        ended = ctx.bus.latest("call.ended")
        assert ended is not None and ended.data["status"] == "no_answer"
        assert missed == [(request_id, "no_answer")]
        late = await http.post(
            "/api/voice/approve",
            json={"request_id": request_id, "amount_spoken": 250, "challenge": row.challenge},
            headers={"Authorization": f"Bearer {VOICE_TOKEN}"},
        )
        assert late.json()["error"] == "already_resolved"
    await ctx.aclose()


async def test_daily_call_cap_denies_without_calling() -> None:
    application = create_app(make_settings(max_calls_per_day=1), Database(None))
    ctx: AppContext = application.state.ctx
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        first = await _pending(http)
        second = await _pending(http)
    first_row, second_row = ctx.approvals.get(first), ctx.approvals.get(second)
    assert first_row is not None and second_row is not None
    assert first_row.status == "pending"
    assert (second_row.status, second_row.resolved_via) == ("denied", "call_cap")
    blocked = ctx.bus.latest("blocked")
    assert blocked is not None and blocked.data["reason"] == "CALL_CAP_REACHED"
    assert ctx.approvals.calls_in_last_day() == 1
    await ctx.aclose()


async def test_approval_rate_warns_at_one_hundred_percent(
    client: httpx.AsyncClient, ctx: AppContext, voice_headers: dict[str, str]
) -> None:
    await ctx.vault.set_policy(
        (await ctx.vault.get_policy()).model_copy(update={"daily_budget_usd": Decimal("5000")})
    )
    for index in range(3):
        request_id = await _pending(client)
        await client.post(
            "/api/voice/approve",
            json={
                "request_id": request_id,
                "amount_spoken": 250,
                "challenge": _challenge(ctx, request_id),
            },
            headers=voice_headers,
        )
        stats = (await client.get("/api/stats")).json()
        assert stats["approvals"]["approved"] == index + 1
        assert bool(stats["approvals"]["warning"]) is (index == 2)
    assert stats["approvals"]["rate_pct"] == 100
    assert "approved all 3" in stats["approvals"]["warning"]

    request_id = await _pending(client)
    await client.post("/api/voice/deny", json={"request_id": request_id}, headers=voice_headers)
    stats = (await client.get("/api/stats")).json()
    assert stats["approvals"] == {
        "total": 4,
        "approved": 3,
        "denied": 1,
        "rate_pct": 75,
        "warning": "",
    }
    assert stats["calls_today"] == 4
    assert stats["budget_remaining_usd"] == 4250


# --------------------------------------------------------------------------- dashboard


async def test_dashboard_sees_the_word_only_while_pending(
    client: httpx.AsyncClient, ctx: AppContext
) -> None:
    request_id = await _pending(client)
    view = (await client.get("/api/approvals")).json()["approvals"][0]
    assert view["request_id"] == request_id
    assert view["plan_id"] == "p_1"
    assert view["challenge"] == _challenge(ctx, request_id)
    assert view["status"] == "pending"

    response = await client.post(
        f"/api/approvals/{request_id}/simulate-voice",
        json={"amount_spoken": 250, "challenge": view["challenge"]},
    )
    assert response.json() == {"ok": True, "status": "approved", "error": ""}
    view = (await client.get("/api/approvals")).json()["approvals"][0]
    assert view["status"] == "approved"
    assert view["challenge"] is None
    row = ctx.approvals.get(request_id)
    assert row is not None and row.resolved_via == "simulated_call"


async def test_dashboard_can_always_deny(client: httpx.AsyncClient, ctx: AppContext) -> None:
    request_id = await _pending(client)
    response = await client.post(f"/api/approvals/{request_id}/deny")
    assert response.json()["status"] == "denied"
    row = ctx.approvals.get(request_id)
    assert row is not None and row.resolved_via == "dashboard"


# --------------------------------------------------------------------------- live call path

LIVE = {
    "calls_enabled": True,
    "call_provider": "elevenlabs_twilio",
    "elevenlabs_api_key": "xi-test-key",
    "elevenlabs_agent_id": "agent_test",
    "elevenlabs_phone_number_id": "phnum_test",
    "owner_phone_number": "+16045550123",
    "elevenlabs_webhook_secret": "whsec-test",
}


@pytest.fixture
async def live_app() -> AsyncIterator[FastAPI]:
    application = create_app(make_settings(**LIVE), Database(None))
    yield application
    await application.state.ctx.aclose()


@pytest.fixture
async def live_client(live_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=live_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        yield http


@respx.mock
async def test_live_call_posts_to_elevenlabs_with_dynamic_variables(
    live_app: FastAPI, live_client: httpx.AsyncClient
) -> None:
    ctx: AppContext = live_app.state.ctx
    route = respx.post(f"{ELEVENLABS_API}{OUTBOUND_CALL_PATH}").mock(
        return_value=httpx.Response(
            200,
            json={"success": True, "message": "ok", "conversation_id": "conv_1", "callSid": "CA1"},
        )
    )
    request_id = await _pending(live_client)
    assert route.call_count == 1
    sent = route.calls.last.request
    assert sent.headers["xi-api-key"] == "xi-test-key"
    body = json.loads(sent.content)
    assert body["agent_id"] == "agent_test"
    assert body["agent_phone_number_id"] == "phnum_test"
    assert body["to_number"] == "+16045550123"
    variables = body["conversation_initiation_client_data"]["dynamic_variables"]
    assert variables["request_id"] == request_id
    assert variables["amount_usd"] == "250"
    assert variables["amount_words"] == "250 dollars"
    assert variables["payee_label"] == "Sam's Arc wallet"
    assert variables["reason_code"] == "OVER_TRANSFER_CAP"
    assert variables["spent_today_usd"] == "0"
    # The word on the owner's screen must never reach the agent.
    assert _challenge(ctx, request_id) not in sent.content.decode()

    started = ctx.bus.latest("call.started")
    assert started is not None and started.data["dry_run"] is False
    row = ctx.approvals.get(request_id)
    assert row is not None and row.conversation_id == "conv_1"

    # With live calls, the dashboard cannot stand in for the phone.
    simulated = await live_client.post(
        f"/api/approvals/{request_id}/simulate-voice",
        json={"amount_spoken": 250, "challenge": row.challenge},
    )
    assert simulated.status_code == 403


@respx.mock
async def test_failed_call_is_recorded_and_the_request_stays_pending(
    live_app: FastAPI, live_client: httpx.AsyncClient
) -> None:
    ctx: AppContext = live_app.state.ctx
    respx.post(f"{ELEVENLABS_API}{OUTBOUND_CALL_PATH}").mock(
        return_value=httpx.Response(402, json={"detail": "plan does not allow telephony"})
    )
    request_id = await _pending(live_client)
    ended = ctx.bus.latest("call.ended")
    assert ended is not None and ended.data["status"] == "failed"
    row = ctx.approvals.get(request_id)
    assert row is not None and row.status == "pending" and row.call_status == "failed"


def _signed(secret: str, payload: dict[str, object], age_s: int = 0) -> tuple[bytes, str]:
    raw = json.dumps(payload).encode()
    timestamp = str(int(time.time()) - age_s)
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw, hashlib.sha256).hexdigest()
    return raw, f"t={timestamp},v0={digest}"


@respx.mock
async def test_unanswered_call_webhook_denies_the_request(
    live_app: FastAPI, live_client: httpx.AsyncClient
) -> None:
    ctx: AppContext = live_app.state.ctx
    respx.post(f"{ELEVENLABS_API}{OUTBOUND_CALL_PATH}").mock(
        return_value=httpx.Response(200, json={"success": True, "conversation_id": "conv_9"})
    )
    request_id = await _pending(live_client)
    payload = {
        "type": "call_initiation_failure",
        "event_timestamp": int(time.time()),
        "data": {"conversation_id": "conv_9", "failure_reason": "no-answer"},
    }
    raw, signature = _signed("whsec-test", payload)

    unsigned = await live_client.post("/api/voice/hooks/elevenlabs", content=raw)
    assert unsigned.status_code == 401
    forged = await live_client.post(
        "/api/voice/hooks/elevenlabs",
        content=raw,
        headers={"ElevenLabs-Signature": _signed("other-secret", payload)[1]},
    )
    assert forged.status_code == 401
    stale = await live_client.post(
        "/api/voice/hooks/elevenlabs",
        content=raw,
        headers={"ElevenLabs-Signature": _signed("whsec-test", payload, age_s=3600)[1]},
    )
    assert stale.status_code == 401
    row = ctx.approvals.get(request_id)
    assert row is not None and row.status == "pending"

    accepted = await live_client.post(
        "/api/voice/hooks/elevenlabs", content=raw, headers={"ElevenLabs-Signature": signature}
    )
    assert accepted.status_code == 200
    row = ctx.approvals.get(request_id)
    assert row is not None
    assert (row.status, row.resolved_via) == ("denied", "no_answer")
    assert row.call_status == "no_answer (no-answer)"


@respx.mock
async def test_call_that_ends_without_a_decision_denies(
    live_app: FastAPI, live_client: httpx.AsyncClient
) -> None:
    ctx: AppContext = live_app.state.ctx
    respx.post(f"{ELEVENLABS_API}{OUTBOUND_CALL_PATH}").mock(
        return_value=httpx.Response(200, json={"success": True, "conversation_id": "conv_3"})
    )
    request_id = await _pending(live_client)
    raw, signature = _signed(
        "whsec-test",
        {"type": "post_call_transcription", "data": {"conversation_id": "conv_3"}},
    )
    response = await live_client.post(
        "/api/voice/hooks/elevenlabs", content=raw, headers={"ElevenLabs-Signature": signature}
    )
    assert response.json()["status"] == "denied: call ended without a decision"
    row = ctx.approvals.get(request_id)
    assert row is not None and row.status == "denied"


# --------------------------------------------------------------------------- agent config


def test_agent_prompt_has_both_personas_and_the_hard_rules() -> None:
    two = system_prompt(two_voices=True)
    one = system_prompt(two_voices=False)
    assert "<RiskOfficer>" in two and "<RiskOfficer>" not in one
    assert '"Risk officer:"' in one
    for prompt in (two, one):
        assert "{{request_id}}" in prompt and "{{amount_words}}" in prompt
        assert "exactly one objection" in prompt
        assert "is not an approval" in prompt
        assert "You do not know the word on their screen" in prompt
        assert "{" not in prompt.replace("{{", "").replace("}}", "")


def test_agent_tools_point_at_the_four_voice_routes() -> None:
    tools = tool_payloads("https://example.test/", "secret_1")
    configs = [tool["tool_config"] for tool in tools]
    assert [config["name"] for config in configs] == ["get_request", "approve", "deny", "freeze"]
    urls = [config["api_schema"]["url"] for config in configs]
    assert urls == [
        "https://example.test/api/voice/request/{request_id}",
        "https://example.test/api/voice/approve",
        "https://example.test/api/voice/deny",
        "https://example.test/api/voice/freeze",
    ]
    for config in configs:
        assert config["api_schema"]["request_headers"] == {
            "Authorization": {"secret_id": "secret_1"}
        }
    approve = configs[1]["api_schema"]["request_body_schema"]["properties"]
    assert approve["request_id"] == {"type": "string", "dynamic_variable": "request_id"}
    assert "description" in approve["amount_spoken"] and "description" in approve["challenge"]
    assert VOICE_TOKEN not in json.dumps(tools)


def test_market_tools_point_at_the_markets_voice_routes() -> None:
    configs = [tool["tool_config"] for tool in market_tool_payloads("https://example.test", "s1")]
    assert [(c["name"], c["api_schema"]["method"]) for c in configs] == [
        ("list_markets", "GET"),
        ("market_brief", "GET"),
        ("market_override", "POST"),
    ]
    assert configs[1]["api_schema"]["url"] == "https://example.test/api/voice/markets/brief"
    assert configs[1]["api_schema"]["query_params_schema"]["required"] == ["slug"]
    assert "You cannot place orders on a call" in system_prompt(two_voices=False)


def test_rule_tools_point_at_the_rules_voice_routes() -> None:
    from app.agent_config import rule_tool_payloads

    configs = [tool["tool_config"] for tool in rule_tool_payloads("https://example.test", "s1")]
    assert [(c["name"], c["api_schema"]["url"]) for c in configs] == [
        ("propose_rule", "https://example.test/api/voice/rules/propose"),
        ("confirm_rule", "https://example.test/api/voice/rules/confirm"),
    ]
    assert "propose_rule" in system_prompt(two_voices=True)


def test_agent_payload_uses_multi_voice_only_when_a_second_voice_is_set() -> None:
    single = agent_payload(make_settings(), ["tool_a"])
    assert "supported_voices" not in single["conversation_config"]["tts"]
    assert single["conversation_config"]["agent"]["prompt"]["tool_ids"] == ["tool_a"]

    double = agent_payload(
        make_settings(elevenlabs_operator_voice_id="v1", elevenlabs_risk_officer_voice_id="v2"),
        ["tool_a"],
    )
    tts = double["conversation_config"]["tts"]
    assert tts["voice_id"] == "v1"
    assert tts["supported_voices"][0]["label"] == "RiskOfficer"
    assert tts["supported_voices"][0]["voice_id"] == "v2"


def test_mask_number_hides_the_middle() -> None:
    assert mask_number("+16045550123") == "+1******0123"
    assert mask_number("") == ""
