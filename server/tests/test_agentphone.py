"""The approval call through AgentPhone: placement, the signed webhook, and the call script.

No test reaches the network: respx stands in for api.agentphone.ai, and any other outbound
request fails the test.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from pydantic import ValidationError

from app import call_script
from app.context import AppContext
from app.db import Database, PhoneSessionRow
from app.main import create_app
from app.voice import (
    AGENTPHONE_API,
    AGENTPHONE_CALLS_PATH,
    CHALLENGE_WORDS,
    ELEVENLABS_API,
    MAX_PHONE_TURNS,
    OUTBOUND_CALL_PATH,
)
from tests.conftest import make_settings

OVER_CAP = "send 250 from my arc wallet to sam's arc wallet"
OVER_PASSKEY = "send 1500 from my arc wallet to sam's arc wallet"
OWNER = "+16045550123"
SECRET = "whsec_agentphone_test"
API_KEY = "sk_test_agentphone_key"
HOOK = "/api/voice/hooks/agentphone"
CALLS_URL = f"{AGENTPHONE_API}{AGENTPHONE_CALLS_PATH}"

LIVE: dict[str, object] = {
    "calls_enabled": True,
    "call_provider": "agentphone",
    "agentphone_api_key": API_KEY,
    "agentphone_agent_id": "agt_test",
    "agentphone_number_id": "num_test",
    "agentphone_webhook_secret": SECRET,
    "owner_phone_number": OWNER,
}


@pytest.fixture
async def phone_app() -> AsyncIterator[FastAPI]:
    application = create_app(make_settings(**LIVE), Database(None))
    yield application
    await application.state.ctx.aclose()


@pytest.fixture
async def phone(phone_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=phone_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        yield http


def _mock_call(call_id: str = "call_1") -> respx.Route:
    return respx.post(CALLS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": call_id,
                "agentId": "agt_test",
                "phoneNumberId": "num_test",
                "toNumber": OWNER,
                "direction": "outbound",
                "status": "in-progress",
            },
        )
    )


async def _pending(client: httpx.AsyncClient, text: str = OVER_CAP) -> str:
    plan = (await client.post("/api/intent", json={"text": text})).json()
    body = (await client.post(f"/api/plans/{plan['plan_id']}/execute", json={})).json()
    assert body["status"] == "needs_approval", body
    return str(body["request_id"])


def _signed(
    payload: dict[str, Any], secret: str = SECRET, age_s: int = 0
) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(payload).encode()
    timestamp = str(int(time.time()) - age_s)
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw, hashlib.sha256).hexdigest()
    return raw, {"X-Webhook-Signature": f"sha256={digest}", "X-Webhook-Timestamp": timestamp}


def _voice_event(call_id: str, transcript: str, **data: Any) -> dict[str, Any]:
    return {
        "event": "agent.message",
        "channel": "voice",
        "agentId": "agt_test",
        "data": {
            "callId": call_id,
            "numberId": "num_test",
            "from": "+17165550100",
            "to": OWNER,
            "status": "in-progress",
            "transcript": transcript,
            "confidence": 0.95,
            "direction": "outbound",
            **data,
        },
        "recentHistory": [],
    }


async def _say(
    client: httpx.AsyncClient, transcript: str, call_id: str = "call_1", **data: Any
) -> list[dict[str, Any]]:
    """Post one caller turn, signed. Returns the reply's NDJSON chunks."""
    raw, headers = _signed(_voice_event(call_id, transcript, **data))
    response = await client.post(HOOK, content=raw, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/x-ndjson")
    chunks = [json.loads(line) for line in response.text.splitlines() if line]
    assert chunks, "a voice turn must always be answered with something to say"
    assert all(chunk.get("interim") for chunk in chunks[:-1])
    assert "interim" not in chunks[-1]
    return chunks


async def _end(client: httpx.AsyncClient, call_id: str = "call_1", **data: Any) -> dict[str, Any]:
    payload = {
        "event": "agent.call_ended",
        "channel": "voice",
        "agentId": "agt_test",
        "data": {
            "callId": call_id,
            "to": OWNER,
            "direction": "outbound",
            "status": "completed",
            "disconnectionReason": "user_hangup",
            **data,
        },
    }
    raw, headers = _signed(payload)
    response = await client.post(HOOK, content=raw, headers=headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _spoken(chunks: list[dict[str, Any]]) -> str:
    return " ".join(str(chunk["text"]) for chunk in chunks)


def _row(ctx: AppContext, request_id: str) -> Any:
    row = ctx.approvals.get(request_id)
    assert row is not None
    return row


def _other_word(word: str) -> str:
    return next(candidate for candidate in CHALLENGE_WORDS if candidate != word)


# --------------------------------------------------------------------------- settings


def test_the_default_provider_is_dry_run_and_blank_means_dry_run() -> None:
    assert make_settings().call_provider == "dry_run"
    assert make_settings(call_provider="").call_provider == "dry_run"
    assert make_settings(call_provider=" AgentPhone ").call_provider == "agentphone"
    with pytest.raises(ValidationError):
        make_settings(call_provider="twillio")


def test_calls_are_live_only_when_the_chosen_provider_has_everything() -> None:
    assert make_settings(**LIVE).calls_live
    assert make_settings(**LIVE).call_blockers == []
    for missing in (
        "agentphone_api_key",
        "agentphone_agent_id",
        "agentphone_number_id",
        "agentphone_webhook_secret",
        "owner_phone_number",
    ):
        settings = make_settings(**{**LIVE, missing: ""})
        assert not settings.calls_live
        assert settings.call_blockers == [missing.upper()]
    off = make_settings(**{**LIVE, "calls_enabled": False})
    assert not off.calls_live and off.call_blockers == ["CALLS_ENABLED"]


def test_elevenlabs_settings_alone_no_longer_make_calls_live() -> None:
    settings = make_settings(
        calls_enabled=True,
        elevenlabs_api_key="k",
        elevenlabs_agent_id="a",
        elevenlabs_phone_number_id="p",
        owner_phone_number=OWNER,
    )
    assert not settings.calls_live
    assert settings.call_blockers == ["CALL_PROVIDER"]


# --------------------------------------------------------------------------- hearing a turn


@pytest.mark.parametrize(
    ("text", "amount"),
    [
        ("two hundred and fifty dollars", Decimal(250)),
        ("It's 250.", Decimal(250)),
        ("$1,500", Decimal(1500)),
        ("two hundred fifty dollars and fifty cents", Decimal("250.50")),
        ("a hundred dollars", Decimal(100)),
    ],
)
def test_amounts_are_read_from_speech(text: str, amount: Decimal) -> None:
    assert call_script.spoken_amount(text) == (amount, False)


def test_an_amount_with_two_readings_is_not_taken() -> None:
    assert call_script.spoken_amount("two fifty") == (None, True)
    assert call_script.hear("two fifty harbor", CHALLENGE_WORDS).amount_unclear


def test_words_are_matched_to_the_vocabulary_not_to_the_expected_word() -> None:
    assert call_script.vocabulary_words("the word is Harbor.", CHALLENGE_WORDS) == ("harbor",)
    assert call_script.vocabulary_words("harbour", CHALLENGE_WORDS) == ("harbor",)
    assert call_script.vocabulary_words("lanterns", CHALLENGE_WORDS) == ("lantern",)
    assert call_script.vocabulary_words("harbor, lantern, harbor", CHALLENGE_WORDS) == (
        "harbor",
        "lantern",
    )
    # Ordinary speech near a vocabulary word is not taken for one.
    assert (
        call_script.vocabulary_words(
            "hello fellow, pardon, two hundred dollars to my pocket", CHALLENGE_WORDS
        )
        == ()
    )


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("freeze", "freeze"),
        ("Freeze everything now", "freeze"),
        ("hit the kill switch", "freeze"),
        ("deny it and freeze the vault", "freeze"),
        ("deny", "deny"),
        ("I decline", "deny"),
        ("no", "deny"),
        ("No thanks.", "deny"),
        ("can you repeat that", "repeat"),
        ("go on", "none"),
        ("yes approve it", "none"),
        ("no wait it is two hundred and fifty dollars harbor", "none"),
    ],
)
def test_intents(text: str, intent: str) -> None:
    assert call_script.hear(text, CHALLENGE_WORDS).intent == intent


def test_the_greeting_states_the_request_and_says_it_is_automated() -> None:
    line = call_script.greeting(
        Decimal(250), "Sam's Arc wallet", "it is over your per-transfer limit"
    )
    assert "automated" in line
    assert "250 dollars to Sam's Arc wallet" in line
    assert "over your per-transfer limit" in line
    assert "deny or freeze" in line


# --------------------------------------------------------------------------- placing the call


@respx.mock
async def test_live_call_goes_to_agentphone_in_webhook_mode(
    phone_app: FastAPI, phone: httpx.AsyncClient
) -> None:
    ctx: AppContext = phone_app.state.ctx
    route = _mock_call()
    eleven = respx.post(f"{ELEVENLABS_API}{OUTBOUND_CALL_PATH}")
    request_id = await _pending(phone)

    assert route.call_count == 1 and eleven.call_count == 0
    sent = route.calls.last.request
    assert sent.url.host == "api.agentphone.ai"
    assert sent.headers["Authorization"] == f"Bearer {API_KEY}"
    body = json.loads(sent.content)
    # Webhook mode: no systemPrompt, so every caller turn comes to this server.
    assert set(body) == {"agentId", "toNumber", "fromNumberId", "initialGreeting"}
    assert body["agentId"] == "agt_test"
    assert body["fromNumberId"] == "num_test"
    assert body["toNumber"] == OWNER
    assert "250 dollars to Sam's Arc wallet" in body["initialGreeting"]
    assert "over your per-transfer limit" in body["initialGreeting"]
    # The word on the owner's screen must never leave the server.
    assert _row(ctx, request_id).challenge not in sent.content.decode().lower()

    started = ctx.bus.latest("call.started")
    assert started is not None
    assert started.data == {
        "request_id": request_id,
        "dry_run": False,
        "status": "started",
        "detail": "placed through agentphone",
    }
    assert _row(ctx, request_id).conversation_id == "call_1"
    with ctx.db.session() as session:
        stored = session.get(PhoneSessionRow, "call_1")
    assert stored is not None and (stored.request_id, stored.state) == (request_id, "stated")

    # With live calls, the dashboard cannot stand in for the phone.
    simulated = await phone.post(
        f"/api/approvals/{request_id}/simulate-voice",
        json={"amount_spoken": 250, "challenge": _row(ctx, request_id).challenge},
    )
    assert simulated.status_code == 403


@respx.mock
async def test_a_refused_call_is_recorded_and_the_request_stays_pending(
    phone_app: FastAPI, phone: httpx.AsyncClient
) -> None:
    ctx: AppContext = phone_app.state.ctx
    respx.post(CALLS_URL).mock(
        return_value=httpx.Response(402, json={"detail": "Add a payment method to place calls"})
    )
    request_id = await _pending(phone)
    ended = ctx.bus.latest("call.ended")
    assert ended is not None and ended.data["status"] == "failed"
    assert "402" in ended.data["detail"] and "payment method" in ended.data["detail"]
    assert API_KEY not in ended.data["detail"]
    row = _row(ctx, request_id)
    assert (row.status, row.call_status) == ("pending", "failed")


async def test_dry_run_with_agentphone_chosen_places_no_call() -> None:
    application = create_app(make_settings(**{**LIVE, "calls_enabled": False}), Database(None))
    ctx: AppContext = application.state.ctx
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        with respx.mock(assert_all_called=False) as router:
            route = router.post(CALLS_URL)
            request_id = await _pending(http)
            assert route.call_count == 0
        started = ctx.bus.latest("call.started")
        assert started is not None and started.data["dry_run"] is True
        # In dry run the dashboard stands in for the phone, through the same checks.
        simulated = await http.post(
            f"/api/approvals/{request_id}/simulate-voice",
            json={"amount_spoken": 250, "challenge": _row(ctx, request_id).challenge},
        )
        assert simulated.json()["status"] == "approved"
        await asyncio.sleep(0.05)  # let the stubbed transfer start before shutdown
    await ctx.aclose()


@respx.mock
async def test_the_daily_call_cap_applies_to_agentphone() -> None:
    application = create_app(make_settings(**{**LIVE, "max_calls_per_day": 1}), Database(None))
    ctx: AppContext = application.state.ctx
    route = _mock_call()
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        await _pending(http)
        plan = (await http.post("/api/intent", json={"text": OVER_CAP})).json()
        second = (await http.post(f"/api/plans/{plan['plan_id']}/execute", json={})).json()
        assert route.call_count == 1
        row = _row(ctx, str(second["request_id"]))
        assert (row.status, row.resolved_via, row.call_status) == ("denied", "call_cap", "capped")
    await ctx.aclose()


# --------------------------------------------------------------------------- the signature


@respx.mock
async def test_the_webhook_needs_a_fresh_valid_signature(
    phone_app: FastAPI, phone: httpx.AsyncClient
) -> None:
    ctx: AppContext = phone_app.state.ctx
    _mock_call()
    request_id = await _pending(phone)
    event = _voice_event("call_1", "deny")
    raw, good = _signed(event)

    assert (await phone.post(HOOK, content=raw)).status_code == 401
    assert (
        await phone.post(HOOK, content=raw, headers=_signed(event, secret="other")[1])
    ).status_code == 401
    assert (
        await phone.post(HOOK, content=raw, headers=_signed(event, age_s=301)[1])
    ).status_code == 401
    assert (
        await phone.post(HOOK, content=raw, headers=_signed(event, age_s=-301)[1])
    ).status_code == 401
    # A valid signature over a different body does not cover this one.
    assert (await phone.post(HOOK, content=raw + b" ", headers=good)).status_code == 401
    no_timestamp = {"X-Webhook-Signature": good["X-Webhook-Signature"]}
    assert (await phone.post(HOOK, content=raw, headers=no_timestamp)).status_code == 401
    assert _row(ctx, request_id).status == "pending"

    assert (await phone.post(HOOK, content=raw, headers=good)).status_code == 200
    assert _row(ctx, request_id).status == "denied"


async def test_the_webhook_is_off_without_a_secret(client: httpx.AsyncClient) -> None:
    raw, headers = _signed(_voice_event("call_1", "hello"))
    assert (await client.post(HOOK, content=raw, headers=headers)).status_code == 503


@respx.mock
async def test_other_events_are_acknowledged_and_change_nothing(
    phone_app: FastAPI, phone: httpx.AsyncClient
) -> None:
    ctx: AppContext = phone_app.state.ctx
    _mock_call()
    request_id = await _pending(phone)
    for payload in (
        {"event": "agent.message", "channel": "sms", "agentId": "agt_test", "data": {}},
        {**_voice_event("call_1", "deny"), "test": True},
        {**_voice_event("call_1", "deny"), "agentId": "agt_someone_else"},
        {"event": "agent.reaction", "channel": "imessage", "agentId": "agt_test"},
    ):
        raw, headers = _signed(payload)
        response = await phone.post(HOOK, content=raw, headers=headers)
        assert response.json() == {"ok": True, "status": "ignored"}
    raw, headers = _signed({"event": "agent.message"})
    not_object = b"[1, 2]"
    assert (
        await phone.post(
            HOOK,
            content=not_object,
            headers={
                "X-Webhook-Timestamp": headers["X-Webhook-Timestamp"],
                "X-Webhook-Signature": "sha256="
                + hmac.new(
                    SECRET.encode(),
                    headers["X-Webhook-Timestamp"].encode() + b"." + not_object,
                    hashlib.sha256,
                ).hexdigest(),
            },
        )
    ).status_code == 400
    assert _row(ctx, request_id).status == "pending"


# --------------------------------------------------------------------------- the conversation


@respx.mock
async def test_objection_comes_first_then_amount_and_word_approve(
    phone_app: FastAPI, phone: httpx.AsyncClient
) -> None:
    ctx: AppContext = phone_app.state.ctx
    _mock_call()
    request_id = await _pending(phone)
    word = _row(ctx, request_id).challenge

    # Saying the right amount and word straight away approves nothing: the risk officer
    # speaks first.
    first = await _say(phone, f"two hundred and fifty dollars {word}")
    assert len(first) == 3
    # The operator says where it came from: the owner's own sentence, linked by now.
    assert first[0]["text"].startswith("The operator. You asked for this yourself:")
    assert OVER_CAP in first[0]["text"]
    assert first[1]["text"].startswith("Now the risk officer.")
    assert "over your 200 dollars transfer limit" in first[1]["text"]
    assert "say the amount, then the word shown on your dashboard" in first[-1]["text"]
    assert not first[-1].get("hangup")
    assert _row(ctx, request_id).status == "pending"
    assert word not in _spoken(first).lower()

    amount_only = await _say(phone, "two hundred and fifty dollars")
    assert amount_only == [
        {"text": "I have 250 dollars. Now say the word shown on your dashboard."}
    ]
    assert _row(ctx, request_id).status == "pending"

    done = await _say(phone, f"{word}.")
    assert done[0] == {"text": "Checking.", "interim": True}
    assert done[-1] == {
        "text": "Approved. 250 dollars to Sam's Arc wallet is going out now. Goodbye.",
        "hangup": True,
    }
    row = _row(ctx, request_id)
    assert (row.status, row.resolved_via, row.voice_ok) == ("approved", "voice", True)
    resolved = ctx.bus.latest("approval.resolved")
    assert resolved is not None and resolved.data["approved"] is True

    assert (await _end(phone))["status"] == "recorded"
    assert _row(ctx, request_id).status == "approved"
    ended = ctx.bus.latest("call.ended")
    assert ended is not None and ended.data["status"] == "ended"


@respx.mock
async def test_amount_and_word_in_one_turn(phone_app: FastAPI, phone: httpx.AsyncClient) -> None:
    ctx: AppContext = phone_app.state.ctx
    _mock_call()
    request_id = await _pending(phone)
    word = _row(ctx, request_id).challenge
    await _say(phone, "go on")
    done = await _say(phone, f"It is 250 dollars and the word is {word}")
    assert done[-1]["hangup"] is True
    assert _row(ctx, request_id).status == "approved"


@respx.mock
async def test_three_wrong_tries_deny(phone_app: FastAPI, phone: httpx.AsyncClient) -> None:
    ctx: AppContext = phone_app.state.ctx
    _mock_call()
    request_id = await _pending(phone)
    word = _row(ctx, request_id).challenge
    wrong = _other_word(word)
    await _say(phone, "go on")

    first = await _say(phone, f"250 dollars {wrong}")
    assert first[-1] == {
        "text": "The word did not match. You have 2 tries left. Say the amount and the word again."
    }
    second = await _say(phone, f"300 dollars {word}")
    assert second[-1]["text"].startswith("The amount did not match. You have 1 try left.")
    third = await _say(phone, f"250 dollars {wrong}")
    assert third[-1] == {"text": call_script.TOO_MANY_ATTEMPTS, "hangup": True}
    row = _row(ctx, request_id)
    assert (row.status, row.resolved_via) == ("denied", "too_many_attempts")

    # The right answer after that is too late.
    late = await _say(phone, f"250 dollars {word}")
    assert late == [{"text": call_script.already_resolved("denied"), "hangup": True}]
    assert _row(ctx, request_id).status == "denied"


@respx.mock
async def test_listing_several_words_is_a_wrong_try_even_with_the_right_one(
    phone_app: FastAPI, phone: httpx.AsyncClient
) -> None:
    ctx: AppContext = phone_app.state.ctx
    _mock_call()
    request_id = await _pending(phone)
    word = _row(ctx, request_id).challenge
    await _say(phone, "go on")
    reply = await _say(phone, f"250 dollars {word} {_other_word(word)}")
    assert reply[0] == {"text": call_script.TOO_MANY_WORDS, "interim": True}
    assert "2 tries left" in reply[-1]["text"]
    row = _row(ctx, request_id)
    assert (row.status, row.attempts) == ("pending", 1)


@respx.mock
async def test_an_unclear_amount_is_asked_again_and_costs_no_try(
    phone_app: FastAPI, phone: httpx.AsyncClient
) -> None:
    ctx: AppContext = phone_app.state.ctx
    _mock_call()
    request_id = await _pending(phone)
    word = _row(ctx, request_id).challenge
    await _say(phone, "go on")
    assert await _say(phone, f"two fifty {word}") == [{"text": call_script.AMOUNT_UNCLEAR}]
    assert await _say(phone, "yes, approve it") == [{"text": call_script.NEED_AMOUNT}]
    assert _row(ctx, request_id).attempts == 0
    done = await _say(phone, "two hundred fifty")
    assert done[-1]["hangup"] is True
    assert _row(ctx, request_id).status == "approved"


@respx.mock
async def test_deny_and_freeze_by_voice(phone_app: FastAPI, phone: httpx.AsyncClient) -> None:
    ctx: AppContext = phone_app.state.ctx
    _mock_call("call_1")
    first = await _pending(phone)
    assert await _say(phone, "No.") == [{"text": call_script.DENIED, "hangup": True}]
    row = _row(ctx, first)
    assert (row.status, row.resolved_via) == ("denied", "voice")

    respx.post(CALLS_URL).mock(return_value=httpx.Response(200, json={"id": "call_2"}))
    second = await _pending(phone)
    reply = await _say(phone, "freeze everything", call_id="call_2")
    assert reply[-1] == {"text": call_script.FROZEN, "hangup": True}
    assert _row(ctx, second).status == "denied"
    assert await ctx.vault.is_frozen()
    frozen = ctx.bus.latest("frozen")
    assert frozen is not None and frozen.data == {"frozen": True, "via": "voice"}


@respx.mock
async def test_repeat_restates_the_request(phone_app: FastAPI, phone: httpx.AsyncClient) -> None:
    ctx: AppContext = phone_app.state.ctx
    _mock_call()
    request_id = await _pending(phone)
    await _say(phone, "hello?")
    again = await _say(phone, "sorry, can you repeat that")
    assert "250 dollars to Sam's Arc wallet" in again[0]["text"]
    assert again[1]["text"].startswith("Now the risk officer.")
    assert _row(ctx, request_id).status == "pending"


@respx.mock
async def test_above_the_passkey_limit_the_call_is_not_enough(
    phone_app: FastAPI, phone: httpx.AsyncClient
) -> None:
    ctx: AppContext = phone_app.state.ctx
    _mock_call()
    request_id = await _pending(phone, OVER_PASSKEY)
    word = _row(ctx, request_id).challenge
    opening = await _say(phone, "go on")
    assert "confirm with your passkey" in opening[-1]["text"]
    reply = await _say(phone, f"1500 dollars {word}")
    assert reply[-1] == {"text": call_script.AWAITING_PASSKEY, "hangup": True}
    row = _row(ctx, request_id)
    assert (row.status, row.voice_ok, row.passkey_ok) == ("pending", True, False)

    # Hanging up after the voice step does not deny: the passkey is still to come.
    assert (await _end(phone))["status"] == "recorded"
    assert _row(ctx, request_id).status == "pending"
    assert (await ctx.approvals.confirm_passkey(request_id)).status == "approved"
    assert _row(ctx, request_id).resolved_via == "voice+passkey"


@respx.mock
async def test_a_call_that_is_never_answered_denies(
    phone_app: FastAPI, phone: httpx.AsyncClient
) -> None:
    ctx: AppContext = phone_app.state.ctx
    missed: list[tuple[str, str]] = []

    async def hook(row: object, status: str) -> None:
        missed.append((getattr(row, "request_id", ""), status))

    ctx.approvals.missed_call_hooks.append(hook)
    _mock_call()
    request_id = await _pending(phone)
    ended = await _end(phone, status="failed", disconnectionReason="dial_no_answer")
    assert ended["status"] == "denied: call was not answered"
    row = _row(ctx, request_id)
    assert (row.status, row.resolved_via) == ("denied", "no_answer")
    assert row.call_status == "no_answer (dial_no_answer)"
    assert missed == [(request_id, "no_answer (dial_no_answer)")]
    resolved = ctx.bus.latest("approval.resolved")
    assert resolved is not None and resolved.data["approved"] is False


@respx.mock
async def test_hanging_up_without_a_decision_denies(
    phone_app: FastAPI, phone: httpx.AsyncClient
) -> None:
    ctx: AppContext = phone_app.state.ctx
    _mock_call()
    request_id = await _pending(phone)
    await _say(phone, "go on")
    await _say(phone, "two hundred and fifty dollars")
    assert (await _end(phone))["status"] == "denied: call ended without a decision"
    row = _row(ctx, request_id)
    assert (row.status, row.resolved_via, row.call_status) == (
        "denied",
        "call_ended",
        "ended_without_decision",
    )


@respx.mock
async def test_a_call_with_too_many_turns_is_denied(
    phone_app: FastAPI, phone: httpx.AsyncClient
) -> None:
    ctx: AppContext = phone_app.state.ctx
    _mock_call()
    request_id = await _pending(phone)
    for _ in range(MAX_PHONE_TURNS):
        reply = await _say(phone, "hmm")
        assert not reply[-1].get("hangup")
    last = await _say(phone, "hmm")
    assert last == [{"text": call_script.TOO_LONG, "hangup": True}]
    assert _row(ctx, request_id).resolved_via == "call_too_long"


@respx.mock
async def test_an_expired_request_cannot_be_approved_on_the_call() -> None:
    application = create_app(make_settings(**{**LIVE, "approval_expiry_s": 0}), Database(None))
    ctx: AppContext = application.state.ctx
    _mock_call()
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        request_id = await _pending(http)
        word = _row(ctx, request_id).challenge
        reply = await _say(http, f"250 dollars {word}")
        assert reply[-1]["hangup"] is True
        assert _row(ctx, request_id).status == "expired"
    await ctx.aclose()


# --------------------------------------------------------------------------- whose call it is


@respx.mock
async def test_a_call_this_server_did_not_place_can_approve_nothing(
    phone_app: FastAPI, phone: httpx.AsyncClient
) -> None:
    ctx: AppContext = phone_app.state.ctx
    _mock_call()
    request_id = await _pending(phone)
    word = _row(ctx, request_id).challenge
    refusal = [{"text": call_script.NOT_OUR_CALL, "hangup": True}]

    # Someone rings the number, even from the owner's own caller id.
    inbound = {"direction": "inbound", "from": OWNER, "to": "+17165550100"}
    assert await _say(phone, "go on", call_id="call_in", **inbound) == refusal
    assert await _say(phone, f"250 dollars {word}", call_id="call_in", **inbound) == refusal
    # An outbound call to some other number.
    other = {"to": "+16045559999"}
    assert await _say(phone, f"250 dollars {word}", call_id="call_x", **other) == refusal
    # No call id at all.
    assert await _say(phone, f"250 dollars {word}", call_id="") == refusal

    row = _row(ctx, request_id)
    assert (row.status, row.attempts) == ("pending", 0)
    assert (await _end(phone, call_id="call_in", **inbound))["status"].startswith("ignored")
    assert _row(ctx, request_id).status == "pending"


@respx.mock
async def test_a_different_call_id_is_matched_only_for_our_one_waiting_call(
    phone_app: FastAPI, phone: httpx.AsyncClient
) -> None:
    ctx: AppContext = phone_app.state.ctx
    _mock_call("call_placed")
    request_id = await _pending(phone)
    word = _row(ctx, request_id).challenge

    # The webhook names the call differently, but it is outbound and to the owner's number.
    opening = await _say(phone, "go on", call_id="call_webhook")
    assert opening[1]["text"].startswith("Now the risk officer.")
    done = await _say(phone, f"250 dollars {word}", call_id="call_webhook")
    assert done[-1]["hangup"] is True
    assert _row(ctx, request_id).status == "approved"
    # The session moved to the webhook's id; a third id finds nothing to adopt.
    refusal = [{"text": call_script.NOT_OUR_CALL, "hangup": True}]
    assert await _say(phone, "go on", call_id="call_third") == refusal


# --------------------------------------------------------------------------- rules by voice


@respx.mock
async def test_rules_are_confirmed_on_the_dashboard_when_the_call_is_agentphone(
    phone_app: FastAPI, phone: httpx.AsyncClient
) -> None:
    """The AgentPhone call is a fixed approval script with no rule tools, so the dashboard
    confirmation must stay available."""
    proposal = (
        await phone.post("/api/rules/propose", json={"text": "lower my daily budget to 300"})
    ).json()
    confirmed = await phone.post(f"/api/rules/{proposal['proposal_id']}/confirm", json={})
    assert confirmed.status_code == 200
    assert confirmed.json()["ok"] is True
