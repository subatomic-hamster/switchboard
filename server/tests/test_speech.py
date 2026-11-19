"""Dashboard speech through ElevenLabs, and the browser-widget fallback for the approval."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest
import respx
from fastapi import FastAPI

from app.context import AppContext
from app.db import Database
from app.main import create_app
from app.speech import DEFAULT_VOICE_ID, ELEVENLABS_API, STT_PATH, TTS_PATH
from tests.conftest import make_settings

OVER_CAP = "send 250 from my arc wallet to sam's arc wallet"


@pytest.fixture
async def keyed_app() -> AsyncIterator[FastAPI]:
    application = create_app(
        make_settings(elevenlabs_api_key="xi-test-key", elevenlabs_agent_id="agent_test"),
        Database(None),
    )
    yield application
    await application.state.ctx.aclose()


@pytest.fixture
async def keyed(keyed_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=keyed_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        yield http


async def test_without_a_key_the_dashboard_is_told_to_use_the_browser(
    client: httpx.AsyncClient,
) -> None:
    assert (await client.get("/api/speech")).json() == {"elevenlabs": False}
    with respx.mock(assert_all_called=False) as router:
        tts = router.post(url__startswith=f"{ELEVENLABS_API}{TTS_PATH}")
        stt = router.post(f"{ELEVENLABS_API}{STT_PATH}")
        said = await client.post("/api/speech/say", json={"text": "hello"})
        heard = await client.post("/api/speech/hear", content=b"audio")
        assert (said.status_code, heard.status_code) == (503, 503)
        assert tts.call_count == 0 and stt.call_count == 0


@respx.mock
async def test_say_sends_the_line_to_elevenlabs_and_returns_audio(
    keyed: httpx.AsyncClient,
) -> None:
    route = respx.post(f"{ELEVENLABS_API}{TTS_PATH}/{DEFAULT_VOICE_ID}").mock(
        return_value=httpx.Response(200, content=b"ID3-mp3-bytes")
    )
    assert (await keyed.get("/api/speech")).json() == {"elevenlabs": True}
    response = await keyed.post("/api/speech/say", json={"text": "Sending 250 dollars to Sam."})
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content == b"ID3-mp3-bytes"
    sent = route.calls.last.request
    assert sent.url.host == "api.elevenlabs.io"
    assert sent.headers["xi-api-key"] == "xi-test-key"
    assert sent.url.params["output_format"] == "mp3_44100_128"
    assert json.loads(sent.content) == {
        "text": "Sending 250 dollars to Sam.",
        "model_id": "eleven_flash_v2_5",
    }


@respx.mock
async def test_hear_sends_the_recording_and_returns_text(keyed: httpx.AsyncClient) -> None:
    route = respx.post(f"{ELEVENLABS_API}{STT_PATH}").mock(
        return_value=httpx.Response(200, json={"text": " Send 20 dollars to Sam. "})
    )
    response = await keyed.post(
        "/api/speech/hear", content=b"webm-bytes", headers={"Content-Type": "audio/webm"}
    )
    assert response.json() == {"text": "Send 20 dollars to Sam."}
    sent = route.calls.last.request
    assert sent.headers["xi-api-key"] == "xi-test-key"
    assert b"webm-bytes" in sent.content
    assert b"scribe_v3" in sent.content
    assert b"audio/webm" in sent.content


@respx.mock
async def test_limits_and_refusals(keyed: httpx.AsyncClient) -> None:
    respx.post(f"{ELEVENLABS_API}{TTS_PATH}/{DEFAULT_VOICE_ID}").mock(
        return_value=httpx.Response(401, json={"detail": "invalid key"})
    )
    refused = await keyed.post("/api/speech/say", json={"text": "hello"})
    assert refused.status_code == 503
    assert "xi-test-key" not in refused.text
    assert (await keyed.post("/api/speech/say", json={"text": ""})).status_code == 422
    assert (await keyed.post("/api/speech/say", json={"text": "x" * 1501})).status_code == 422
    assert (await keyed.post("/api/speech/hear", content=b"")).status_code == 422
    too_long = await keyed.post("/api/speech/hear", content=b"x" * (4 * 1024 * 1024 + 1))
    assert too_long.status_code == 413


async def test_speech_routes_are_not_served_through_a_tunnel(keyed: httpx.AsyncClient) -> None:
    outside = {"X-Forwarded-For": "203.0.113.9"}
    assert (await keyed.get("/api/speech", headers=outside)).status_code == 403
    assert (
        await keyed.post("/api/speech/say", json={"text": "hello"}, headers=outside)
    ).status_code == 403


async def test_widget_config_carries_the_request_but_never_the_word(
    keyed_app: FastAPI, keyed: httpx.AsyncClient, client: httpx.AsyncClient
) -> None:
    ctx: AppContext = keyed_app.state.ctx
    plan = (await keyed.post("/api/intent", json={"text": OVER_CAP})).json()
    body = (await keyed.post(f"/api/plans/{plan['plan_id']}/execute", json={})).json()
    request_id = str(body["request_id"])
    response = await keyed.get(f"/api/approvals/{request_id}/widget")
    assert response.status_code == 200
    config = response.json()
    assert config["agent_id"] == "agent_test"
    variables = config["dynamic_variables"]
    assert variables["request_id"] == request_id
    assert variables["amount_words"] == "250 dollars"
    assert variables["spent_today_usd"] == "0"
    row = ctx.approvals.get(request_id)
    assert row is not None and row.challenge not in response.text.lower()
    assert (await keyed.get("/api/approvals/nope/widget")).status_code == 404
    # No agent id, no widget.
    assert (await client.get(f"/api/approvals/{request_id}/widget")).status_code == 404
