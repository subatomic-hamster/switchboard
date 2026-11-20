"""Move quotes from two order books, with the fee arithmetic worked out by hand."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.markets.book import Book, Level
from app.markets.gemini import parse_book as gemini_book
from app.markets.polymarket import parse_book as pm_book
from app.markets.quote import build_quote, post_cost_pct, post_price
from app.markets.stub import GuardRefused, UnknownMarket
from tests.markets_fixtures import load

D = Decimal


def book(bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> Book:
    return Book(
        tuple(Level(D(p), D(q)) for p, q in bids), tuple(Level(D(p), D(q)) for p, q in asks)
    )


def test_quote_worked_by_hand() -> None:
    pm = book([("0.45", "120")], [("0.47", "50")])
    gem = book([("0.44", "10")], [("0.46", "100")])
    quote = build_quote(pm, gem, D(4))
    # sell fee 0.095 x 4 x 0.45 x 0.55 = 0.09405; buy fee 0.07 x 4 x 0.46 x 0.54 = 0.069552
    assert quote.sell.bid == D("0.45")
    assert quote.sell.post_at == D("0.46")  # one tick inside the 0.45 / 0.47 spread
    assert quote.sell.taker_fee == D("0.09")
    assert quote.buy.ask == D("0.46")
    assert quote.buy.taker_fee == D("0.07")
    # (1.84 + 0.069552 - (1.80 - 0.09405)) / 1.80 = 11.31 %
    assert quote.move_cost_pct == D("11.3")
    assert quote.bid_depth == D("120")


def test_posting_does_not_cross_a_one_tick_spread() -> None:
    assert post_price(book([("0.45", "1")], [("0.46", "1")])) == D("0.46")  # join the ask
    assert post_price(book([("0.45", "1")], [("0.50", "1")])) == D("0.46")
    # a finer tick (some markets quote in tenths of a cent)
    assert post_price(book([("0.155", "1")], [("0.160", "1")]), D("0.001")) == D("0.156")


def test_maker_route_costs_less_than_the_taker_route() -> None:
    pm = book([("0.45", "120")], [("0.49", "50")])
    gem = book([], [("0.46", "100")])
    quote = build_quote(pm, gem, D(4))
    # post at 0.46: (0.46 - 0.46) x 4 + buy fee 0.07 = 0.07 over 1.80 = 3.9 %
    assert quote.sell.post_at == D("0.46")
    assert post_cost_pct(quote, D(4)) == D("3.9")
    assert post_cost_pct(quote, D(4)) < quote.move_cost_pct


def test_a_large_order_walks_the_book_and_fees_are_summed_per_level() -> None:
    pm = book([("0.45", "100"), ("0.44", "100")], [("0.47", "1")])
    gem = book([], [("0.46", "100"), ("0.48", "100")])
    quote = build_quote(pm, gem, D(150))
    # sell: 100 @ 0.45 and 50 @ 0.44
    sell_fee = D("0.095") * (100 * D("0.45") * D("0.55") + 50 * D("0.44") * D("0.56"))
    buy_fee = D("0.07") * (100 * D("0.46") * D("0.54") + 50 * D("0.48") * D("0.52"))
    assert quote.sell.taker_fee == sell_fee.quantize(D("0.01"))
    assert quote.buy.taker_fee == buy_fee.quantize(D("0.01"))
    assert quote.sell.bid == D("0.45") and quote.buy.ask == D("0.46")  # best prices
    proceeds = 100 * D("0.45") + 50 * D("0.44") - sell_fee
    outlay = 100 * D("0.46") + 50 * D("0.48") + buy_fee
    assert quote.move_cost_pct == ((outlay - proceeds) / (150 * D("0.45")) * 100).quantize(D("0.1"))


def test_minimum_size_and_empty_books_are_refused() -> None:
    pm, gem = book([("0.45", "5")], [("0.47", "5")]), book([], [("0.46", "5")])
    assert build_quote(pm, gem, D("0.01")).bid_depth == D("5")
    with pytest.raises(GuardRefused, match="minimum"):
        build_quote(pm, gem, D("0.009"))
    with pytest.raises(UnknownMarket, match="bidding"):
        build_quote(book([], [("0.47", "5")]), gem, D(1))
    with pytest.raises(UnknownMarket, match="offering"):
        build_quote(pm, book([("0.4", "1")], []), D(1))


def test_quote_from_the_recorded_nba_books() -> None:
    doc = load("nba_hou_den")
    pm = pm_book(doc["books"]["polymarket"]["aec-nba-hou-den-2026-11-20"])
    gem = gemini_book(doc["books"]["gemini"]["GEMI-NBA-2611210300-HOU-DEN-M-HOU"])
    quote = build_quote(pm, gem, D(4))
    assert pm.best_bid is not None and gem.best_ask is not None
    assert quote.sell.bid == pm.best_bid.price and quote.buy.ask == gem.best_ask.price
    expect_sell = D("0.095") * 4 * pm.best_bid.price * (1 - pm.best_bid.price)
    assert quote.sell.taker_fee == expect_sell.quantize(D("0.01"))
    assert quote.model_dump(mode="json")["move_cost_pct"] > 0
