"""Demo mode (app/demo_desk.py): routes, portfolio, suggestions, the BTC 5-minute buy, moves with
the rebate, the insight verdicts, settling, and the Ethereum to Solana send. No network: both
venues, the Polymarket data API and OpenRouter are faked with respx."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI

from app.context import AppContext
from app.db import Database
from app.demo_desk import (
    BTC_PREFIX,
    DATA_API,
    NBA,
    DemoDesk,
    choose_trader,
    current_window,
    find_similar,
)
from app.events import _sse
from app.main import create_app
from app.markets.match import evaluate
from tests.conftest import make_settings
from tests.test_markets_real import Venues

NOW = datetime(2026, 11, 20, 15, 1, tzinfo=UTC)
BTC = "cpc-btc-updown-5m-2026-11-20-1500z"
BTC_SYMBOL = "GEMI-BTC05M2611201505-UP"
OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
ROCKETS, ATLETICO, BTC_POS = "demo-1", "demo-2", "demo-3"
LINKED_SLUG = "houston-rockets-vs.-denver-nuggets"

BTC_PM = {
    "slug": BTC,
    "question": "BTC Up or Down 5m",
    "title": "BTC Up or Down 5m",
    "description": "This market will settle to Up if the Bitcoin price at 10:05AM ET on Nov 20, "
    "2026 is greater than or equal to the price at 10:00AM ET. Outcome sourced from CF "
    "Benchmarks' Bitcoin Real-Time Index (BRTI)",
    "gameStartTime": "2026-11-20T15:00:00Z",
    "orderPriceMinTickSize": "0.01",
    "marketSides": [{"description": "Yes", "long": True}, {"description": "No", "long": False}],
    "assetPriceTerms": {
        "marketType": "ASSET_PRICE_MARKET_TYPE_UP_DOWN",
        "asset": {"symbol": "btc"},
        "indexSymbol": "BRTI",
        "horizon": "5m",
        "windowStart": "2026-11-20T15:00:00Z",
        "windowEnd": "2026-11-20T15:05:00Z",
        "strike": None,
    },
}
BTC_EVENT = {
    "title": "BTC price today at 10:05am EST",
    "category": "Crypto",
    "ticker": "BTC05M2611201505",
    "status": "active",
    "template": "crypto-up-down",
    "source": "LS-LRRR-XBTUSD",
    "sourceDetails": {"agency": "lukka", "index": "LS-LRRR-XBTUSD"},
    "expiryDate": "2026-11-20T15:05:00.000Z",
    "startTime": "2026-11-20T15:00:00.000Z",
    "participants": [],
    "contracts": [
        {
            "label": "Up",
            "ticker": "UP",
            "instrumentSymbol": BTC_SYMBOL,
            "expiryDate": "2026-11-20T15:05:00.000Z",
            "strike": {"type": "reference", "value": "61660.13"},
            "prices": {"bestBid": "0.12", "bestAsk": "0.21"},
        }
    ],
}
BTC_PM_BOOK = {
    "marketData": {
        "bids": [{"px": {"value": "0.15"}, "qty": "1000"}],
        "offers": [{"px": {"value": "0.16"}, "qty": "1000"}],
    }
}


def position(title: str, value: float, **extra: Any) -> dict[str, Any]:
    return {
        "title": title,
        "outcome": "Yes",
        "current_size": 100,
        "avg_price": 0.40,
        "current_price": 0.50,
        "current_value": value,
        "total_pnl": 10.0,
        "slug": title.lower().replace(" ", "-"),
        "event_slug": "ev-" + title.lower().replace(" ", "-"),
        "end_date": "2099-12-01",
        "status": "OPEN",
        **extra,
    }


LEADERS = [
    {"user_id": "0xaaa", "user_name": "thin"},
    {"user_id": "0xbbb", "user_name": "0xbbb"},
]
POSITIONS = {
    "0xaaa": [position("Only two", 5), position("Just two", 4)],
    "0xbbb": [
        position("Houston Rockets vs. Denver Nuggets", 900),
        position("Will it rain on Mars", 50),
        position("Another one", 40),
        position("Fourth", 30),
        position("Closed bet", 999, status="CLOSED"),
    ],
}


@dataclass
class Stage:
    client: httpx.AsyncClient
    ctx: AppContext
    desk: DemoDesk
    venues: Venues
    app: FastAPI


@asynccontextmanager
async def stage(*, data_api: bool = True, **settings: Any) -> AsyncIterator[Stage]:
    venues = Venues(respx.mock)
    venues.data["markets"][BTC] = BTC_PM
    venues.data["events"]["BTC05M2611201505"] = BTC_EVENT
    venues.data["pm_books"][BTC] = BTC_PM_BOOK
    venues.data["gem_books"][BTC_SYMBOL] = {"bids": [], "asks": []}
    if data_api:
        respx.get(f"{DATA_API}/v2/leaderboard").mock(
            return_value=httpx.Response(200, json={"data": LEADERS})
        )
        respx.get(url__regex=rf"{DATA_API}/v2/positions.*").mock(
            side_effect=lambda req: httpx.Response(
                200, json={"data": POSITIONS[req.url.params["user"]]}
            )
        )
    else:
        respx.get(url__regex=rf"{DATA_API}/.*").mock(return_value=httpx.Response(500))
    config: dict[str, Any] = {
        "demo_mode": True,
        "use_real_markets": True,
        "policy_transfer_cap_usd": Decimal(500),
        "policy_daily_budget_usd": Decimal(2000),
    }
    app = create_app(make_settings(**(config | settings)), Database(None))
    ctx: AppContext = app.state.ctx
    assert ctx.demo is not None
    ctx.demo._now = lambda: NOW
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            yield Stage(client, ctx, ctx.demo, venues, app)
    finally:
        await ctx.aclose()
        assert isinstance(ctx.markets, object)


async def get(s: Stage, path: str, **params: Any) -> Any:
    response = await s.client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def post(s: Stage, path: str, body: dict[str, Any] | None = None) -> Any:
    response = await s.client.post(path, json=body or {})
    assert response.status_code == 200, response.text
    return response.json()


async def balances(s: Stage) -> dict[str, float]:
    return {b["place"]: b["usd"] for b in (await get(s, "/api/demo/portfolio"))["balances"]}


async def buy_btc(s: Stage) -> dict[str, Any]:
    text = "Put 30 dollars from my Ethereum wallet into the Bitcoin 5 minute market on Polymarket"
    plan = await post(s, "/api/intent", {"text": text})
    done = await post(s, f"/api/plans/{plan['plan_id']}/execute", {"route_id": "cheapest"})
    assert done["status"] == "executing"
    return dict(plan)


async def offer_and_move_rockets(s: Stage) -> None:
    await post(s, "/api/demo/offer")
    await post(s, "/api/desk/move", {"slug": NBA, "qty": 320, "mode": "take"})


def labels(data: dict[str, Any]) -> list[str]:
    return [x["label"] for x in data["suggestions"]]


# ------------------------------------------------------------------------------- switched off


async def test_demo_routes_are_404_without_demo_mode(client: httpx.AsyncClient, ctx: Any) -> None:
    assert ctx.demo is None
    for path in ("portfolio", "suggestions", "recent", "insight?position_id=demo-1"):
        assert (await client.get(f"/api/demo/{path}")).status_code == 404
    for path in ("offer", "settle", "reset"):
        assert (await client.post(f"/api/demo/{path}")).status_code == 404


# --------------------------------------------------------------------------------- portfolio


@respx.mock
async def test_portfolio_has_scripted_and_linked_positions_with_live_prices() -> None:
    async with stage() as s:
        await s.desk.warm()  # what the server does at startup: the slow Gemini listing
        data = await get(s, "/api/demo/portfolio")
        assert data["account"]["linked"] is True
        assert data["account"]["wallet"] == "0xbbb"
        assert data["account"]["venue"] == "Polymarket"
        assert data["offer"] is None
        by_id = {p["id"]: p for p in data["positions"]}
        rockets, atletico = by_id[ROCKETS], by_id[ATLETICO]
        assert (rockets["venue"], rockets["contracts"], rockets["avg_price"]) == (
            "Polymarket US",
            320,
            0.44,
        )
        assert rockets["price"] == 0.47  # best bid in the recorded book
        assert rockets["url"].endswith(NBA) and rockets["status"] == "open"
        assert atletico["venue"] == "Gemini" and atletico["avg_price"] == 0.58
        assert atletico["slug"] == "atc-lal-get-atm-2026-11-22-atm"
        linked = [p for p in data["positions"] if p["id"].startswith("pm-")]
        assert [p["venue"] for p in linked] == ["Polymarket"] * 4  # closed one left out
        assert linked[0]["title"].startswith("Houston Rockets")  # by current value
        assert linked[0]["url"].startswith("https://polymarket.com/event/")
        assert linked[0]["adjacent"]["venue"] == "Gemini"  # a similar market, labelled as such
        assert linked[0]["adjacent"]["symbol"].startswith("GEMI-NBA")
        assert linked[1]["adjacent"] is None
        names = {b["place"] for b in data["balances"]}
        assert names == {
            "Ethereum wallet",
            "Polymarket US",
            "Polymarket",
            "Gemini",
            "Solana wallet",
            "Arc wallet",
        }
        recent = (await get(s, "/api/demo/recent"))["actions"]
        assert 7 <= len(recent) <= 12
        assert {a["kind"] for a in recent} >= {"send", "receive", "swap", "bet", "win"}
        assert [a["ts"] for a in recent] == sorted((a["ts"] for a in recent), reverse=True)
        assert any(a["to"] == "Sam's Solana wallet" for a in recent)


@respx.mock
async def test_data_api_down_leaves_only_scripted_positions() -> None:
    async with stage(data_api=False) as s:
        data = await get(s, "/api/demo/portfolio")
        assert data["account"] is None
        assert [p["id"] for p in data["positions"]] == [ROCKETS, ATLETICO]


@respx.mock
async def test_pinned_wallet_is_used_instead_of_the_leaderboard() -> None:
    async with stage(demo_trader_wallet="0xbbb") as s:
        assert (await get(s, "/api/demo/portfolio"))["account"]["wallet"] == "0xbbb"


def test_trader_picker_prefers_sports_or_crypto_with_three_open_positions() -> None:
    users = [{"user_id": f"0x{i}"} for i in range(5)]
    few = [position("a", 1), position("b", 1)]
    politics = [position("Will X win the election", 1)] * 3
    sports = [position("Lakers vs. Celtics", 1)] * 3
    closed = [position("c", 1, status="CLOSED")] * 5
    chosen = choose_trader(users, [few, RuntimeError("down"), politics, sports, closed])
    assert chosen is not None and chosen[0]["user_id"] == "0x3"
    # No sporty one: the first with three open positions.
    chosen = choose_trader(users, [few, closed, politics, politics, politics])
    assert chosen is not None and chosen[0]["user_id"] == "0x2"
    assert choose_trader(users, [few, closed]) is None
    assert choose_trader(users, [RuntimeError("x")] * 5) is None


def test_similar_gemini_market_needs_two_shared_words() -> None:
    events = [
        {
            "title": "Houston Rockets at Denver Nuggets",
            "contracts": [
                {"label": "Rockets", "instrumentSymbol": "S1", "prices": {"bestBid": "0.4"}}
            ],
        }
    ]
    hit = find_similar("Houston Rockets vs. Denver Nuggets", events)
    assert hit is not None and hit.symbol == "S1" and hit.price == Decimal("0.4")
    assert find_similar("Will the Rockets win", events) is None  # one shared word


def test_similar_market_picks_the_same_kind_of_contract() -> None:
    events = [
        {
            "title": "Toronto Raptors vs Detroit Pistons",
            "contracts": [
                {"label": "Total Points: More than 193.5", "instrumentSymbol": "TOT"},
                {"label": "Point Spread: Detroit by 1.5", "instrumentSymbol": "SPR"},
                {"label": "Toronto", "instrumentSymbol": "WIN"},
            ],
        }
    ]
    pick = lambda title: getattr(find_similar(title, events), "symbol", None)  # noqa: E731
    assert pick("Raptors vs. Pistons") == "WIN"
    assert pick("Spread: Raptors Pistons (-4.5)") == "SPR"
    assert pick("Raptors vs. Pistons: O/U 220.5") == "TOT"


def test_current_window_rolls_over_with_under_a_minute_left() -> None:
    mid = current_window(datetime(2026, 11, 20, 15, 1, 20, tzinfo=UTC))
    assert mid[2] == BTC and mid[1] - mid[0] == (mid[1] - mid[0]).__class__(minutes=5)
    late = current_window(datetime(2026, 11, 20, 15, 4, 30, tzinfo=UTC))
    assert late[2] == f"{BTC_PREFIX}2026-11-20-1505z"


# --------------------------------------------------------------------------- match and buy


def test_btc_5m_pair_is_different_on_source_only() -> None:
    result = evaluate(BTC_PM, [BTC_EVENT])
    checks = result.response.checks
    assert (checks.event, checks.direction, checks.time) == ("same", "same", "same")
    assert checks.strike == "n/a" and checks.source == "different"
    assert result.response.verdict == "refuse"
    assert result.response.gemini_symbol == BTC_SYMBOL


@respx.mock
async def test_btc_5m_is_matched_through_gemini_and_tracked() -> None:
    async with stage() as s:
        match = await get(s, "/api/markets/match", slug=BTC)
        assert match["verdict"] == "refuse" and match["checks"]["source"] == "different"
        await buy_btc(s)
        assert BTC in s.ctx.markets._tracked  # type: ignore[attr-defined]


@respx.mock
async def test_market_buy_plans_from_the_live_ask_then_fills() -> None:
    async with stage() as s:
        plan = await buy_btc(s)
        assert plan["kind"] == "market_buy" and plan["parser"] == "rules"
        buy = plan["market_buy"]
        assert buy["venue"] == "Polymarket US" and buy["slug"] == BTC and buy["side"] == "Up"
        assert buy["price"] == 0.16 and buy["contracts"] == 173.64 and buy["fee_usd"] == 2.22
        assert buy["from_place"] == "Ethereum wallet" and buy["amount_usd"] == 30
        assert buy["ends_at"].startswith("2026-11-20T15:05:00")
        assert "simulated" in plan["spoken_quote"]
        data = await get(s, "/api/demo/portfolio")
        assert {b["place"]: b["usd"] for b in data["balances"]}["Ethereum wallet"] == 220
        held = next(p for p in data["positions"] if p["id"] == BTC_POS)
        assert held["slug"] == BTC and held["ends_at"].startswith("2026-11-20T15:05:00")
        assert held["contracts"] == 173.64 and held["price"] == 0.15
        first = (await get(s, "/api/demo/recent"))["actions"][0]
        assert first["kind"] == "bet" and first["from"] == "Ethereum wallet"
        events = [e.type for e in s.ctx.bus.history()]
        assert "demo.updated" in events
        # The same plan cannot be filled twice.
        again = await s.client.post(
            f"/api/plans/{plan['plan_id']}/execute", json={"route_id": "cheapest"}
        )
        assert again.status_code == 409


@respx.mock
async def test_market_buy_needs_a_wallet_and_enough_money() -> None:
    async with stage() as s:
        text = "Put 30 dollars into the Bitcoin 5 minute market on Polymarket"
        assert (await s.client.post("/api/intent", json={"text": text})).status_code == 422
        big = "Put 900 dollars from my Ethereum wallet into the Bitcoin 5 minute market"
        plan = await post(s, "/api/intent", {"text": big})
        done = await s.client.post(f"/api/plans/{plan['plan_id']}/execute", json={})
        assert done.status_code == 409 and "does not have" in done.json()["detail"]


@respx.mock
async def test_demo_updated_goes_out_as_its_own_sse_event_name() -> None:
    async with stage() as s:
        await post(s, "/api/demo/offer")
        event = next(e for e in s.ctx.bus.history() if e.type == "demo.updated")
        assert _sse(event)["event"] == "demo.updated"
        assert set(event.data) == {"title", "detail"}
        data = await get(s, "/api/demo/portfolio")
        assert data["offer"]["expires_at"].startswith("2026-11-20T15:11:00")
        assert data["offer"]["rebate_pct"] == 3 and data["offer"]["venue"] == "Gemini"


# --------------------------------------------------------------------- suggestions and moves


@respx.mock
async def test_suggestions_change_across_buy_offer_move_and_settle() -> None:
    async with stage() as s:
        seen = [labels(await get(s, "/api/demo/suggestions"))]
        assert seen[0][0] == "Put $30 into BTC 5-min" and "Send $40 to Sam again" in seen[0]
        first = (await get(s, "/api/demo/suggestions"))["suggestions"][0]
        assert first["text"].startswith("Put 30 dollars from my Ethereum wallet")
        assert first["why"]
        await buy_btc(s)
        seen.append(labels(await get(s, "/api/demo/suggestions")))
        assert "Put $30 into BTC 5-min" not in seen[1]
        await post(s, "/api/demo/offer")
        seen.append(labels(await get(s, "/api/demo/suggestions")))
        assert "Move Rockets to Gemini (3% rebate)" in seen[2]
        assert "Move BTC 5-min to Gemini" in seen[2]
        await post(s, "/api/desk/move", {"slug": NBA, "qty": 320, "mode": "take"})
        seen.append(labels(await get(s, "/api/demo/suggestions")))
        assert "Move Rockets to Gemini (3% rebate)" not in seen[3]
        await post(s, "/api/demo/settle")
        seen.append(labels(await get(s, "/api/demo/suggestions")))
        assert "Send $150 to Sam's Solana" in seen[4]
        sentence = next(
            x["text"] for x in (await get(s, "/api/demo/suggestions"))["suggestions"]
            if "150" in x["label"]
        )  # fmt: skip
        assert sentence == (
            "Send 150 dollars from my Ethereum wallet to Sam's Solana wallet to cover my debt"
        )
        assert all(len(x) <= 4 for x in seen)
        assert all(seen[i] != seen[i + 1] for i in range(len(seen) - 1))


@respx.mock
async def test_rockets_move_applies_the_rebate_and_lands_on_gemini() -> None:
    async with stage() as s:
        plain = await get(s, "/api/desk/brief", slug=NBA, qty=320)
        await post(s, "/api/demo/offer")
        rebated = await get(s, "/api/desk/brief", slug=NBA, qty=320)
        before, after = plain["options"][0], rebated["options"][0]
        assert "rebate" in after["label"] and "rebate" not in before["label"]
        assert after["cost_usd"] < before["cost_usd"]
        await post(s, "/api/desk/move", {"slug": NBA, "qty": 320, "mode": "take"})
        data = await get(s, "/api/demo/portfolio")
        old = next(p for p in data["positions"] if p["id"] == ROCKETS)
        new = next(p for p in data["positions"] if p["venue"] == "Gemini" and p["slug"] == NBA)
        assert old["status"] == "moved" and new["status"] == "open" and new["contracts"] == 320
        assert new["avg_price"] == pytest.approx(0.48 * 0.97)  # the quote's ask less 3%
        assert (await get(s, "/api/demo/recent"))["actions"][0]["kind"] == "move"


@respx.mock
async def test_move_sentences_from_the_cards_resolve_to_the_right_position() -> None:
    async with stage() as s:
        await buy_btc(s)
        await get(s, "/api/demo/portfolio")  # linked positions are loaded
        cases = {
            "Move my Rockets position to Gemini": NBA,
            "Move my Rockets vs Nuggets · Nov 21 position to Gemini": NBA,
            "Move my Bitcoin 5 minute position to Gemini": BTC,
            "Move my BTC Up or Down 5m position to Gemini": BTC,
            "Move my Houston Rockets vs. Denver Nuggets position to Gemini": LINKED_SLUG,
        }
        for text, slug in cases.items():
            plan = await post(s, "/api/intent", {"text": text})
            move = plan["market_move"]
            assert plan["kind"] == "market_move" and move["slug"] == slug, text
            assert (move["from_venue"], move["to_venue"]) == ("polymarket", "gemini")
        back = await post(
            s,
            "/api/intent",
            {"text": "Move my Atletico de Madrid vs Getafe · Nov 22 position to Polymarket US"},
        )
        assert back["market_move"]["slug"] == "atc-lal-get-atm-2026-11-22-atm"
        assert (back["market_move"]["from_venue"], back["market_move"]["to_venue"]) == (
            "gemini",
            "polymarket",
        )


# ------------------------------------------------------------------------------------ insight


@respx.mock
async def test_btc_5m_insight_is_against_from_the_real_source_check() -> None:
    async with stage() as s:
        await buy_btc(s)
        data = await get(s, "/api/demo/insight", position_id=BTC_POS)
        assert data["verdict"] == "against" and data["source"] == "fallback"
        assert data["alternative"] == "Roll into the next Polymarket 5-minute window instead."
        source = next(c for c in data["checks"] if c["field"] == "Who decides the outcome")
        assert source["same"] is False
        assert "CF Benchmarks" in source["detail"] and "Lukka" in source["detail"]
        assert len(data["checks"]) == 5 and sum(c["same"] for c in data["checks"]) == 4
        assert "Roll into the next" in data["body"]
        assert (await s.client.get("/api/demo/insight?position_id=nope")).status_code == 404


@respx.mock
async def test_rockets_insight_is_go_with_the_offer_and_neutral_without() -> None:
    async with stage() as s:
        await s.desk.warm()
        calm = await get(s, "/api/demo/insight", position_id=ROCKETS)
        assert calm["verdict"] == "neutral" and "47" in calm["body"]
        await post(s, "/api/demo/offer")
        data = await get(s, "/api/demo/insight", position_id=ROCKETS)
        assert data["verdict"] == "go" and all(c["same"] for c in data["checks"])
        assert "5 of 5" in data["body"] and "3% rebate" in data["body"] and "$" in data["body"]
        linked = await get(s, "/api/demo/insight", position_id="pm-1")
        assert linked["verdict"] == "neutral" and "not checked to be the same bet" in linked["body"]
        gem = await get(s, "/api/demo/insight", position_id=ATLETICO)
        assert gem["verdict"] == "neutral"


@respx.mock
async def test_grok_words_the_insight_but_never_changes_the_verdict() -> None:
    answer = "Go ahead and move it, the two indexes are identical. Everything matches here."
    async with stage(openrouter_api_key="k") as s:
        await buy_btc(s)
        route = respx.post(OPENROUTER).mock(
            return_value=httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})
        )
        data = await get(s, "/api/demo/insight", position_id=BTC_POS)
        assert data["source"] == "grok" and data["model"] == "x-ai/grok-4.7"
        assert data["body"] == answer and data["verdict"] == "against"
        body = route.calls.last.request.read().decode()
        assert '"temperature":0.2' in body.replace(" ", "") and "x-ai/grok-4.7" in body
        await get(s, "/api/demo/insight", position_id=BTC_POS)
        assert route.call_count == 1  # cached for a minute per (position, verdict)


@respx.mock
@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(500),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": "x" * 900}}]}),
        httpx.Response(200, content=b"not json"),
    ],
)
async def test_insight_falls_back_when_grok_fails_or_answers_oddly(reply: httpx.Response) -> None:
    async with stage(openrouter_api_key="k") as s:
        await buy_btc(s)
        respx.post(OPENROUTER).mock(return_value=reply)
        data = await get(s, "/api/demo/insight", position_id=BTC_POS)
        assert data["source"] == "fallback" and data["model"] == ""
        assert data["verdict"] == "against" and "Roll into the next" in data["body"]


@respx.mock
async def test_insight_survives_grok_timing_out() -> None:
    async with stage(openrouter_api_key="k") as s:
        await buy_btc(s)
        respx.post(OPENROUTER).mock(side_effect=httpx.ReadTimeout("slow"))
        data = await get(s, "/api/demo/insight", position_id=BTC_POS)
        assert data["source"] == "fallback" and data["verdict"] == "against"


# -------------------------------------------------------------------- settle and the 150 send


@respx.mock
async def test_settle_pays_the_winnings_into_the_ethereum_wallet() -> None:
    async with stage() as s:
        # Not moved yet: the Polymarket US position settles.
        await post(s, "/api/demo/settle")
        data = await get(s, "/api/demo/portfolio")
        rockets = next(p for p in data["positions"] if p["id"] == ROCKETS)
        assert rockets["status"] == "won" and rockets["price"] == 1
        assert rockets["value_usd"] == 320 and rockets["pnl_usd"] == pytest.approx(179.2)
        assert {b["place"]: b["usd"] for b in data["balances"]}["Ethereum wallet"] == 570
        assert (await get(s, "/api/demo/recent"))["actions"][0]["kind"] == "win"
        assert (await post(s, "/api/demo/settle"))["ok"] is False  # nothing left to settle


@respx.mock
async def test_settle_after_the_move_pays_out_the_gemini_position() -> None:
    async with stage() as s:
        await offer_and_move_rockets(s)
        await post(s, "/api/demo/settle")
        data = await get(s, "/api/demo/portfolio")
        won = [p for p in data["positions"] if p["status"] == "won"]
        assert [p["venue"] for p in won] == ["Gemini"]
        moved = next(p for p in data["positions"] if p["id"] == ROCKETS)
        assert moved["status"] == "moved"
        assert {b["place"]: b["usd"] for b in data["balances"]}["Ethereum wallet"] == 570


@respx.mock
async def test_reset_puts_everything_back() -> None:
    async with stage() as s:
        await buy_btc(s)
        await post(s, "/api/demo/offer")
        await post(s, "/api/demo/settle")
        await post(s, "/api/demo/reset")
        data = await get(s, "/api/demo/portfolio")
        assert [p["id"] for p in data["positions"] if p["id"].startswith("demo")] == [
            ROCKETS,
            ATLETICO,
        ]
        assert data["offer"] is None and (await balances(s))["Ethereum wallet"] == 250


@respx.mock
async def test_150_dollars_from_ethereum_to_sams_solana_plans_and_executes() -> None:
    sentence = "Send 150 dollars from my Ethereum wallet to Sam's Solana wallet to cover my debt"
    async with stage() as s:
        plan = await post(s, "/api/intent", {"text": sentence})
        assert plan["kind"] == "transfer"
        assert plan["request"]["destination"]["label"] == "Sam's Solana wallet"
        assert plan["request"]["source"]["chain"] == "eth-sepolia"
        assert {r["id"] for r in plan["routes"]} == {"cheapest", "fastest"}
        for route in plan["routes"]:
            assert route["cost_usd"] > 0 and route["eta_s"] > 0 and route["steps"]
        assert plan["preview"]["outcome"] == "pay"  # inside the raised cap of 500
        done = await post(s, f"/api/plans/{plan['plan_id']}/execute", {"route_id": "cheapest"})
        assert done["status"] == "executing" and done["transfer_id"]
        assert (await balances(s))["Ethereum wallet"] == 100
        first = (await get(s, "/api/demo/recent"))["actions"][0]
        assert (first["kind"], first["amount_usd"], first["to"]) == (
            "send",
            150,
            "Sam's Solana wallet",
        )
