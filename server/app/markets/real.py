"""`RealMarkets`: satisfies `app.ports.MarketsPort` on live public data from Polymarket US and
Gemini, behind `USE_REAL_MARKETS`. Replaces `StubMarkets` with the same methods.

- `match`, `quote` and `listing` use only public endpoints (no keys). Responses are kept for a
  short TTL so the dashboard does not hammer the venues.
- `move` goes through `app.markets.move.MoveEngine`: dry run unless `TRADING_ENABLED` and all
  four keys are set, and never above `effective_max_order_usd`.
- A venue that cannot be reached raises `VenueError` naming it (the app maps it to a 503).
  `listing()` is synchronous in the port, so it returns the last good snapshot, refreshed by
  `refresh_loop()` in the background; `last_error` says why a refresh failed.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any, TypeVar

import httpx

from app.config import Settings
from app.events import EventBus
from app.markets.book import Book, VenueError
from app.markets.gemini import GeminiClient
from app.markets.match import (
    MatchResult,
    PriceEvent,
    SportsEvent,
    WeatherEvent,
    describe_polymarket,
    evaluate,
)
from app.markets.move import MoveEngine
from app.markets.polymarket import Connector, PolymarketClient
from app.markets.quote import build_quote
from app.markets.stub import GuardRefused, UnknownMarket
from app.ports import AnchorPort
from app.schemas import (
    MarketListing,
    MarketMatchResponse,
    MarketMoveRequest,
    MarketQuoteResponse,
    VenuePrices,
)

log = logging.getLogger("switchboard.markets")
T = TypeVar("T")

# Markets shown on the dashboard until discovery finds live cross-listed games. The first two
# are the plan's reference pairs; the third is the look-alike that must be refused. Pairs from
# a finished game simply fail to load and are skipped.
DEFAULT_TRACKED = (
    "aec-nba-hou-den-2026-11-20",
    "atc-lal-get-atm-2026-11-22-atm",
    "tc-temp-sfohigh-2026-11-20-gte56lt57f",
)
TRACKED_CAP = 12


class RealMarkets:
    def __init__(
        self,
        bus: EventBus,
        settings: Settings,
        anchor: AnchorPort,
        *,
        http: httpx.AsyncClient | None = None,
        connector: Connector | None = None,
        first_id: int = 1,
        ttl_s: float = 15.0,
        refresh_s: float = 30.0,
        tracked: tuple[str, ...] = DEFAULT_TRACKED,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(headers={"User-Agent": "switchboard-markets/0.1"})
        self._pm = PolymarketClient(self._http)  # public reads only: no keys, never live
        self._gem = GeminiClient(self._http)
        self.engine = MoveEngine(
            bus, settings, anchor, self._http, connector=connector, first_id=first_id
        )
        self._ttl, self._refresh_s, self._clock = ttl_s, refresh_s, clock
        self._cache: dict[str, tuple[float, Any]] = {}
        self._tracked: list[str] = list(tracked)
        self._listing: list[MarketListing] = []
        self.last_error: str = ""

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # --------------------------------------------------------------------- cache

    async def _cached(self, key: str, ttl: float, fetch: Callable[[], Awaitable[T]]) -> T:
        """A value fetched within `ttl` seconds. When the venue fails, a stale value (up to ten
        times older) is served rather than an error; with none, the error is raised."""
        now = self._clock()
        hit = self._cache.get(key)
        if hit is not None and now - hit[0] < ttl:
            value: T = hit[1]
            return value
        try:
            fresh = await fetch()
        except VenueError as exc:
            if hit is not None and now - hit[0] < ttl * 10:
                log.warning("%s; serving a value %.0fs old", exc, now - hit[0])
                stale: T = hit[1]
                return stale
            raise
        self._cache[key] = (now, fresh)
        return fresh

    async def _pm_market(self, slug: str) -> dict[str, Any]:
        return await self._cached(f"pm:{slug}", self._ttl, lambda: self._pm.market(slug))

    async def _pm_book(self, slug: str) -> Book:
        return await self._cached(f"pmb:{slug}", self._ttl / 3, lambda: self._pm.book(slug))

    async def _gem_book(self, symbol: str) -> Book:
        return await self._cached(f"gb:{symbol}", self._ttl / 3, lambda: self._gem.book(symbol))

    # --------------------------------------------------------------------- match

    async def _gemini_events(self, pm_market: dict[str, Any]) -> MatchResult:
        """Fetch the Gemini events that could be the same thing, and compare."""
        pm = describe_polymarket(pm_market)
        event = pm.event
        if isinstance(event, SportsEvent):
            return await self._sports(pm_market)
        if isinstance(event, PriceEvent) and pm.time is not None:
            ticker = f"{event.asset.upper()}{pm.time:%y%m%d%H%M}"
            if event.style == "up_down":
                # Gemini's interval series: BTC05M + the window's end, e.g. BTC05M2611201505.
                horizon = str((pm_market.get("assetPriceTerms") or {}).get("horizon", ""))
                m = re.fullmatch(r"(\d+)m", horizon.lower())
                ticker = f"{event.asset.upper()}{int(m[1]):02d}M{pm.time:%y%m%d%H%M}" if m else ""
            try:
                found = (
                    [await self._cached(f"ge:{ticker}", 60, lambda: self._gem.get_event(ticker))]
                    if ticker
                    else []
                )
            except UnknownMarket:
                found = []
            return evaluate(pm_market, found)
        if isinstance(event, WeatherEvent):
            events = await self._cached(
                "gl:Weather", 60, lambda: self._gem.list_events(category="Weather")
            )
            return evaluate(pm_market, events)
        return evaluate(pm_market, [])

    async def _sports(self, pm_market: dict[str, Any]) -> MatchResult:
        """Search Gemini by each team's name words and abbreviation until a candidate game turns
        up (its text search is accent- and word-sensitive)."""
        terms: list[str] = []
        for side in pm_market.get("marketSides", []):
            team = side.get("team") or {}
            words = [
                w for w in str(team.get("name", "")).split() if len(w) > 3 and w.lower() != "club"
            ]
            for term in [*words, str(team.get("abbreviation", "")).upper()]:
                if term and term not in terms:
                    terms.append(term)
        events: dict[str, dict[str, Any]] = {}
        result = evaluate(pm_market, [])
        for term in terms[:6]:
            found = await self._cached(
                f"gs:{term}",
                60,
                lambda t=term: self._gem.list_events(search=t, max_events=150),  # type: ignore[misc]
            )
            events.update({e["ticker"]: e for e in found})
            result = evaluate(pm_market, list(events.values()))
            if result.response.checks.event != "unknown":
                break
        return result

    async def _match(self, slug: str, *, fresh: bool = False) -> MatchResult:
        if fresh:
            self._cache.pop(f"pm:{slug}", None)
        market = await self._pm_market(slug)
        return await self._cached(
            f"match:{slug}", 0 if fresh else self._ttl * 2, lambda: self._gemini_events(market)
        )

    async def match(self, venue: str, slug: str) -> MarketMatchResponse:
        if venue != "polymarket":
            raise UnknownMarket(f"matching from {venue!r} is not supported; use venue=polymarket")
        return (await self._match(slug)).response

    # --------------------------------------------------------------------- quote

    async def quote(self, from_venue: str, slug: str, qty: Decimal) -> MarketQuoteResponse:
        result = await self._match(slug)
        if result.response.verdict != "match" or result.response.gemini_symbol is None:
            raise UnknownMarket(f"{slug} has no equivalent contract on Gemini: {self._why(result)}")
        pm_book = await self._pm_book(slug)
        gem_book = await self._gem_book(result.response.gemini_symbol)
        return build_quote(pm_book, gem_book, qty, tick=await self._tick(slug))

    async def _tick(self, slug: str) -> Decimal:
        market = await self._pm_market(slug)
        return Decimal(str(market.get("orderPriceMinTickSize") or "0.01"))

    @staticmethod
    def _why(result: MatchResult) -> str:
        return "; ".join(result.response.notes) or "not verified"

    # ---------------------------------------------------------------------- move

    async def move(self, request: MarketMoveRequest) -> str:
        result = await self._match(request.slug, fresh=True)
        if result.response.verdict != "match" or result.response.gemini_symbol is None:
            raise GuardRefused(f"the two markets are not equivalent: {self._why(result)}")
        return await self.engine.execute(
            request, result.response.gemini_symbol, await self._tick(request.slug)
        )

    # ------------------------------------------------------------------- listing

    def listing(self) -> list[MarketListing]:
        return list(self._listing)

    async def refresh(self) -> None:
        """Rebuild the side-by-side listing for every tracked market. One failing market
        does not stop the others; the last good snapshot stays if all fail."""
        rows: list[MarketListing] = []
        errors: list[str] = []
        for slug in list(self._tracked):
            try:
                rows.append(await self._listing_for(slug))
            except (VenueError, UnknownMarket) as exc:
                errors.append(f"{slug}: {exc}")
        self.last_error = "; ".join(errors)
        if rows or not errors:
            self._listing = rows
        if errors:
            log.warning("markets refresh: %s", self.last_error)

    async def _listing_for(self, slug: str) -> MarketListing:
        market = await self._pm_market(slug)
        result = await self._match(slug)
        pm_book = await self._pm_book(slug)
        if pm_book.best_bid is None or pm_book.best_ask is None:
            raise UnknownMarket(f"{slug} has no two-sided book")
        symbol = result.response.gemini_symbol
        checks = result.response.checks
        if checks.strike == "different" or checks.direction == "different":
            symbol = None  # the nearest contract is a different bet: do not show its price
        gem: VenuePrices | None = None
        if symbol:
            book = await self._gem_book(symbol)
            if book.best_bid and book.best_ask:
                gem = VenuePrices(
                    bid=book.best_bid.price, ask=book.best_ask.price, bid_depth=book.best_bid.qty
                )
        return MarketListing(
            slug=slug,
            title=str(market.get("question", slug)),
            kind=str(market.get("sportsMarketType") or market.get("marketType") or "").replace(
                "_", " "
            ),
            event_time=result.pm.time,
            gemini_symbol=symbol,
            polymarket=VenuePrices(
                bid=pm_book.best_bid.price,
                ask=pm_book.best_ask.price,
                bid_depth=pm_book.best_bid.qty,
            ),
            gemini=gem,
        )

    async def refresh_loop(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:
                log.exception("markets refresh failed")
            await asyncio.sleep(self._refresh_s)

    def track(self, slug: str) -> None:
        """Add a market to the listing (oldest dropped past the cap)."""
        if slug not in self._tracked:
            self._tracked.append(slug)
            del self._tracked[: max(0, len(self._tracked) - TRACKED_CAP)]
