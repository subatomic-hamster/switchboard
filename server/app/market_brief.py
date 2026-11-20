"""The markets operator: what it says about moving a position, and the routes behind it.

A brief puts together, for one market and quantity:

1. the equivalence check (P1's `match`): the same bet, or refuse and say which field differs;
2. the cost of the move (P1's `quote`), as two options: sell now and pay the taker fee, or
   post an ask and wait, which makes the sale a maker order;
3. "will I get a bid?", answered from bid depth and spread. That is a fact, not a forecast;
4. the hold-or-move opinion, with its track record, which the owner can override.

Facts and the forecast are kept apart in the text, and the forecast always carries the
"not financial advice" line.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.context import AppContext, get_ctx
from app.forecast import DISCLAIMER, ForecastUnavailable
from app.markets.stub import GuardRefused, UnknownMarket
from app.planner import say_money
from app.schemas import (
    ForecastList,
    ForecastRequest,
    MarketBrief,
    MarketListing,
    MarketListingList,
    MarketMatchResponse,
    MarketMoveRequest,
    MarketMoveResponse,
    MarketQuoteResponse,
    MoveOption,
    OkResponse,
    Opinion,
    RevealRequest,
    TrackRecord,
    VoiceOverrideRequest,
)
from app.voice import require_voice_token

CENT = Decimal("0.01")
TENTH = Decimal("0.1")

CHECK_NAMES = {
    "event": "the event",
    "strike": "the threshold",
    "direction": "the direction",
    "time": "the event time",
    "source": "who decides the outcome",
}


def cents(price: Decimal) -> str:
    value = (price * 100).quantize(TENTH, rounding=ROUND_HALF_UP).normalize()
    return f"{value:f} cents"


def options_for(
    quote: MarketQuoteResponse, qty: Decimal, rebate_pct: Decimal | None = None
) -> list[MoveOption]:
    """Sell now (taker on both sides) versus post an ask and wait (maker on the sale).
    `rebate_pct` (demo mode's Gemini offer) is paid back on the Gemini buy leg."""
    buy_fee = quote.buy.taker_fee
    rebate = quote.buy.ask * qty * rebate_pct / 100 if rebate_pct else Decimal(0)
    note = f", {rebate_pct.normalize():f}% rebate" if rebate_pct else ""
    take_cost = (quote.buy.ask - quote.sell.bid) * qty + quote.sell.taker_fee + buy_fee - rebate
    post_cost = (quote.buy.ask - quote.sell.post_at) * qty + buy_fee - rebate
    sale_value = quote.sell.bid * qty

    def pct(cost: Decimal) -> Decimal:
        return (cost / sale_value * 100).quantize(TENTH, rounding=ROUND_HALF_UP)

    return [
        MoveOption(
            mode="take",
            label=f"Sell now and pay the fee{note}",
            sell_price=quote.sell.bid,
            buy_price=quote.buy.ask,
            fees_usd=(quote.sell.taker_fee + buy_fee).quantize(CENT),
            cost_usd=take_cost.quantize(CENT, rounding=ROUND_HALF_UP),
            cost_pct=pct(take_cost),
        ),
        MoveOption(
            mode="post",
            label=f"Post an ask and wait{note}",
            sell_price=quote.sell.post_at,
            buy_price=quote.buy.ask,
            fees_usd=buy_fee.quantize(CENT),
            cost_usd=post_cost.quantize(CENT, rounding=ROUND_HALF_UP),
            cost_pct=pct(post_cost),
        ),
    ]


def bid_answer(quote: MarketQuoteResponse, listing: MarketListing | None, qty: Decimal) -> str:
    depth = quote.bid_depth
    spread = (listing.polymarket.ask - listing.polymarket.bid) if listing else None
    spread_text = f" The spread is {cents(spread)}." if spread is not None else ""
    if depth >= qty:
        return (
            f"Yes. {depth.normalize():f} contracts are bid at {cents(quote.sell.bid)}, more than "
            f"the {qty.normalize():f} you want to sell.{spread_text}"
        )
    return (
        f"Only partly. {depth.normalize():f} contracts are bid at {cents(quote.sell.bid)} and you "
        f"want to sell {qty.normalize():f}, so the rest would wait or fill lower.{spread_text}"
    )


def refusal_text(match: MarketMatchResponse) -> str:
    differing = [
        CHECK_NAMES[name]
        for name, value in match.checks.model_dump().items()
        if value == "different"
    ]
    what = " and ".join(differing) if differing else "one of the settlement terms"
    note = f" {match.notes[0][:1].upper()}{match.notes[0][1:]}." if match.notes else ""
    return (
        f"I can't move that. The two markets look alike but are not the same bet: {what} "
        f"differ{'s' if len(differing) <= 1 else ''}.{note}"
    )


def track_text(record: TrackRecord) -> str:
    if record.revealed == 0 or record.mean_brier is None or record.market_brier is None:
        return (
            f"My track record: no forecasts scored yet, {record.pending} waiting on their events."
        )
    better = "better than" if record.mean_brier < record.market_brier else "no better than"
    return (
        f"My track record: {record.revealed} forecast{'s' if record.revealed != 1 else ''} "
        f"scored, average Brier {record.mean_brier:.3f} against the market's "
        f"{record.market_brier:.3f}. Lower is better, so I have been {better} the market price."
    )


def spoken_brief(brief: MarketBrief) -> str:
    """What the markets operator reads out. Facts first, the forecast last and labelled."""
    if brief.match.verdict != "match":
        return refusal_text(brief.match)
    parts = ["These are the same bet on both venues."]
    if brief.match.notes:
        parts.append(f"One difference to know: {brief.match.notes[0]}.")
    if brief.quote is not None and brief.options:
        take, post = brief.options[0], brief.options[1]
        parts.append(
            f"Selling now gets {cents(take.sell_price)} a contract. Buying on Gemini costs "
            f"{cents(take.buy_price)}. With fees, moving {brief.qty.normalize():f} contracts "
            f"costs {say_money(take.cost_usd)}, {take.cost_pct} percent."
        )
        parts.append(
            f"Posting at {cents(post.sell_price)} and waiting avoids the selling fee: "
            f"{post.cost_pct} percent."
        )
        parts.append(f"Will you get a bid? {brief.bid_answer}")
    if brief.opinion is not None:
        parts.append(f"My opinion, which is a forecast and not advice: {brief.opinion.summary}")
        parts.append(track_text(brief.track_record))
        parts.append("You can override me.")
    elif brief.opinion_unavailable:
        parts.append(brief.opinion_unavailable)
    return " ".join(parts)


def all_in_buy_price(quote: MarketQuoteResponse, qty: Decimal) -> Decimal:
    """What one contract costs to buy on the destination venue, fee included."""
    return quote.buy.ask + quote.buy.taker_fee / qty


class MarketDesk:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    def listing(self, slug: str) -> MarketListing | None:
        return next((m for m in self._ctx.markets.listing() if m.slug == slug), None)

    async def brief(self, slug: str, qty: Decimal) -> MarketBrief:
        ctx = self._ctx
        match = await ctx.markets.match("polymarket", slug)
        listing = self.listing(slug)
        quote: MarketQuoteResponse | None = None
        options: list[MoveOption] = []
        answer = "No quote: the move is refused, so there is nothing to sell into."
        opinion: Opinion | None = None
        if match.verdict == "match":
            quote = await ctx.markets.quote("polymarket", slug, qty)
            options = options_for(quote, qty, ctx.demo.rebate_pct() if ctx.demo else None)
            answer = bid_answer(quote, listing, qty)
            opinion = ctx.forecaster.latest_for(slug, all_in_buy_price(quote, qty))
        families = ctx.forecaster.families()
        unavailable = ""
        if opinion is None and match.verdict == "match":
            unavailable = (
                "I have not formed an opinion on this one yet. Ask for it and I will."
                if len(set(families)) >= 3
                else "I have no forecast to offer: it needs models from three different "
                f"families and {len(set(families))} are configured."
            )
        brief = MarketBrief(
            slug=slug,
            title=listing.title if listing else slug,
            qty=qty,
            match=match,
            listing=listing,
            quote=quote,
            options=options,
            bid_answer=answer,
            opinion=opinion,
            opinion_unavailable=unavailable,
            track_record=ctx.forecaster.track_record(),
            spoken="",
            disclaimer=DISCLAIMER,
        )
        return brief.model_copy(update={"spoken": spoken_brief(brief)})

    async def form_opinion(self, slug: str, qty: Decimal) -> Opinion:
        ctx = self._ctx
        match = await ctx.markets.match("polymarket", slug)
        if match.verdict != "match":
            raise GuardRefused(refusal_text(match))
        listing = self.listing(slug)
        if listing is None:
            raise UnknownMarket(slug)
        quote = await ctx.markets.quote("polymarket", slug, qty)
        return await ctx.forecaster.opine(listing, all_in_buy_price(quote, qty))


router = APIRouter(prefix="/api", tags=["markets desk"])
Ctx = Annotated[AppContext, Depends(get_ctx)]
Slug = Annotated[str, Query(min_length=1)]
Qty = Annotated[Decimal, Query(gt=0)]


@router.get("/desk/markets", response_model=MarketListingList)
async def desk_markets(ctx: Ctx) -> MarketListingList:
    """Markets with both venues' prices, for the side-by-side panel."""
    return MarketListingList(markets=ctx.markets.listing())


@router.get("/desk/brief", response_model=MarketBrief)
async def desk_brief(ctx: Ctx, slug: Slug, qty: Qty = Decimal("1")) -> MarketBrief:
    """Match result, move cost, bid depth, the latest opinion and its track record."""
    try:
        return await ctx.desk.brief(slug, qty)
    except UnknownMarket as exc:
        raise HTTPException(status_code=404, detail=f"unknown market: {exc}") from exc


@router.post("/desk/move", response_model=MarketMoveResponse)
async def desk_move(body: MarketMoveRequest, ctx: Ctx) -> MarketMoveResponse:
    """Run the move through the guards. Refused unless the markets match; against the stub it
    places no order."""
    try:
        match = await ctx.markets.match("polymarket", body.slug)
        if match.verdict != "match":
            raise GuardRefused(refusal_text(match))
        return MarketMoveResponse(move_id=await ctx.markets.move(body))
    except UnknownMarket as exc:
        raise HTTPException(status_code=404, detail=f"unknown market: {exc}") from exc
    except GuardRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/forecasts", response_model=Opinion)
async def create_forecast(body: ForecastRequest, ctx: Ctx) -> Opinion:
    """Ask the forecasting models, commit the hash of the opinion, and return it."""
    try:
        return await ctx.desk.form_opinion(body.slug, body.qty)
    except UnknownMarket as exc:
        raise HTTPException(status_code=404, detail=f"unknown market: {exc}") from exc
    except GuardRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ForecastUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/forecasts", response_model=ForecastList)
async def list_forecasts(ctx: Ctx) -> ForecastList:
    """Every opinion with its commitment, and the track record once events have resolved."""
    return ForecastList(forecasts=ctx.forecaster.all(), track_record=ctx.forecaster.track_record())


@router.post("/forecasts/{receipt_id}/override", response_model=Opinion)
async def override_forecast(receipt_id: str, ctx: Ctx) -> Opinion:
    """The owner goes against the opinion. Recorded."""
    try:
        return ctx.forecaster.override(receipt_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown forecast") from exc


@router.post("/forecasts/{receipt_id}/reveal", response_model=Opinion)
async def reveal_forecast(receipt_id: str, body: RevealRequest, ctx: Ctx) -> Opinion:
    """After the event: verify the commitment and score the opinion (Brier)."""
    try:
        return await ctx.forecaster.reveal(receipt_id, body.outcome)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown forecast") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


voice_router = APIRouter(
    prefix="/api/voice/markets",
    tags=["voice tools"],
    dependencies=[Depends(require_voice_token)],
)


@voice_router.get("/list", response_model=MarketListingList)
async def voice_market_list(ctx: Ctx) -> MarketListingList:
    """Voice tool. The markets the operator can compare."""
    return MarketListingList(markets=ctx.markets.listing())


@voice_router.get("/brief", response_model=MarketBrief)
async def voice_market_brief(ctx: Ctx, slug: Slug, qty: Qty = Decimal("1")) -> MarketBrief:
    """Voice tool. The markets operator reads `spoken`."""
    try:
        return await ctx.desk.brief(slug, qty)
    except UnknownMarket as exc:
        raise HTTPException(status_code=404, detail=f"unknown market: {exc}") from exc


@voice_router.post("/override", response_model=OkResponse)
async def voice_market_override(body: VoiceOverrideRequest, ctx: Ctx) -> OkResponse:
    """Voice tool. Records that the owner chose to go against the opinion."""
    try:
        ctx.forecaster.override(body.receipt_id)
    except KeyError:
        return OkResponse(ok=False, error="unknown_forecast")
    return OkResponse(ok=True, status="overridden")
