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
from sqlmodel import func, select

from app import (
    bots,
    connectors,
    events,
    hostile,
    market_brief,
    mcp_http,
    passkey,
    rules,
    speech,
    voice,
)
from app.chain import escrow as escrow_routes
from app.chain import stream
from app.chain import stub as chain_stub
from app.chain.anchor import ChainAnchor
from app.chain.anomaly import AnomalyDetector
from app.chain.escrow import ChainEscrow
from app.chain.transfer import CctpTransfers
from app.chain.vault import ChainVault
from app.chain.watchdog import Watchdog
from app.config import REPO_ROOT, Settings, get_settings
from app.context import AppContext, Tasks, get_ctx
from app.db import Database, EventRow
from app.events import EventBus, StoredEvent
from app.forecast import Forecaster
from app.holds import HoldingVault
from app.intent import IntentError, IntentParser, make_llm_call
from app.local_only import LocalOnly
from app.markets import stub as markets_stub
from app.planner import PlanError, Planner
from app.ports import AnchorPort, VaultPort
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
        missing = [
            name.upper()
            for name in (
                "vault_address",
                "arc_rpc_url",
                "owner_private_key",
                "agent_private_key",
                "approver_private_key",
            )
            if not getattr(settings, name)
        ]
        if missing:
            raise RuntimeError(
                f"USE_P1_STUBS=false needs the real vault, but {', '.join(missing)} "
                "is not set. Set it in .env, or set USE_P1_STUBS=true."
            )
        if not settings.quicknode_stream_secret:
            log.warning(
                "QUICKNODE_STREAM_SECRET is not set: every Stream delivery will be rejected, "
                "so no approval.requested event will arrive"
            )
        log.warning(
            "real vault on chain %s; %smarkets are STUBS",
            settings.arc_chain_id,
            "" if settings.use_real_transfers else "transfers and ",
        )
    # Real transfers need the real vault: the stub vault pays nobody, so there is nothing to carry.
    real_transfers = settings.use_real_transfers and not settings.use_p1_stubs
    if settings.use_real_transfers and settings.use_p1_stubs:
        log.warning("USE_REAL_TRANSFERS is ignored while USE_P1_STUBS=true")
    tasks = Tasks()
    # The stubs number their requests, permits, transfers and moves from 1 in memory. On a
    # database that has been used before, start above everything it has recorded, so a number
    # from an earlier run is never handed out again (a reused request number would be taken
    # for a duplicate delivery and dropped).
    with database.session() as session:
        first_id = int(session.exec(select(func.max(EventRow.id))).one() or 0) + 1
    # Everything reaches the vault through this wrapper, which adds server-side holds for
    # connected assistants and passes every other call straight through.
    # Hold ids are "h<n>" and the contract's request ids are plain numbers, so they never meet.
    inner: VaultPort = (
        chain_stub.StubVault(bus, settings, wallets.allowlist(), first_id=first_id)
        if settings.use_p1_stubs
        else ChainVault(settings, wallets.allowlist(), bus)
    )
    anomaly = AnomalyDetector(settings, database, bus) if settings.anomaly_on else None
    vault = HoldingVault(inner, bus, first_id=first_id, anomaly=anomaly)
    anchor: AnchorPort = chain_stub.StubAnchor()
    escrow: ChainEscrow | None = None
    if not settings.use_p1_stubs:
        if settings.ledger_address:
            anchor = ChainAnchor(settings)
        else:
            log.warning("LEDGER_ADDRESS is not set: forecast receipts use the STUB anchor")
        if settings.escrow_address:
            escrow = ChainEscrow(settings)
        else:
            log.warning("ESCROW_ADDRESS is not set: /api/escrow routes answer 503")
    ctx = AppContext(
        settings=settings,
        db=database,
        bus=bus,
        tasks=tasks,
        wallets=wallets,
        vault=vault,
        transfers=(
            CctpTransfers(tasks.spawn, bus, settings, first_id=first_id)
            if real_transfers
            else chain_stub.StubTransfers(tasks.spawn, bus, settings, first_id=first_id)
        ),
        markets=markets_stub.StubMarkets(bus, settings, first_id=first_id),
        anchor=anchor,
        escrow=escrow,
    )

    async def track_stream(event: StoredEvent) -> None:
        ctx.stream_stale = bool(event.data.get("stale"))

    bus.on("stream.stale", track_stream)
    if not settings.use_p1_stubs:
        ctx.watchdog = Watchdog(ctx)  # stale until the Stream's first delivery
    ctx.holds = vault
    ctx.planner = Planner(ctx, IntentParser(wallets, make_llm_call(settings)))
    ctx.approvals = voice.Approvals(ctx)
    ctx.passkeys = passkey.Passkeys(ctx)
    ctx.forecaster = Forecaster(ctx)
    ctx.desk = market_brief.MarketDesk(ctx)
    ctx.rules = rules.Rules(ctx)
    ctx.hostile = hostile.HostileLab(ctx)
    ctx.bots = bots.Bots(ctx)
    ctx.speech = speech.Speech(ctx)
    ctx.connectors = connectors.Connectors(ctx)
    return ctx


def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    settings = settings or get_settings()
    ctx = build_context(settings, db)
    gateway = mcp_http.McpGateway(ctx)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log.info(
            "switchboard up: stubs=%s trading_enabled=%s calls_live=%s call_provider=%s",
            settings.use_p1_stubs,
            settings.trading_enabled,
            settings.calls_live,
            settings.call_provider,
        )
        if settings.call_provider != "dry_run" and not settings.calls_live:
            log.info(
                "calls stay in dry run until these are set: %s", ", ".join(settings.call_blockers)
            )
        await ctx.approvals.resume()
        if ctx.watchdog is not None:
            ctx.tasks.spawn(ctx.watchdog.run(), "stream-watchdog")
        if settings.anthropic_api_key:
            # The first structured-output call compiles the schema and is slow; do it now.
            ctx.tasks.spawn(ctx.planner.warm_up(), "intent-warm-up")
        # The MCP SDK answers requests inside a task group that lives as long as the app.
        async with gateway.server.session_manager.run():
            yield
        await ctx.aclose()

    app = FastAPI(
        title="Switchboard",
        version="0.1.0",
        description="Voice-driven money router with contract-enforced limits.",
        lifespan=lifespan,
    )
    app.state.ctx = ctx
    app.state.mcp = gateway
    app.add_middleware(LocalOnly)

    app.include_router(events.router)
    app.include_router(chain_stub.router)
    app.include_router(chain_stub.hook_router if settings.use_p1_stubs else stream.router)
    app.include_router(escrow_routes.router)
    app.include_router(markets_stub.router)
    app.include_router(voice.router)
    app.include_router(voice.dashboard)
    app.include_router(passkey.router)
    app.include_router(market_brief.router)
    app.include_router(market_brief.voice_router)
    app.include_router(rules.router)
    app.include_router(rules.voice_router)
    app.include_router(hostile.router)
    app.include_router(bots.router)
    app.include_router(bots.dashboard)
    app.include_router(speech.router)
    app.include_router(connectors.router)
    app.include_router(connectors.dashboard)
    app.include_router(mcp_http.router)

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
            call_provider=ctx.settings.call_provider,
            agent_tools_live=ctx.settings.agent_tools_live,
            using_p1_stubs=ctx.settings.use_p1_stubs,
            passkey_threshold_usd=ctx.settings.passkey_threshold_usd,
            passkey_registered=ctx.passkeys.registered(),
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
