"""Live smoke test against the real public endpoints. Skipped unless run with `-m live`:

    uv run pytest -m live tests/test_markets_live.py

Reads only (public GETs, no keys). Nothing here can place an order."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.db import Database
from app.events import EventBus
from app.markets.gemini import GeminiClient
from app.markets.polymarket import PolymarketClient
from app.markets.real import RealMarkets
from tests.conftest import make_settings

pytestmark = pytest.mark.live


class _Anchor:
    async def commit(self, commitment_hex: str, label: str) -> str:
        return "live-test"

    async def reveal(self, anchor_ref: str, preimage: bytes) -> None:
        pass


async def _live_nba_slugs(
    gem: GeminiClient, pm: PolymarketClient
) -> tuple[dict[str, Any], list[str]]:
    """A listed Gemini NBA game, and the Polymarket US game markets found by one team's name."""
    games = [
        e
        for e in await gem.list_events(search="NBA", max_events=100)
        if e["ticker"].startswith("NBA-") and e["ticker"].endswith("-M")
    ]
    assert games, "Gemini lists no NBA game right now"
    team = games[0]["participants"][0]["name"].split()[-1]
    found = await pm.search(team, limit=10)
    slugs = [m["slug"] for e in found for m in e["markets"] if m["slug"].startswith("aec-nba-")]
    assert slugs, f"Polymarket US search for {team!r} found no NBA game"
    return games[0], slugs


async def test_both_venues_answer_with_the_shapes_the_code_reads() -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        gem, pm = GeminiClient(http), PolymarketClient(http)
        event, slugs = await _live_nba_slugs(gem, pm)
        symbol = event["contracts"][0]["instrumentSymbol"]
        book = await gem.book(symbol)
        if book.best_bid and book.best_ask:
            assert Decimal(0) < book.best_bid.price <= book.best_ask.price < Decimal(1)

        market = await pm.market(slugs[0])
        assert market["slug"] == slugs[0] and market["feeCoefficient"] == 0.095
        pm_book = await pm.book(slugs[0])
        if pm_book.best_bid and pm_book.best_ask:
            assert pm_book.best_bid.price < pm_book.best_ask.price
        assert "bestBid" in await pm.bbo(slugs[0])


async def test_match_runs_on_live_listed_games() -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        _, slugs = await _live_nba_slugs(GeminiClient(http), PolymarketClient(http))
        real = RealMarkets(EventBus(Database(None)), make_settings(), _Anchor(), http=http)
        verdicts = [(await real.match("polymarket", s)).verdict for s in slugs[:3]]
        assert set(verdicts) <= {"match", "refuse"}
