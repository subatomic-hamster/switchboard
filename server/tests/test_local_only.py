"""The dashboard's routes stay on this machine when the server is behind a public tunnel."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app import main
from app.config import Settings
from app.context import AppContext
from app.db import Database
from app.local_only import host_name

TUNNEL = {"Host": "random-words.trycloudflare.com", "Cf-Connecting-Ip": "203.0.113.9"}
OVER_CAP = "send 250 from my arc wallet to sam's arc wallet"

DASHBOARD_ROUTES = [
    ("GET", "/api/approvals"),
    ("GET", "/api/stats"),
    ("GET", "/api/rules"),
    ("GET", "/api/events/history"),
    ("GET", "/api/summary"),
    ("GET", "/api/desk/markets"),
    ("GET", "/api/passkey/status"),
    ("GET", "/events"),
    ("POST", "/api/intent"),
    ("POST", "/api/freeze"),
    ("POST", "/api/hostile"),
    ("POST", "/api/rules/propose"),
    ("POST", "/api/approvals/1/simulate-voice"),
    ("POST", "/api/approvals/1/deny"),
    ("POST", "/api/chain/transfer"),
    ("POST", "/api/chain/stub/stream"),
    ("POST", "/api/markets/move"),
    ("POST", "/api/forecasts"),
    ("POST", "/api/passkey/register/options"),
]


def test_host_name() -> None:
    assert host_name("localhost:8787") == "localhost"
    assert host_name("127.0.0.1") == "127.0.0.1"
    assert host_name("[::1]:8787") == "[::1]"
    assert host_name("LOCALHOST:5173") == "localhost"
    assert host_name("evil.example:8787") == "evil.example"


@pytest.mark.parametrize(("method", "path"), DASHBOARD_ROUTES)
async def test_dashboard_routes_are_refused_through_a_tunnel(
    client: httpx.AsyncClient, ctx: AppContext, method: str, path: str
) -> None:
    response = await client.request(method, path, json={}, headers=TUNNEL)
    assert response.status_code == 403
    assert "only served on the machine" in response.json()["detail"]
    assert ctx.bus.history() == []


async def test_claim_page_and_assets_are_public_but_the_dashboard_root_is_not(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "a.js").write_text("x")
    (tmp_path / "index.html").write_text("<html>spa</html>")
    # Point the app at this fake build, whether or not a real web/dist exists on this machine.
    monkeypatch.setattr(main, "WEB_DIST", tmp_path)
    app = main.create_app(settings, Database(None))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as http:
            page = await http.get("/claim", headers=TUNNEL)
            assert page.status_code == 200 and "spa" in page.text
            assert (await http.get("/assets/a.js", headers=TUNNEL)).status_code == 200
            assert (await http.get("/", headers=TUNNEL)).status_code == 403
    finally:
        await app.state.ctx.aclose()


async def test_the_challenge_word_cannot_be_fetched_from_outside(
    client: httpx.AsyncClient, ctx: AppContext, voice_headers: dict[str, str]
) -> None:
    plan = (await client.post("/api/intent", json={"text": OVER_CAP})).json()
    request = (await client.post(f"/api/plans/{plan['plan_id']}/execute", json={})).json()
    row = ctx.approvals.get(request["request_id"])
    assert row is not None

    # The owner's own browser sees the word.
    local = await client.get("/api/approvals")
    assert row.challenge in local.text

    # Anyone coming through the tunnel does not, whichever header gives them away.
    for headers in (
        TUNNEL,
        {"Host": "random-words.trycloudflare.com"},
        {"X-Forwarded-For": "203.0.113.9"},
        {"Forwarded": "for=203.0.113.9"},
        {"Host": "localhost.evil.example"},
    ):
        outside = await client.get("/api/approvals", headers=headers)
        assert outside.status_code == 403, headers
        assert row.challenge not in outside.text

    # The voice agent, coming through the same tunnel with its token, gets the request details
    # and still not the word.
    info = await client.get(
        f"/api/voice/request/{request['request_id']}", headers={**TUNNEL, **voice_headers}
    )
    assert info.status_code == 200
    assert row.challenge not in info.text


async def test_public_routes_still_need_their_own_credential(
    client: httpx.AsyncClient, bot_headers: dict[str, str], voice_headers: dict[str, str]
) -> None:
    assert (await client.get("/api/health", headers=TUNNEL)).status_code == 200
    assert (await client.get("/api/bots/summary", headers=TUNNEL)).status_code == 401
    assert (
        await client.get("/api/bots/summary", headers={**TUNNEL, **bot_headers})
    ).status_code == 200
    assert (await client.post("/api/voice/freeze", json={}, headers=TUNNEL)).status_code == 401
    assert (
        await client.post("/api/voice/hooks/elevenlabs", content=b"{}", headers=TUNNEL)
    ).status_code in {401, 503}
    assert (
        await client.get("/api/voice/markets/list", headers={**TUNNEL, **voice_headers})
    ).status_code == 200


async def test_local_requests_are_served(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    for base in ("http://localhost:8787", "http://127.0.0.1:8787", "http://[::1]:8787"):
        async with httpx.AsyncClient(transport=transport, base_url=base) as http:
            assert (await http.get("/api/stats")).status_code == 200, base
