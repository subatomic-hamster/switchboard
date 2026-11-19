"""Phase 0: the skeleton, the event bus, and the stubbed P1 routes."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.context import AppContext
from app.db import Database
from app.events import StoredEvent, event_stream
from app.main import create_app
from app.schemas import (
    EVENT_PAYLOADS,
    FROZEN_EVENT_TYPES,
    BlockedData,
    FrozenData,
    MarketQuoteResponse,
    Reason,
    VoiceApproveRequest,
)
from tests.conftest import make_settings


async def test_health(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_frozen_event_names_are_all_defined() -> None:
    assert set(FROZEN_EVENT_TYPES) <= set(EVENT_PAYLOADS)
    assert FROZEN_EVENT_TYPES == (
        "transfer.sent",
        "transfer.arrived",
        "approval.requested",
        "approval.resolved",
        "blocked",
        "trade.posted",
        "trade.filled",
        "stream.stale",
        "frozen",
    )


def test_money_is_decimal_in_python_and_a_number_on_the_wire() -> None:
    body = VoiceApproveRequest.model_validate(
        {"request_id": "7", "amount_spoken": 250, "challenge": "harbor"}
    )
    assert isinstance(body.amount_spoken, Decimal)
    assert body.amount_spoken == Decimal("250")
    assert VoiceApproveRequest.model_validate(
        {"request_id": "7", "amount_spoken": "250.40", "challenge": "x"}
    ).amount_spoken == Decimal("250.40")
    wire = json.loads(body.model_dump_json())
    assert wire["amount_spoken"] == 250


async def test_bus_stores_delivers_and_replays(ctx: AppContext) -> None:
    seen: list[StoredEvent] = []

    async def handler(event: StoredEvent) -> None:
        seen.append(event)

    ctx.bus.on("frozen", handler)
    first = await ctx.bus.publish("frozen", FrozenData(frozen=True, via="test"))
    second = await ctx.bus.publish("frozen", {"frozen": False})
    assert [event.id for event in seen] == [first.id, second.id]
    assert seen[0].data == {"frozen": True, "via": "test"}
    assert [event.id for event in ctx.bus.history()] == [first.id, second.id]
    assert [event.id for event in ctx.bus.history(after_id=first.id)] == [second.id]
    latest = ctx.bus.latest("frozen")
    assert latest is not None and latest.id == second.id


async def test_bus_rejects_unknown_types_and_bad_payloads(ctx: AppContext) -> None:
    with pytest.raises(ValueError, match="unknown event type"):
        await ctx.bus.publish("made.up", {})
    with pytest.raises(ValueError):
        await ctx.bus.publish("frozen", {"frozen": "not-a-bool-at-all"})
    assert ctx.bus.history() == []


async def test_a_failing_handler_does_not_stop_the_others(ctx: AppContext) -> None:
    seen: list[int] = []

    async def bad(event: StoredEvent) -> None:
        raise RuntimeError("boom")

    async def good(event: StoredEvent) -> None:
        seen.append(event.id)

    ctx.bus.on("blocked", bad)
    ctx.bus.on("blocked", good)
    event = await ctx.bus.publish("blocked", BlockedData(reason=Reason.FROZEN))
    assert seen == [event.id]


async def test_sse_stream_replays_then_goes_live(ctx: AppContext) -> None:
    old = await ctx.bus.publish("frozen", FrozenData(frozen=True))
    missed = await ctx.bus.publish("frozen", FrozenData(frozen=False))
    stream = event_stream(ctx.bus, after_id=old.id)
    first = await asyncio.wait_for(anext(stream), timeout=1)
    assert first["id"] == str(missed.id)
    assert first["event"] == "frozen"
    assert json.loads(first["data"])["data"] == {"frozen": False, "via": ""}

    live = await ctx.bus.publish("frozen", FrozenData(frozen=True))
    second = await asyncio.wait_for(anext(stream), timeout=1)
    assert second["id"] == str(live.id)
    await stream.aclose()


async def test_history_route_returns_typed_events(
    client: httpx.AsyncClient, ctx: AppContext
) -> None:
    await ctx.bus.publish("frozen", FrozenData(frozen=True, via="voice"))
    response = await client.get("/api/events/history")
    assert response.status_code == 200
    events = response.json()["events"]
    assert len(events) == 1
    assert events[0]["type"] == "frozen"
    assert events[0]["data"] == {"frozen": True, "via": "voice"}


async def test_stub_chain_transfer_emits_sent_then_arrived(
    client: httpx.AsyncClient, ctx: AppContext
) -> None:
    response = await client.post(
        "/api/chain/transfer",
        json={
            "from_chain": "eth-sepolia",
            "to_chain": "arc-testnet",
            "to": "0x2222222222222222222222222222222222222222",
            "amount_usd": 200,
        },
    )
    assert response.status_code == 200
    transfer_id = response.json()["transfer_id"]
    assert transfer_id.startswith("t_")
    await asyncio.sleep(0.2)
    types = [(event.type, event.data["transfer_id"]) for event in ctx.bus.history()]
    assert types == [("transfer.sent", transfer_id), ("transfer.arrived", transfer_id)]


async def test_stub_market_match_and_refusal(client: httpx.AsyncClient) -> None:
    match = await client.get(
        "/api/markets/match", params={"venue": "polymarket", "slug": "aec-nba-hou-den-2026-11-20"}
    )
    assert match.status_code == 200
    body = match.json()
    assert body["verdict"] == "match"
    assert body["gemini_symbol"] == "GEMI-NBA-2611210300-HOU-DEN-M-HOU"
    assert body["checks"] == {
        "event": "same",
        "strike": "n/a",
        "direction": "same",
        "time": "same",
        "source": "same",
    }

    refuse = await client.get(
        "/api/markets/match", params={"slug": "cpc-btc-above-day-2026-11-19-63000"}
    )
    assert refuse.json()["verdict"] == "refuse"
    assert refuse.json()["checks"]["source"] == "different"

    unknown = await client.get("/api/markets/match", params={"slug": "nope"})
    assert unknown.status_code == 404


async def test_stub_market_quote_uses_the_documented_fee_formula(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(
        "/api/markets/quote",
        params={"from": "polymarket", "slug": "aec-nba-hou-den-2026-11-20", "qty": 4},
    )
    assert response.status_code == 200
    quote = MarketQuoteResponse.model_validate(response.json())
    # Polymarket US: 0.095 x 4 x 0.45 x 0.55 = 0.094; Gemini: 0.07 x 4 x 0.46 x 0.54 = 0.070
    assert quote.sell.bid == Decimal("0.45")
    assert quote.sell.post_at == Decimal("0.46")
    assert quote.sell.taker_fee == Decimal("0.09")
    assert quote.buy.ask == Decimal("0.46")
    assert quote.buy.taker_fee == Decimal("0.07")
    assert quote.bid_depth == Decimal("120")
    # (1.84 + 0.0696) - (1.80 - 0.0941) = 0.2037 over 1.80 = 11.3%
    assert quote.move_cost_pct == Decimal("11.3")


async def test_stub_market_move_never_trades_and_respects_the_order_cap(
    client: httpx.AsyncClient, ctx: AppContext
) -> None:
    over = await client.post(
        "/api/markets/move", json={"slug": "aec-nba-hou-den-2026-11-20", "qty": 10, "mode": "post"}
    )
    assert over.status_code == 409
    assert "MAX_ORDER_USD" in over.json()["detail"]

    lookalike = await client.post(
        "/api/markets/move",
        json={"slug": "cpc-btc-above-day-2026-11-19-63000", "qty": 1, "mode": "post"},
    )
    assert lookalike.status_code == 409

    ok = await client.post(
        "/api/markets/move", json={"slug": "aec-nba-hou-den-2026-11-20", "qty": 4, "mode": "post"}
    )
    assert ok.status_code == 200
    events = [event for event in ctx.bus.history() if event.type.startswith("trade.")]
    assert [event.type for event in events] == [
        "trade.posted",
        "trade.filled",
        "trade.posted",
        "trade.filled",
    ]
    assert all(event.data["dry_run"] is True for event in events)


async def test_stub_stream_toggle_sets_the_stale_flag(
    client: httpx.AsyncClient, ctx: AppContext
) -> None:
    assert ctx.stream_stale is False
    await client.post("/api/chain/stub/stream", json={"stale": True})
    assert ctx.stream_stale is True
    await client.post("/api/chain/stub/stream", json={"stale": False})
    assert ctx.stream_stale is False


async def test_venues_label_unconnected_ones_as_planned(client: httpx.AsyncClient) -> None:
    venues = {v["id"]: v["status"] for v in (await client.get("/api/venues")).json()["venues"]}
    assert venues["kalshi"] == "planned"
    assert venues["gemini"] in {"fixtures", "connected"}


async def test_a_restart_on_the_same_database_does_not_reuse_request_numbers(
    tmp_path: Path,
) -> None:
    """The stub vault counts in memory. If it began again at 1 after a restart, a new request
    would look like a repeat of an old one and no approval would be raised for it."""
    sentence = "send 250 from my arc wallet to sam's arc wallet"
    seen: list[str] = []
    for _ in range(2):
        database = Database(tmp_path / "switchboard.sqlite")
        application = create_app(make_settings(), database)
        ctx: AppContext = application.state.ctx
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
            plan = (await http.post("/api/intent", json={"text": sentence})).json()
            body = (await http.post(f"/api/plans/{plan['plan_id']}/execute", json={})).json()
            assert body["status"] == "needs_approval"
            request_id = str(body["request_id"])
            row = ctx.approvals.get(request_id)
            assert row is not None and row.status == "pending"
            pending = [a for a in ctx.approvals.views() if a.status == "pending"]
            assert [a.request_id for a in pending] == [request_id]
            seen.append(request_id)
            await ctx.approvals.deny(request_id, via="dashboard")
            await asyncio.sleep(0.01)  # let the expiry timer start before shutdown
        await ctx.aclose()
    assert len(set(seen)) == 2
