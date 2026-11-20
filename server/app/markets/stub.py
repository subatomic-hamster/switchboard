"""STUB. Stands in for P1's `markets/` modules until they exist.

Answers from `server/fixtures/markets.json`, a snapshot of the pairs in the plan's reference
table. It never contacts a venue and never places an order: `move` only publishes
`trade.posted` and `trade.filled` events flagged `dry_run`.

P1: replace by providing an object that satisfies `app.ports.MarketsPort` and a router with the
same three routes, then set USE_P1_STUBS=false.
"""

from __future__ import annotations

import itertools
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import FIXTURES_DIR, Settings
from app.context import AppContext, get_ctx
from app.events import EventBus
from app.schemas import (
    BuyQuote,
    MarketListing,
    MarketMatchResponse,
    MarketMoveRequest,
    MarketMoveResponse,
    MarketQuoteResponse,
    MatchChecks,
    SellQuote,
    TradeData,
    VenuePrices,
)

CENT = Decimal("0.01")
TICK = Decimal("0.01")


class UnknownMarket(LookupError):
    pass


class GuardRefused(PermissionError):
    """Raised when the real-money guards refuse a move."""


def taker_fee(rate: Decimal, qty: Decimal, price: Decimal) -> Decimal:
    """Both venues: rate x contracts x price x (1 - price)."""
    return rate * qty * price * (1 - price)


class StubMarkets:
    """STUB of the venue clients, the equivalence check and the move."""

    def __init__(
        self, bus: EventBus, settings: Settings, path: Path | None = None, first_id: int = 1
    ) -> None:
        self._bus = bus
        self._settings = settings
        raw = json.loads((path or FIXTURES_DIR / "markets.json").read_text(encoding="utf-8"))
        self._fees = {key: Decimal(value) for key, value in raw["fees"].items()}
        self._markets: dict[str, dict[str, Any]] = {m["slug"]: m for m in raw["markets"]}
        self._ids = itertools.count(first_id)

    def listing(self) -> list[MarketListing]:
        """Both venues' prices for every market in the fixture, for the side-by-side view."""
        return [
            MarketListing(
                slug=market["slug"],
                title=market["title"],
                kind=market["kind"],
                event_time=market.get("event_time"),
                gemini_symbol=market["gemini_symbol"],
                polymarket=VenuePrices(**market["polymarket"]),
                gemini=VenuePrices(**market["gemini"]) if market["gemini"] else None,
            )
            for market in self._markets.values()
        ]

    def _get(self, slug: str) -> dict[str, Any]:
        market = self._markets.get(slug)
        if market is None:
            raise UnknownMarket(slug)
        return market

    async def match(self, venue: str, slug: str) -> MarketMatchResponse:
        market = self._get(slug)
        return MarketMatchResponse(
            verdict=market["verdict"],
            checks=MatchChecks(**market["checks"]),
            gemini_symbol=market["gemini_symbol"],
            notes=list(market["notes"]),
        )

    async def quote(self, from_venue: str, slug: str, qty: Decimal) -> MarketQuoteResponse:
        """Cost of selling `qty` at the Polymarket US bid and buying it at the Gemini ask,
        paying the taker fee on both sides, as a percentage of the sale value."""
        market = self._get(slug)
        if market["gemini"] is None:
            raise UnknownMarket(f"{slug} has no equivalent contract on Gemini")
        bid = Decimal(market["polymarket"]["bid"])
        ask = Decimal(market["gemini"]["ask"])
        sell_fee = taker_fee(self._fees["polymarket_taker_rate"], qty, bid)
        buy_fee = taker_fee(self._fees["gemini_taker_rate"], qty, ask)
        proceeds = bid * qty - sell_fee
        outlay = ask * qty + buy_fee
        cost_pct = (outlay - proceeds) / (bid * qty) * 100
        return MarketQuoteResponse(
            sell=SellQuote(
                bid=bid,
                post_at=min(bid + TICK, Decimal(market["polymarket"]["ask"])),
                taker_fee=sell_fee.quantize(CENT, rounding=ROUND_HALF_UP),
            ),
            buy=BuyQuote(ask=ask, taker_fee=buy_fee.quantize(CENT, rounding=ROUND_HALF_UP)),
            move_cost_pct=cost_pct.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP),
            bid_depth=Decimal(market["polymarket"]["bid_depth"]),
        )

    async def move(self, request: MarketMoveRequest) -> str:
        """STUB: never trades. Applies the guards the real move will, then emits dry-run
        events so the dashboard and the operator voice can be shown."""
        market = self._get(request.slug)
        if market["verdict"] != "match" or market["gemini"] is None:
            raise GuardRefused("the two markets are not equivalent")
        bid = Decimal(market["polymarket"]["bid"])
        price = (
            bid if request.mode == "take" else min(bid + TICK, Decimal(market["polymarket"]["ask"]))
        )
        notional = price * request.qty
        if notional > self._settings.effective_max_order_usd:
            raise GuardRefused(
                f"order of ${notional:.2f} is over MAX_ORDER_USD "
                f"(${self._settings.effective_max_order_usd})"
            )
        move_id = f"m_{next(self._ids)}"
        sell = TradeData(
            move_id=move_id,
            venue="polymarket",
            slug=request.slug,
            side="sell",
            qty=request.qty,
            price=price,
            dry_run=True,
        )
        buy = TradeData(
            move_id=move_id,
            venue="gemini",
            slug=str(market["gemini_symbol"]),
            side="buy",
            qty=request.qty,
            price=Decimal(market["gemini"]["ask"]),
            dry_run=True,
        )
        await self._bus.publish("trade.posted", sell)
        await self._bus.publish("trade.filled", sell)
        await self._bus.publish("trade.posted", buy)
        await self._bus.publish("trade.filled", buy)
        return move_id


router = APIRouter(prefix="/api/markets", tags=["markets (STUB)"])
Ctx = Annotated[AppContext, Depends(get_ctx)]


@router.get("/match", response_model=MarketMatchResponse)
async def market_match(
    ctx: Ctx,
    slug: Annotated[str, Query(min_length=1)],
    venue: str = "polymarket",
) -> MarketMatchResponse:
    """STUB. The five-field equivalence check, answered from fixtures."""
    try:
        return await ctx.markets.match(venue, slug)
    except UnknownMarket as exc:
        raise HTTPException(status_code=404, detail=f"unknown market: {exc}") from exc


@router.get("/quote", response_model=MarketQuoteResponse)
async def market_quote(
    ctx: Ctx,
    slug: Annotated[str, Query(min_length=1)],
    qty: Annotated[Decimal, Query(gt=0)],
    from_: Annotated[str, Query(alias="from")] = "polymarket",
) -> MarketQuoteResponse:
    """STUB. Cost of selling on Polymarket US and buying on Gemini, from fixture prices."""
    try:
        return await ctx.markets.quote(from_, slug, qty)
    except UnknownMarket as exc:
        raise HTTPException(status_code=404, detail=f"unknown market: {exc}") from exc


@router.post("/move", response_model=MarketMoveResponse)
async def market_move(body: MarketMoveRequest, ctx: Ctx) -> MarketMoveResponse:
    """STUB. Places no order. Emits dry-run trade events after applying the guards."""
    try:
        return MarketMoveResponse(move_id=await ctx.markets.move(body))
    except UnknownMarket as exc:
        raise HTTPException(status_code=404, detail=f"unknown market: {exc}") from exc
    except GuardRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
