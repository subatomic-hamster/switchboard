"""`RealMarkets` against mocked venues serving the recorded fixtures: match, quote, move,
listing, the cache, the fallback when a venue is down, and the wiring behind USE_REAL_MARKETS."""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from app.config import FIXTURES_DIR
from app.db import Database
from app.events import EventBus
from app.main import build_context, create_app
from app.markets.book import VenueError
from app.markets.gemini import GEMINI_API
from app.markets.polymarket import GATEWAY
from app.markets.real import RealMarkets
from app.markets.stub import GuardRefused, StubMarkets, UnknownMarket
from app.schemas import MarketMoveRequest
from tests.conftest import make_settings

NBA = "aec-nba-hou-den-2026-11-20"
BTC = "cpc-btc-above-day-2026-11-19-63000"
SFO = "tc-temp-sfohigh-2026-11-20-gte56lt57f"
CASES = [p.stem for p in (FIXTURES_DIR / "markets").glob("*.json") if "SYNTHETIC" not in p.stem]


def corpus() -> dict[str, Any]:
    docs = [
        json.loads((FIXTURES_DIR / "markets" / f"{name}.json").read_text(encoding="utf-8"))
        for name in CASES
    ]
    return {
        "markets": {m["slug"]: m for d in docs for m in d["polymarket"]},
        "events": {e["ticker"]: e for d in docs for e in d["gemini"]},
        "pm_books": {s: b for d in docs for s, b in d["books"]["polymarket"].items()},
        "gem_books": {s: b for d in docs for s, b in d["books"]["gemini"].items()},
    }


class Venues:
    """Routes for both venues, answering from the fixtures. `down` makes both unreachable."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.data = corpus()
        self.down = False
        self.hits: dict[str, int] = {}
        router.get(url__regex=rf"{GATEWAY}/v1/market/slug/(?P<slug>.+)").mock(
            side_effect=self.answer(
                lambda m: {"market": self.data["markets"][m["slug"]]}, "market", ""
            )
        )
        router.get(url__regex=rf"{GATEWAY}/v1/markets/(?P<slug>.+)/book").mock(
            side_effect=self.answer(lambda m: self.data["pm_books"][m["slug"]], "pm_book", "")
        )
        router.get(url__regex=rf"{GEMINI_API}/v1/prediction-markets/events/(?P<t>[^/?]+)").mock(
            side_effect=self.answer(lambda m: self.data["events"][m["t"]], "gem_event", "")
        )
        router.get(url__regex=rf"{GEMINI_API}/v1/prediction-markets/events(\?.*)?$").mock(
            side_effect=self.answer(self.events, "gem_events", "")
        )
        router.get(url__regex=rf"{GEMINI_API}/v1/book/(?P<s>.+)").mock(
            side_effect=self.answer(lambda m: self.data["gem_books"][m["s"]], "gem_book", "")
        )

    def events(self, _: Any) -> dict[str, Any]:
        rows = list(self.data["events"].values())
        return {"data": rows, "pagination": {"limit": 50, "offset": 0, "total": len(rows)}}

    def answer(
        self, build: Callable[[Any], Any], name: str, _: str
    ) -> Callable[..., httpx.Response]:
        def handler(request: httpx.Request, **groups: str) -> httpx.Response:
            self.hits[name] = self.hits.get(name, 0) + 1
            if self.down:
                raise httpx.ConnectError("venue unreachable")
            try:
                return httpx.Response(200, json=build(groups))
            except KeyError:
                return httpx.Response(404, json={"error": "not found"})

        return handler


class Anchor:
    def __init__(self) -> None:
        self.commits: list[tuple[str, str]] = []

    async def commit(self, commitment_hex: str, label: str) -> str:
        self.commits.append((commitment_hex, label))
        return "anchor"


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(http: httpx.AsyncClient, clock: Clock | None = None, **settings: Any) -> RealMarkets:
    return RealMarkets(
        EventBus(Database(None)),
        make_settings(**settings),
        Anchor(),
        http=http,
        clock=clock or Clock(),
        tracked=(NBA, SFO),
    )


@respx.mock
async def test_match_quote_and_listing_on_the_recorded_nba_pair() -> None:
    venues = Venues(respx.mock)
    async with httpx.AsyncClient() as http:
        real = make(http)
        result = await real.match("polymarket", NBA)
        assert result.verdict == "match"
        assert result.gemini_symbol == "GEMI-NBA-2611210300-HOU-DEN-M-HOU"
        quote = await real.quote("polymarket", NBA, Decimal(4))
        assert quote.sell.bid == Decimal("0.47") and quote.buy.ask == Decimal("0.48")
        await real.refresh()
    by_slug = {m.slug: m for m in real.listing()}
    nba = by_slug[NBA]
    assert nba.gemini is not None and nba.gemini.ask == Decimal("0.48")
    assert nba.polymarket.bid == Decimal("0.47") and nba.gemini_symbol
    assert nba.event_time is not None
    # The look-alike is listed without a Gemini price: the nearest band is a different bet.
    assert real.last_error.startswith(SFO)  # no book fixture for it, so it is skipped
    assert venues.hits["market"] >= 1


@respx.mock
async def test_a_refused_market_has_no_quote_and_cannot_be_moved() -> None:
    Venues(respx.mock)
    async with httpx.AsyncClient() as http:
        real = make(http)
        verdict = await real.match("polymarket", BTC)
        assert verdict.verdict == "refuse" and verdict.checks.source == "different"
        with pytest.raises(UnknownMarket, match="different settlement source"):
            await real.quote("polymarket", BTC, Decimal(1))
        with pytest.raises(GuardRefused, match="not equivalent"):
            await real.move(MarketMoveRequest(slug=BTC, qty=Decimal(1)))
    assert real.engine.records == {}


@respx.mock
async def test_an_unknown_slug_is_unknown_not_a_crash() -> None:
    Venues(respx.mock)
    async with httpx.AsyncClient() as http:
        with pytest.raises(UnknownMarket):
            await make(http).match("polymarket", "no-such-market")
        with pytest.raises(UnknownMarket, match="not supported"):
            await make(http).match("gemini", NBA)


@respx.mock
async def test_move_is_a_dry_run_while_trading_is_disabled() -> None:
    Venues(respx.mock)
    anchor_calls = 0
    async with httpx.AsyncClient() as http:
        real = make(http)
        move_id = await real.move(MarketMoveRequest(slug=NBA, qty=Decimal(2), mode="take"))
        rec = real.engine.records[move_id]
        anchor_calls = len(real.engine._anchor.commits)  # type: ignore[attr-defined]
    assert (rec.status, rec.dry_run) == ("dry_run", True)
    assert rec.gemini_symbol == "GEMI-NBA-2611210300-HOU-DEN-M-HOU"
    assert anchor_calls == 1


@respx.mock
async def test_responses_are_cached_for_the_ttl_then_refetched() -> None:
    venues = Venues(respx.mock)
    clock = Clock()
    async with httpx.AsyncClient() as http:
        real = make(http, clock)
        await real.quote("polymarket", NBA, Decimal(1))
        first = dict(venues.hits)
        await real.quote("polymarket", NBA, Decimal(1))
        assert venues.hits == first  # all from cache
        clock.now += 60
        await real.quote("polymarket", NBA, Decimal(1))
        assert venues.hits["pm_book"] == first["pm_book"] + 1


@respx.mock
async def test_a_down_venue_serves_the_last_good_value_then_a_clear_error() -> None:
    venues = Venues(respx.mock)
    clock = Clock()
    async with httpx.AsyncClient() as http:
        real = make(http, clock)
        good = await real.quote("polymarket", NBA, Decimal(1))
        venues.down = True
        clock.now += 40  # past every TTL (books: 5 s), inside ten times each
        assert await real.quote("polymarket", NBA, Decimal(1)) == good
        clock.now += 10_000  # too old to serve
        with pytest.raises(VenueError, match=r"polymarket|gemini"):
            await real.quote("polymarket", NBA, Decimal(1))


@respx.mock
async def test_a_venue_down_from_the_start_is_a_503_naming_the_venue() -> None:
    venues = Venues(respx.mock)
    venues.down = True
    application = create_app(make_settings(use_real_markets=True), Database(None))
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        resp = await client.get("/api/markets/match", params={"slug": NBA})
        assert resp.status_code == 503
        body = resp.json()
        assert body["venue"] == "polymarket" and "unreachable" in body["detail"]
        assert (await client.get("/api/health")).status_code == 200
    await application.state.ctx.aclose()
    await application.state.ctx.markets.aclose()


@respx.mock
async def test_the_routes_work_end_to_end_with_real_markets_on() -> None:
    Venues(respx.mock)
    application = create_app(make_settings(use_real_markets=True), Database(None))
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        match = (await client.get("/api/markets/match", params={"slug": NBA})).json()
        assert match["verdict"] == "match"
        quote = (await client.get("/api/markets/quote", params={"slug": NBA, "qty": 4})).json()
        assert quote["sell"]["bid"] == 0.47 and quote["bid_depth"] > 0
        moved = await client.post("/api/markets/move", json={"slug": NBA, "qty": 2, "mode": "take"})
        assert moved.status_code == 200 and moved.json()["move_id"] == "m_1"
        over = await client.post("/api/markets/move", json={"slug": NBA, "qty": 50})
        assert over.status_code == 409 and "MAX_ORDER_USD" in over.json()["detail"]
        venues = (await client.get("/api/venues")).json()["venues"]
        by_id = {v["id"]: v for v in venues}
        assert by_id["polymarket"]["status"] == "connected" and "dry-run" in by_id["gemini"]["note"]
    await application.state.ctx.aclose()
    await application.state.ctx.markets.aclose()


def test_the_default_is_still_the_stub_and_the_flag_selects_real_markets() -> None:
    stub_ctx = build_context(make_settings(), Database(None))
    assert isinstance(stub_ctx.markets, StubMarkets)
    real_ctx = build_context(make_settings(use_real_markets=True), Database(None))
    assert isinstance(real_ctx.markets, RealMarkets)
    assert type(real_ctx.vault).__name__ == "HoldingVault"  # P1 stubs untouched by the flag
    assert real_ctx.settings.trading_enabled is False
