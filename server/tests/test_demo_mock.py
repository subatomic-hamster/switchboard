"""The mock demo runs end to end against the app and produces every kind of event it promises."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI

from app.context import AppContext
from app.db import Database
from app.demo_mock import Demo
from app.main import create_app
from tests.conftest import make_settings


async def test_mock_demo_replays_a_full_day(app: FastAPI, ctx: AppContext) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        await Demo(client, pace=0).run()
    await asyncio.sleep(0.2)

    types = [event.type for event in ctx.bus.history()]
    for expected in (
        "transfer.sent",
        "transfer.arrived",
        "approval.requested",
        "call.started",
        "approval.resolved",
        "trade.posted",
        "trade.filled",
        "stream.stale",
        "blocked",
        "frozen",
    ):
        assert expected in types, f"{expected} missing from the demo"

    resolved = [event.data for event in ctx.bus.history() if event.type == "approval.resolved"]
    assert [item["approved"] for item in resolved] == [True, False]
    blocked = [event.data["reason"] for event in ctx.bus.history() if event.type == "blocked"]
    assert blocked == ["STREAM_STALE", "FROZEN"]
    # 150 + 250 (approved) + 40 after the unfreeze; the denied and blocked ones moved nothing.
    assert await ctx.vault.spent_today() == Decimal("440")
    assert await ctx.vault.is_frozen() is False
    assert ctx.stream_stale is False
    assert all(event.data["dry_run"] for event in ctx.bus.history() if event.type == "trade.filled")


async def test_mock_demo_refuses_to_run_when_calls_are_live() -> None:
    application = create_app(
        make_settings(
            calls_enabled=True,
            call_provider="elevenlabs_twilio",
            elevenlabs_api_key="k",
            elevenlabs_agent_id="a",
            elevenlabs_phone_number_id="p",
            owner_phone_number="+16045550123",
        ),
        Database(None),
    )
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        with pytest.raises(SystemExit, match="only runs in dry run"):
            await Demo(client, pace=0).run()
    ctx: AppContext = application.state.ctx
    assert ctx.bus.history() == []
    await ctx.aclose()
