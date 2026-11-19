"""Shared fixtures. Tests never read the real `.env` and never reach the network."""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI

from app.config import Settings
from app.context import AppContext
from app.db import Database
from app.main import create_app

VOICE_TOKEN = "test-voice-token"
BOT_TOKEN = "test-bot-token"


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "use_p1_stubs": True,
        "trading_enabled": False,
        "calls_enabled": False,
        "voice_token": VOICE_TOKEN,
        "bot_token": BOT_TOKEN,
        "policy_transfer_cap_usd": Decimal("200"),
        "policy_daily_budget_usd": Decimal("500"),
        "passkey_threshold_usd": Decimal("1000"),
        "approval_expiry_s": 300,
        "max_calls_per_day": 5,
        "demo_time_scale": 1000.0,
        "anthropic_api_key": "",
        "openrouter_api_key": "",
        "openai_api_key": "",
        "google_ai_api_key": "",
        "xai_api_key": "",
        "elevenlabs_api_key": "",
        "call_provider": "dry_run",
        "agentphone_api_key": "",
        "agentphone_agent_id": "",
        "agentphone_number_id": "",
        "agentphone_webhook_secret": "",
        "owner_phone_number": "",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure nothing in the developer's environment leaks into a test."""
    for name in (
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_AI_API_KEY",
        "XAI_API_KEY",
        "ELEVENLABS_API_KEY",
        "AGENTPHONE_API_KEY",
        "AGENTPHONE_AGENT_ID",
        "AGENTPHONE_NUMBER_ID",
        "AGENTPHONE_WEBHOOK_SECRET",
        "CALL_PROVIDER",
        "OWNER_PHONE_NUMBER",
        "CALLS_ENABLED",
        "TRADING_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings, Database(None))
    yield application
    ctx: AppContext = application.state.ctx
    await ctx.aclose()


@pytest.fixture
def ctx(app: FastAPI) -> AppContext:
    context: AppContext = app.state.ctx
    return context


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        yield http


@pytest.fixture
def voice_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {VOICE_TOKEN}"}


@pytest.fixture
def bot_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {BOT_TOKEN}"}
