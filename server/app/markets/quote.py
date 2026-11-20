"""The cost of moving a position: sell on Polymarket US, buy on Gemini. Arithmetic on two
order books, not a prediction.

Fees (both venues): taker fee = rate x contracts x price x (1 - price), summed per book level
taken. Makers pay nothing on Polymarket US.
- Polymarket US taker rate 0.095: the plan, and `feeCoefficient` on every market read on
  2026-11-20 (see app.markets.polymarket.POLYMARKET_TAKER_RATE).
- Gemini taker rate 0.07 for contracts listed on or after 2026-03-18:
  https://www.gemini.com/fees/predictions (see app.markets.gemini.GEMINI_TAKER_RATE).
  Older contracts follow a legacy schedule (0.05 taker); not modelled.

Two ways to sell: "take" crosses the bid and pays the taker fee; "post" rests an ask at
`sell.post_at` and pays nothing on the sale, but may not fill. The buy on Gemini is always a
taker order, because it fires the moment the sale fills.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.markets.book import Book, Level
from app.markets.gemini import GEMINI_TAKER_RATE
from app.markets.polymarket import MIN_QTY, POLYMARKET_TAKER_RATE
from app.markets.stub import CENT, GuardRefused, UnknownMarket, taker_fee
from app.schemas import BuyQuote, MarketQuoteResponse, SellQuote

TENTH = Decimal("0.1")
TICK = Decimal("0.01")


def _fee(rate: Decimal, levels: list[Level]) -> Decimal:
    return sum((taker_fee(rate, lv.qty, lv.price) for lv in levels), Decimal(0))


def _cost(levels: list[Level]) -> Decimal:
    return sum((lv.qty * lv.price for lv in levels), Decimal(0))


def post_price(book: Book, tick: Decimal = TICK) -> Decimal:
    """Where to rest a sell so it is a maker order: one tick inside the spread, or at the
    best ask when the spread is a single tick (one tick inside would cross and take)."""
    bid, ask = book.best_bid, book.best_ask
    if bid is None or ask is None:
        raise UnknownMarket("Polymarket US has no two-sided book for this market")
    return min(bid.price + tick, ask.price)


def build_quote(
    pm_book: Book,
    gemini_book: Book,
    qty: Decimal,
    *,
    pm_rate: Decimal = POLYMARKET_TAKER_RATE,
    gemini_rate: Decimal = GEMINI_TAKER_RATE,
    tick: Decimal = TICK,
) -> MarketQuoteResponse:
    """Sell `qty` at Polymarket US, buy it at Gemini. `bid`/`ask` are the best prices;
    fees and `move_cost_pct` walk the book for the whole quantity. `bid_depth` is the number
    of contracts resting at the best bid."""
    if qty < MIN_QTY:
        raise GuardRefused(f"the minimum size is {MIN_QTY} contracts")
    if pm_book.best_bid is None:
        raise UnknownMarket("nobody is bidding on Polymarket US")
    if gemini_book.best_ask is None:
        raise UnknownMarket("nobody is offering on Gemini")
    sells = pm_book.walk("bids", qty)
    buys = gemini_book.walk("asks", qty)
    sell_fee, buy_fee = _fee(pm_rate, sells), _fee(gemini_rate, buys)
    proceeds = _cost(sells) - sell_fee
    outlay = _cost(buys) + buy_fee
    gross = pm_book.best_bid.price * qty
    return MarketQuoteResponse(
        sell=SellQuote(
            bid=pm_book.best_bid.price,
            post_at=post_price(pm_book, tick),
            taker_fee=sell_fee.quantize(CENT, rounding=ROUND_HALF_UP),
        ),
        buy=BuyQuote(
            ask=gemini_book.best_ask.price, taker_fee=buy_fee.quantize(CENT, rounding=ROUND_HALF_UP)
        ),
        move_cost_pct=((outlay - proceeds) / gross * 100).quantize(TENTH, rounding=ROUND_HALF_UP),
        bid_depth=pm_book.best_bid.qty,
    )


def post_cost_pct(quote: MarketQuoteResponse, qty: Decimal) -> Decimal:
    """Cost of the maker route as a percentage of the sale at the bid: no fee on the sale,
    sold at `post_at`, bought at the ask with the taker fee."""
    cost = (quote.buy.ask - quote.sell.post_at) * qty + quote.buy.taker_fee
    return (cost / (quote.sell.bid * qty) * 100).quantize(TENTH, rounding=ROUND_HALF_UP)
