"""App factory, routers and lifespan.

Run with `uv run python -m app` from `server/`, or
`uv run uvicorn app.main:create_app --factory --port 8787`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import events, voice
from app.chain import stub as chain_stub
from app.config import REPO_ROOT, Settings, get_settings
from app.context import AppContext, Tasks, get_ctx
from app.db import Database
from app.events import EventBus, StoredEvent
from app.intent import IntentError, IntentParser, make_llm_call
from app.markets import stub as markets_stub
from app.planner import PlanError, Planner
from app.schemas import (
    ExecuteRequest,
    ExecuteResponse,
    FreezeRequest,
    IntentRequest,
    IntentResponse,
    OkResponse,
    StatsResponse,
    Venue,
    VenueList,
    WalletList,
)
from app.wallets import WalletBook

log = logging.getLogger("switchboard")

WEB_DIST = REPO_ROOT / "web" / "dist"

CtxDep = Annotated[AppContext, Depends(get_ctx)]

VENUES: list[Venue] = [
    Venue(id="polymarket", name="Polymarket US", status="fixtures", note="P1 client pending"),
    Venue(id="gemini", name="Gemini", status="fixtures", note="P1 client pending"),
    Venue(id="kalshi", name="Kalshi", status="planned", note="not connected yet"),
    Venue(id="limitless", name="Limitless", status="planned", note="not connected yet"),
    Venue(id="phantom", name="Phantom", status="planned", note="not connected yet"),
]


def build_context(settings: Settings, db: Database | None = None) -> AppContext:
    database = db or Database(settings.database_file)
    bus = EventBus(database)
    wallets = WalletBook()
    if not settings.use_p1_stubs:
        # P1's real modules are not on this branch yet. Fail loudly rather than pretend.
        raise RuntimeError(
            "USE_P1_STUBS=false, but P1's chain/ and markets/ modules are not wired in yet. "
            "Set USE_P1_STUBS=true, or wire the real modules into app.main.build_context."
        )
    tasks = Tasks()
    ctx = AppContext(
        settings=settings,
        db=database,
        bus=bus,
        tasks=tasks,
        wallets=wallets,
        vault=chain_stub.StubVault(bus, settings, wallets.allowlist()),
        transfers=chain_stub.StubTransfers(tasks.spawn, bus, settings),
        markets=markets_stub.StubMarkets(bus, settings),
        anchor=chain_stub.StubAnchor(),
    )

    async def track_stream(event: StoredEvent) -> None:
        ctx.stream_stale = bool(event.data.get("stale"))

    bus.on("stream.stale", track_stream)
    ctx.planner = Planner(ctx, IntentParser(wallets, make_llm_call(settings)))
    ctx.approvals = voice.Approvals(ctx)
    return ctx


def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    settings = settings or get_settings()
    ctx = build_context(settings, db)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log.info(
            "switchboard up: stubs=%s trading_enabled=%s calls_live=%s",
            settings.use_p1_stubs,
            settings.trading_enabled,
            settings.calls_live,
        )
        await ctx.approvals.resume()
        if settings.anthropic_api_key:
            # The first structured-output call compiles the schema and is slow; do it now.
            ctx.tasks.spawn(ctx.planner.warm_up(), "intent-warm-up")
        yield
        await ctx.aclose()

    app = FastAPI(
        title="Switchboard",
        version="0.1.0",
        description="Voice-driven money router with contract-enforced limits.",
        lifespan=lifespan,
    )
    app.state.ctx = ctx

    app.include_router(events.router)
    app.include_router(chain_stub.router)
    app.include_router(markets_stub.router)
    app.include_router(voice.router)
    app.include_router(voice.dashboard)

    @app.get("/api/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/intent", response_model=IntentResponse, tags=["plans"])
    async def intent(body: IntentRequest, ctx: CtxDep) -> IntentResponse:
        """Turn a sentence into a plan with quoted routes. Nothing is executed."""
        try:
            return await ctx.planner.create_plan(body.text)
        except IntentError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/plans/{plan_id}/execute", response_model=ExecuteResponse, tags=["plans"])
    async def execute(plan_id: str, body: ExecuteRequest, ctx: CtxDep) -> ExecuteResponse:
        """Ask the vault to spend. Returns executing, needs_approval, or blocked with a reason."""
        try:
            return await ctx.planner.execute(plan_id, body.route_id)
        except PlanError as exc:
            status = 404 if "unknown" in str(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @app.get("/api/stats", response_model=StatsResponse, tags=["meta"])
    async def stats(ctx: CtxDep) -> StatsResponse:
        """Budget remaining, approval rate, and the state of every safety switch."""
        policy = await ctx.vault.get_policy()
        spent = await ctx.vault.spent_today()
        return StatsResponse(
            policy=policy,
            spent_today_usd=spent,
            budget_remaining_usd=max(policy.daily_budget_usd - spent, Decimal(0)),
            approvals=ctx.approvals.stats(),
            calls_today=ctx.approvals.calls_in_last_day(),
            max_calls_per_day=ctx.settings.max_calls_per_day,
            frozen=await ctx.vault.is_frozen(),
            stream_stale=ctx.stream_stale,
            trading_enabled=ctx.settings.trading_enabled,
            max_order_usd=ctx.settings.effective_max_order_usd,
            calls_live=ctx.settings.calls_live,
            using_p1_stubs=ctx.settings.use_p1_stubs,
            passkey_threshold_usd=ctx.settings.passkey_threshold_usd,
            passkey_registered=False,
        )

    @app.post("/api/freeze", response_model=OkResponse, tags=["meta"])
    async def freeze(body: FreezeRequest, ctx: CtxDep) -> OkResponse:
        """Freeze or unfreeze the vault from the dashboard."""
        await ctx.vault.freeze(body.frozen, via="dashboard")
        return OkResponse(ok=True, status="frozen" if body.frozen else "live")

    @app.get("/api/wallets", response_model=WalletList, tags=["meta"])
    async def wallets(ctx: CtxDep) -> WalletList:
        return WalletList(wallets=ctx.wallets.wallets)

    @app.get("/api/venues", response_model=VenueList, tags=["meta"])
    async def venues() -> VenueList:
        """Prediction-market venues and how real each one is right now."""
        return VenueList(venues=VENUES)

    _mount_dashboard(app, WEB_DIST)
    return app


def _mount_dashboard(app: FastAPI, dist: Path) -> None:
    """Serve the built dashboard if `web/dist` exists (production). In development, Vite serves
    the dashboard and proxies /api and /events here."""
    index = dist / "index.html"
    if not index.is_file():
        return
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(index)
