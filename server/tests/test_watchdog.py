"""The dead man's switch (`app/chain/watchdog.py`). A fake clock, never a sleep."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.chain.watchdog import Watchdog
from app.context import AppContext
from app.db import Database
from app.main import create_app
from tests.conftest import make_settings
from tests.test_stream import KEY, PATH, SECRET, VAULT, flat, resolved_log, sign

HEARTBEAT = json.dumps({"heartbeat": 123}).encode()


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
async def real_app() -> AsyncIterator[FastAPI]:
    application = create_app(
        make_settings(
            use_p1_stubs=False,
            vault_address=VAULT,
            arc_rpc_url="http://127.0.0.1:1",
            owner_private_key=KEY,
            agent_private_key=KEY,
            approver_private_key=KEY,
            quicknode_stream_secret=SECRET,
        ),
        Database(None),
    )
    yield application
    await application.state.ctx.aclose()


@pytest.fixture
def ctx(real_app: FastAPI) -> AppContext:
    context: AppContext = real_app.state.ctx
    return context


@pytest.fixture
def clock(ctx: AppContext) -> Clock:
    fake = Clock()
    ctx.watchdog = Watchdog(ctx, clock=fake)
    return fake


@pytest.fixture
async def http(real_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app), base_url="http://localhost"
    ) as client:
        yield client


async def beat(http: httpx.AsyncClient, body: bytes = HEARTBEAT) -> httpx.Response:
    return await http.post(PATH, content=body, headers=sign(body))


def stale_events(ctx: AppContext) -> list[dict[str, Any]]:
    return [e.data for e in ctx.bus.history() if e.type == "stream.stale"]


async def test_stale_at_startup_until_the_first_heartbeat(
    ctx: AppContext, clock: Clock, http: httpx.AsyncClient
) -> None:
    # Safe default: nothing has been heard, so spending is already refused.
    assert ctx.stream_stale is True
    assert ctx.last_stream_delivery is None
    clock.advance(10)
    assert (await beat(http)).status_code == 200
    assert ctx.stream_stale is False
    assert stale_events(ctx) == [{"stale": False, "seconds_since_delivery": 0}]


async def test_the_background_task_announces_the_startup_state(
    ctx: AppContext, clock: Clock
) -> None:
    task = asyncio.get_running_loop().create_task(ctx.watchdog.run())  # type: ignore[union-attr]
    await asyncio.sleep(0)  # let it run to its first wait; no time passes
    task.cancel()
    assert stale_events(ctx) == [{"stale": True, "seconds_since_delivery": 0}]


async def test_silence_turns_stale_once_and_a_delivery_recovers_it(
    ctx: AppContext, clock: Clock, http: httpx.AsyncClient
) -> None:
    assert ctx.watchdog is not None
    await beat(http)
    clock.advance(90)  # the limit is exceeded, not reached
    await ctx.watchdog.check()
    assert ctx.stream_stale is False

    clock.advance(1)
    await ctx.watchdog.check()
    assert ctx.stream_stale is True
    clock.advance(30)
    await ctx.watchdog.check()  # still stale: no second event
    assert stale_events(ctx) == [
        {"stale": False, "seconds_since_delivery": 0},
        {"stale": True, "seconds_since_delivery": 91},
    ]

    assert (await beat(http)).status_code == 200  # the next delivery
    assert ctx.stream_stale is False
    assert (await beat(http)).status_code == 200  # and a healthy stream publishes nothing more
    assert [e["stale"] for e in stale_events(ctx)] == [False, True, False]


async def test_a_logs_delivery_counts_like_a_heartbeat(
    ctx: AppContext, clock: Clock, http: httpx.AsyncClient
) -> None:
    body = flat(resolved_log(True))
    assert (await beat(http, body)).status_code == 200
    assert ctx.stream_stale is False


async def test_a_rejected_delivery_does_not_feed_the_watchdog(
    ctx: AppContext, clock: Clock, http: httpx.AsyncClient
) -> None:
    headers = sign(HEARTBEAT)
    headers["x-qn-signature"] = "00" * 32
    assert (await http.post(PATH, content=HEARTBEAT, headers=headers)).status_code == 401
    assert (
        await http.post(PATH, content=HEARTBEAT, headers=sign(HEARTBEAT, secret="x"))
    ).status_code == 401
    assert ctx.last_stream_delivery is None
    assert ctx.stream_stale is True


async def test_the_demo_route_forces_stale_and_releases_it(
    ctx: AppContext, clock: Clock, http: httpx.AsyncClient
) -> None:
    await beat(http)
    assert ctx.stream_stale is False

    forced = await http.post("/api/chain/stub/stream", json={"stale": True})
    assert forced.json()["status"] == "stale"
    assert ctx.stream_stale is True
    await beat(http)  # deliveries do not clear a forced state
    assert ctx.stream_stale is True

    released = await http.post("/api/chain/stub/stream", json={"stale": False})
    assert released.json()["status"] == "live"
    assert ctx.stream_stale is False
    assert [e["stale"] for e in stale_events(ctx)] == [False, True, False]


async def test_releasing_the_force_cannot_hide_a_real_silence(
    ctx: AppContext, clock: Clock, http: httpx.AsyncClient
) -> None:
    await beat(http)
    await http.post("/api/chain/stub/stream", json={"stale": True})
    clock.advance(200)
    released = await http.post("/api/chain/stub/stream", json={"stale": False})
    assert released.json()["status"] == "stale"
    assert ctx.stream_stale is True


async def test_the_forcing_route_stays_on_this_machine(real_app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app), base_url="http://example.com"
    ) as outside:
        response = await outside.post("/api/chain/stub/stream", json={"stale": True})
    assert response.status_code == 403


async def test_stubs_have_no_watchdog_and_the_route_still_publishes_directly() -> None:
    application = create_app(make_settings(), Database(None))
    stub_ctx: AppContext = application.state.ctx
    assert stub_ctx.watchdog is None and stub_ctx.stream_stale is False
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://localhost"
    ) as client:
        assert (await client.post("/api/chain/stub/stream", json={"stale": True})).json()[
            "status"
        ] == "stale"
    assert stub_ctx.stream_stale is True
    await stub_ctx.aclose()
