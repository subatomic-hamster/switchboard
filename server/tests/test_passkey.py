"""Passkey confirmation above the second threshold."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI

from app.context import AppContext
from app.db import Database
from app.main import create_app
from tests.conftest import VOICE_TOKEN, make_settings
from tests.webauthn_sim import SoftAuthenticator

ORIGIN = "http://localhost:5173"
HEADERS = {"Origin": ORIGIN}
VOICE = {"Authorization": f"Bearer {VOICE_TOKEN}"}
BIG = "send 250 from my arc wallet to sam's arc wallet"
SMALL_NEW = "send 20 from my arc wallet to 0x9c1e8a4f2b7d6c5a3e1f0b9d8c7a6e5f4d3c4f2c"


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    # A low second threshold, so a 250 dollar request needs the passkey as well.
    application = create_app(make_settings(passkey_threshold_usd=Decimal("100")), Database(None))
    yield application
    await application.state.ctx.aclose()


async def _pending(client: httpx.AsyncClient, text: str = BIG) -> str:
    plan = (await client.post("/api/intent", json={"text": text})).json()
    body = (await client.post(f"/api/plans/{plan['plan_id']}/execute", json={})).json()
    assert body["status"] == "needs_approval"
    return str(body["request_id"])


async def _register(client: httpx.AsyncClient) -> SoftAuthenticator:
    authenticator = SoftAuthenticator()
    options = (await client.post("/api/passkey/register/options", headers=HEADERS)).json()
    assert options["rp"] == {"name": "Switchboard", "id": "localhost"}
    response = await client.post(
        "/api/passkey/register/verify",
        json={"credential": authenticator.create(options, ORIGIN)},
        headers=HEADERS,
    )
    assert response.json() == {"ok": True, "status": "registered", "error": ""}
    return authenticator


async def _assert(
    client: httpx.AsyncClient, authenticator: SoftAuthenticator, request_id: str
) -> httpx.Response:
    options = await client.post(
        "/api/passkey/assert/options", json={"request_id": request_id}, headers=HEADERS
    )
    assert options.status_code == 200, options.text
    return await client.post(
        "/api/passkey/assert/verify",
        json={"request_id": request_id, "credential": authenticator.get(options.json(), ORIGIN)},
        headers=HEADERS,
    )


async def _voice_approve(client: httpx.AsyncClient, ctx: AppContext, request_id: str) -> dict:  # type: ignore[type-arg]
    row = ctx.approvals.get(request_id)
    assert row is not None
    response = await client.post(
        "/api/voice/approve",
        json={"request_id": request_id, "amount_spoken": 250, "challenge": row.challenge},
        headers=VOICE,
    )
    return dict(response.json())


async def test_large_approval_needs_voice_then_passkey(
    client: httpx.AsyncClient, ctx: AppContext
) -> None:
    assert (await client.get("/api/passkey/status")).json() == {"registered": False}
    authenticator = await _register(client)
    assert (await client.get("/api/stats")).json()["passkey_registered"] is True

    request_id = await _pending(client)
    view = (await client.get("/api/approvals")).json()["approvals"][0]
    assert view["requires_passkey"] is True

    assert await _voice_approve(client, ctx, request_id) == {
        "ok": True,
        "status": "awaiting_passkey",
        "error": "",
    }
    assert ctx.bus.latest("approval.resolved") is None
    assert await ctx.vault.spent_today() == Decimal("0")

    confirmed = await _assert(client, authenticator, request_id)
    assert confirmed.json() == {"ok": True, "status": "approved", "error": ""}
    await asyncio.sleep(0.1)
    assert await ctx.vault.spent_today() == Decimal("250")
    row = ctx.approvals.get(request_id)
    assert row is not None and row.resolved_via == "voice+passkey"


async def test_passkey_first_then_voice(client: httpx.AsyncClient, ctx: AppContext) -> None:
    authenticator = await _register(client)
    request_id = await _pending(client)
    first = await _assert(client, authenticator, request_id)
    assert first.json() == {"ok": True, "status": "awaiting_voice", "error": ""}
    assert await ctx.vault.spent_today() == Decimal("0")
    assert (await _voice_approve(client, ctx, request_id))["status"] == "approved"


async def test_passkey_alone_never_approves(client: httpx.AsyncClient, ctx: AppContext) -> None:
    authenticator = await _register(client)
    request_id = await _pending(client)
    await _assert(client, authenticator, request_id)
    row = ctx.approvals.get(request_id)
    assert row is not None and row.status == "pending"
    assert "transfer.sent" not in [event.type for event in ctx.bus.history()]


async def test_an_assertion_is_bound_to_its_request(
    client: httpx.AsyncClient, ctx: AppContext
) -> None:
    authenticator = await _register(client)
    first = await _pending(client)
    second = await _pending(client)
    options = (
        await client.post(
            "/api/passkey/assert/options", json={"request_id": first}, headers=HEADERS
        )
    ).json()
    credential = authenticator.get(options, ORIGIN)
    # Present the assertion made for the first request against the second one.
    replay = await client.post(
        "/api/passkey/assert/verify",
        json={"request_id": second, "credential": credential},
        headers=HEADERS,
    )
    assert replay.status_code == 400
    row = ctx.approvals.get(second)
    assert row is not None and row.passkey_ok is False


async def test_a_challenge_cannot_be_used_twice(client: httpx.AsyncClient) -> None:
    authenticator = await _register(client)
    request_id = await _pending(client)
    options = (
        await client.post(
            "/api/passkey/assert/options", json={"request_id": request_id}, headers=HEADERS
        )
    ).json()
    credential = authenticator.get(options, ORIGIN)
    body = {"request_id": request_id, "credential": credential}
    assert (
        await client.post("/api/passkey/assert/verify", json=body, headers=HEADERS)
    ).status_code == 200
    again = await client.post("/api/passkey/assert/verify", json=body, headers=HEADERS)
    assert again.status_code == 400
    assert "expired" in again.json()["detail"]


async def test_a_different_key_is_rejected(client: httpx.AsyncClient, ctx: AppContext) -> None:
    await _register(client)
    request_id = await _pending(client)
    stranger = SoftAuthenticator()
    response = await _assert(client, stranger, request_id)
    assert response.status_code == 400
    row = ctx.approvals.get(request_id)
    assert row is not None and row.passkey_ok is False


async def test_only_the_dashboard_origin_may_use_passkeys(client: httpx.AsyncClient) -> None:
    # No Origin: the passkey route refuses. Another site's Origin: the local-only guard refuses
    # it first, before the route runs.
    for headers, status in (({}, 400), ({"Origin": "https://evil.example"}, 403)):
        response = await client.post("/api/passkey/register/options", headers=headers)
        assert response.status_code == status


async def test_only_one_passkey_can_be_registered(client: httpx.AsyncClient) -> None:
    await _register(client)
    again = await client.post("/api/passkey/register/options", headers=HEADERS)
    assert again.status_code == 400
    assert "already registered" in again.json()["detail"]


async def test_small_requests_do_not_take_a_passkey(
    client: httpx.AsyncClient, ctx: AppContext
) -> None:
    await _register(client)
    request_id = await _pending(client, SMALL_NEW)
    row = ctx.approvals.get(request_id)
    assert row is not None and row.passkey_required is False
    response = await client.post(
        "/api/passkey/assert/options", json={"request_id": request_id}, headers=HEADERS
    )
    assert response.status_code == 400


async def test_large_request_without_a_registered_passkey_cannot_complete(
    client: httpx.AsyncClient, ctx: AppContext
) -> None:
    request_id = await _pending(client)
    assert (await _voice_approve(client, ctx, request_id))["status"] == "awaiting_passkey"
    response = await client.post(
        "/api/passkey/assert/options", json={"request_id": request_id}, headers=HEADERS
    )
    assert response.status_code == 400
    assert "no passkey is registered" in response.json()["detail"]
    assert await ctx.vault.spent_today() == Decimal("0")
