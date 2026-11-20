"""The move: guards, the dry-run transcript, and every live path (full fill, partial fill,
unfilled, failed second leg) against mocked venues and a fake private socket.

No test sends anything to a real venue: respx raises on any request that is not mocked, and the
"live" tests use dummy keys against mocked URLs."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from app.db import Database
from app.events import EventBus, StoredEvent
from app.markets.book import VenueError
from app.markets.gemini import GEMINI_API
from app.markets.move import DUMMY_GEMINI_SECRET, DUMMY_PM_SECRET, MoveEngine
from app.markets.polymarket import GATEWAY, TRADING, sign_request
from app.markets.stub import GuardRefused
from app.schemas import MarketMoveRequest
from tests.conftest import make_settings
from tests.markets_fixtures import load
from tests.test_markets_venues import PM_SECRET, FakeSocket, execution, fake_connector

SLUG = "aec-nba-hou-den-2026-11-20"
SYMBOL = "GEMI-NBA-2611210300-HOU-DEN-M-HOU"
DOC = load("nba_hou_den")
# Recorded books: Polymarket US 0.47 bid / 0.48 ask, Gemini 0.47 bid / 0.48 ask.
SELL_AT_TAKE, SELL_AT_POST, BUY_AT = Decimal("0.47"), Decimal("0.48"), Decimal("0.48")


class Anchor:
    def __init__(self) -> None:
        self.commits: list[tuple[str, str]] = []

    async def commit(self, commitment_hex: str, label: str) -> str:
        self.commits.append((commitment_hex, label))
        return f"anchor:{len(self.commits)}"

    async def reveal(self, anchor_ref: str, preimage: bytes) -> None:
        pass


def mock_books(router: respx.MockRouter) -> None:
    router.get(f"{GATEWAY}/v1/markets/{SLUG}/book").mock(
        return_value=httpx.Response(200, json=DOC["books"]["polymarket"][SLUG])
    )
    router.get(f"{GEMINI_API}/v1/book/{SYMBOL}").mock(
        return_value=httpx.Response(200, json=DOC["books"]["gemini"][SYMBOL])
    )


class Rig:
    def __init__(self, engine: MoveEngine, bus: EventBus, anchor: Anchor) -> None:
        self.engine, self.bus, self.anchor = engine, bus, anchor
        self.events: list[StoredEvent] = []

    @property
    def trades(self) -> list[tuple[str, str, str, str, bool]]:
        return [
            (
                e.type,
                e.data["venue"],
                e.data["side"],
                str(Decimal(str(e.data["qty"])).normalize()),
                e.data["dry_run"],
            )
            for e in self.events
        ]


def rig(
    http: httpx.AsyncClient,
    sock: FakeSocket | None = None,
    **settings: Any,
) -> Rig:
    bus = EventBus(Database(None))
    anchor = Anchor()
    engine = MoveEngine(
        bus,
        make_settings(**settings),
        anchor,
        http,
        connector=fake_connector(sock or FakeSocket([])),
        post_timeout_s=0.1,
        take_timeout_s=0.1,
    )
    r = Rig(engine, bus, anchor)

    async def collect(event: StoredEvent) -> None:
        r.events.append(event)

    bus.on("trade.posted", collect)
    bus.on("trade.filled", collect)
    return r


LIVE = {
    "trading_enabled": True,
    "polymarket_access_key": "pm-test-key",
    "polymarket_private_key": PM_SECRET,
    "gemini_api_key": "gem-test-key",
    "gemini_api_secret": "gem-test-secret",
}


def req(qty: str = "2", mode: str = "take") -> MarketMoveRequest:
    return MarketMoveRequest(slug=SLUG, qty=Decimal(qty), mode=mode)


# --------------------------------------------------------------------------- guards


@respx.mock
async def test_notional_over_the_limit_is_refused_before_anything_is_signed() -> None:
    mock_books(respx.mock)
    async with httpx.AsyncClient() as http:
        r = rig(http)
        with pytest.raises(GuardRefused, match="over MAX_ORDER_USD"):
            await r.engine.execute(req("5"), SYMBOL)  # 5 x 0.48 = $2.40 > $2
    assert r.engine.records == {} and r.events == [] and r.anchor.commits == []


@respx.mock
async def test_the_hard_ceiling_cannot_be_raised_by_a_setting() -> None:
    mock_books(respx.mock)
    async with httpx.AsyncClient() as http:
        r = rig(http, max_order_usd=Decimal("500"))
        assert r.engine._settings.effective_max_order_usd == Decimal("2")
        with pytest.raises(GuardRefused, match="MAX_ORDER_USD"):
            await r.engine.execute(req("5"), SYMBOL)


@respx.mock
async def test_a_lower_setting_lowers_the_limit() -> None:
    mock_books(respx.mock)
    async with httpx.AsyncClient() as http:
        r = rig(http, max_order_usd=Decimal("0.5"))
        with pytest.raises(GuardRefused, match="MAX_ORDER_USD"):
            await r.engine.execute(req("2"), SYMBOL)  # $0.96 > $0.50


@respx.mock
async def test_below_the_minimum_size_is_refused_without_any_request() -> None:
    async with httpx.AsyncClient() as http:
        r = rig(http)
        with pytest.raises(GuardRefused, match="minimum"):
            await r.engine.execute(req("0.005"), SYMBOL)


@respx.mock
async def test_trading_enabled_without_keys_is_refused() -> None:
    mock_books(respx.mock)
    orders = respx.post(f"{TRADING}/v1/orders")
    async with httpx.AsyncClient() as http:
        r = rig(http, trading_enabled=True)
        with pytest.raises(GuardRefused, match="keys are not all set"):
            await r.engine.execute(req(), SYMBOL)
    assert not orders.called


@respx.mock
async def test_one_missing_key_is_enough_to_refuse() -> None:
    mock_books(respx.mock)
    async with httpx.AsyncClient() as http:
        r = rig(http, None, **{**LIVE, "gemini_api_secret": ""})
        with pytest.raises(GuardRefused, match="keys are not all set"):
            await r.engine.execute(req(), SYMBOL)


# --------------------------------------------------------------------------- dry run


@respx.mock
async def test_dry_run_transcript_sends_nothing_and_shows_the_exact_requests() -> None:
    mock_books(respx.mock)
    pm_orders = respx.post(f"{TRADING}/v1/orders")
    gem_orders = respx.post(f"{GEMINI_API}/v1/prediction-markets/order")
    async with httpx.AsyncClient() as http:
        r = rig(http)  # TRADING_ENABLED is false
        move_id = await r.engine.execute(req("2", "take"), SYMBOL)

    assert not pm_orders.called and not gem_orders.called
    rec = r.engine.records[move_id]
    assert (rec.status, rec.dry_run) == ("dry_run", True)

    # The events P2's dashboard consumes, all marked dry_run.
    assert r.trades == [
        ("trade.posted", "polymarket", "sell", "2", True),
        ("trade.filled", "polymarket", "sell", "2", True),
        ("trade.posted", "gemini", "buy", "2", True),
        ("trade.filled", "gemini", "buy", "2", True),
    ]

    # The requests, signed with the dummy key.
    sell, buy = rec.requests
    assert sell["method"] == "POST" and sell["url"] == f"{TRADING}/v1/orders"
    assert json.loads(sell["body"]) == {
        "marketSlug": SLUG,
        "intent": "ORDER_INTENT_SELL_LONG",
        "type": "ORDER_TYPE_LIMIT",
        "price": {"value": "0.47", "currency": "USD"},
        "quantity": 2.0,
        "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
    }
    assert sell["headers"]["X-PM-Access-Key"] == "DRY-RUN-DUMMY"
    stamp = int(sell["headers"]["X-PM-Timestamp"])
    assert sell["headers"]["X-PM-Signature"] == sign_request(
        DUMMY_PM_SECRET, stamp, "POST", "/v1/orders"
    )
    payload = json.loads(base64.b64decode(buy["headers"]["X-GEMINI-PAYLOAD"]))
    assert payload["request"] == "/v1/prediction-markets/order"
    assert (payload["symbol"], payload["side"], payload["outcome"]) == (SYMBOL, "buy", "yes")
    assert (payload["quantity"], payload["price"]) == ("2", "0.48")
    assert payload["timeInForce"] == "immediate-or-cancel"
    expected = hmac.new(
        DUMMY_GEMINI_SECRET.encode(),
        buy["headers"]["X-GEMINI-PAYLOAD"].encode(),
        hashlib.sha384,
    ).hexdigest()
    assert buy["headers"]["X-GEMINI-SIGNATURE"] == expected
    assert buy["headers"]["X-GEMINI-APIKEY"] == "DRY-RUN-DUMMY"

    # Recorded onchain: the hash of the trade record, not of the signed requests.
    [(commitment, label)] = r.anchor.commits
    assert label == f"trade:{move_id}" and rec.anchor_ref == "anchor:1"
    canonical = json.dumps(rec.commitment_payload(), sort_keys=True, separators=(",", ":"))
    assert commitment == hashlib.sha256(canonical.encode()).hexdigest() == rec.commitment

    # The cash instruction is on the record, marked imaginary, and not in the real queue.
    assert rec.rebalance[0].text.startswith("DRY RUN: Move $0.96 from Polymarket US to Gemini")
    assert r.engine.rebalance_queue == []


@respx.mock
async def test_post_mode_dry_run_rests_the_ask_inside_the_spread_as_good_till_cancel() -> None:
    mock_books(respx.mock)
    async with httpx.AsyncClient() as http:
        r = rig(http)
        move_id = await r.engine.execute(req("2", "post"), SYMBOL)
    body = json.loads(r.engine.records[move_id].requests[0]["body"])
    assert body["tif"] == "TIME_IN_FORCE_GOOD_TILL_CANCEL"
    assert body["price"]["value"] == "0.48"  # one-tick spread: join the ask, never cross


@respx.mock
async def test_each_dry_run_gets_its_own_id_and_commitment() -> None:
    mock_books(respx.mock)
    async with httpx.AsyncClient() as http:
        r = rig(http)
        first = await r.engine.execute(req("1"), SYMBOL)
        second = await r.engine.execute(req("2"), SYMBOL)
    assert (first, second) == ("m_1", "m_2")
    assert r.engine.records[first].commitment != r.engine.records[second].commitment


# --------------------------------------------------------------------------- live paths


def mock_live(
    router: respx.MockRouter, gemini: httpx.Response | Exception | None = None
) -> dict[str, respx.Route]:
    mock_books(router)
    return {
        "pm_order": router.post(f"{TRADING}/v1/orders").mock(
            return_value=httpx.Response(200, json={"id": "o1"})
        ),
        "pm_cancel": router.post(f"{TRADING}/v1/order/o1/cancel").mock(
            return_value=httpx.Response(200)
        ),
        "gem_order": router.post(f"{GEMINI_API}/v1/prediction-markets/order").mock(
            **(
                {"side_effect": gemini}
                if isinstance(gemini, Exception)
                else {
                    "return_value": gemini
                    or httpx.Response(
                        200,
                        json={
                            "orderId": 77,
                            "status": "filled",
                            "filledQuantity": "4",
                            "avgExecutionPrice": "0.48",
                        },
                    )
                }
            )
        ),
    }


@respx.mock
async def test_live_full_fill_sells_then_buys_and_queues_the_cash_instruction() -> None:
    routes = mock_live(respx.mock)
    sock = FakeSocket([execution("o1", "1"), execution("o1", "3", "EXECUTION_TYPE_FILL")])
    async with httpx.AsyncClient() as http:
        r = rig(http, sock, **LIVE)
        move_id = await r.engine.execute(req("4", "take"), SYMBOL)

    rec = r.engine.records[move_id]
    assert (rec.status, rec.dry_run) == ("filled", False)
    assert rec.sold_qty == rec.bought_qty == Decimal(4)
    assert r.trades == [
        ("trade.posted", "polymarket", "sell", "4", False),
        ("trade.filled", "polymarket", "sell", "4", False),
        ("trade.posted", "gemini", "buy", "4", False),
        ("trade.filled", "gemini", "buy", "4", False),
    ]
    assert not routes["pm_cancel"].called
    # The sale was signed with the real key and sent with the real access key.
    sent = routes["pm_order"].calls.last.request
    assert sent.headers["X-PM-Access-Key"] == "pm-test-key"
    stamp = int(sent.headers["X-PM-Timestamp"])
    assert sent.headers["X-PM-Signature"] == sign_request(PM_SECRET, stamp, "POST", "/v1/orders")
    gem_sent = routes["gem_order"].calls.last.request
    assert gem_sent.headers["X-GEMINI-APIKEY"] == "gem-test-key"
    assert json.loads(sock.sent[0])["subscribe"]["marketSlugs"] == [SLUG]  # subscribed first
    [cash] = r.engine.rebalance_queue
    assert (cash.kind, cash.from_venue, cash.to_venue) == ("cash", "polymarket", "gemini")
    assert cash.amount_usd == Decimal("1.92")
    assert r.anchor.commits and rec.anchor_ref


@respx.mock
async def test_live_partial_fill_cancels_the_rest_and_buys_only_what_sold() -> None:
    routes = mock_live(
        respx.mock,
        httpx.Response(
            200, json={"orderId": 78, "filledQuantity": "1", "avgExecutionPrice": "0.48"}
        ),
    )
    async with httpx.AsyncClient() as http:
        r = rig(http, FakeSocket([execution("o1", "1")]), **LIVE)
        move_id = await r.engine.execute(req("4", "post"), SYMBOL)
    rec = r.engine.records[move_id]
    assert routes["pm_cancel"].called  # the resting remainder is cancelled
    assert json.loads(routes["pm_cancel"].calls.last.request.content) == {"marketSlug": SLUG}
    gem_payload = json.loads(
        base64.b64decode(routes["gem_order"].calls.last.request.headers["X-GEMINI-PAYLOAD"])
    )
    assert gem_payload["quantity"] == "1"
    assert (rec.status, rec.sold_qty, rec.bought_qty) == ("partial", Decimal(1), Decimal(1))
    assert any("cancelled" in n for n in rec.notes)
    assert [t[3] for t in r.trades if t[0] == "trade.filled"] == ["1", "1"]


@respx.mock
async def test_live_unfilled_order_times_out_cancels_and_buys_nothing() -> None:
    routes = mock_live(respx.mock)
    async with httpx.AsyncClient() as http:
        r = rig(http, FakeSocket([]), **LIVE)
        move_id = await r.engine.execute(req("4", "post"), SYMBOL)
    rec = r.engine.records[move_id]
    assert rec.status == "unfilled"
    assert routes["pm_cancel"].called and not routes["gem_order"].called
    assert [t[0] for t in r.trades] == ["trade.posted"]  # posted, never filled
    assert r.engine.rebalance_queue == []
    assert r.anchor.commits  # even a move that did nothing is recorded


@respx.mock
async def test_live_failed_second_leg_reports_the_stranded_leg_and_queues_a_rebalance() -> None:
    routes = mock_live(respx.mock, httpx.Response(500, json={"reason": "InsufficientFunds"}))
    async with httpx.AsyncClient() as http:
        r = rig(http, FakeSocket([execution("o1", "4", "EXECUTION_TYPE_FILL")]), **LIVE)
        move_id = await r.engine.execute(req("4", "take"), SYMBOL)
    rec = r.engine.records[move_id]
    assert routes["gem_order"].called
    assert rec.status == "stranded"
    assert rec.sold_qty == Decimal(4) and rec.bought_qty == 0
    assert any("Gemini leg failed" in n for n in rec.notes)
    assert any(n.startswith("STRANDED: sold 4") for n in rec.notes)
    [stranded] = r.engine.rebalance_queue
    assert stranded.kind == "stranded_leg" and stranded.amount_usd == Decimal(
        "1.84"
    )  # 4 x the 0.46 fill
    assert [t[:3] for t in r.trades if t[0] == "trade.filled"] == [
        ("trade.filled", "polymarket", "sell")
    ]


@respx.mock
async def test_live_partial_second_leg_strands_only_the_unbought_part() -> None:
    mock_live(
        respx.mock,
        httpx.Response(
            200, json={"orderId": 79, "filledQuantity": "3", "avgExecutionPrice": "0.48"}
        ),
    )
    async with httpx.AsyncClient() as http:
        r = rig(http, FakeSocket([execution("o1", "4", "EXECUTION_TYPE_FILL")]), **LIVE)
        move_id = await r.engine.execute(req("4", "take"), SYMBOL)
    rec = r.engine.records[move_id]
    assert rec.status == "stranded" and rec.bought_qty == Decimal(3)
    kinds = sorted(i.kind for i in r.engine.rebalance_queue)
    assert kinds == ["cash", "stranded_leg"]
    assert any("1 contracts were not bought" in n for n in rec.notes)


@respx.mock
async def test_live_rejected_sale_raises_and_never_touches_gemini() -> None:
    routes = mock_live(respx.mock)
    routes["pm_order"].mock(return_value=httpx.Response(500, json={"message": "no"}))
    async with httpx.AsyncClient() as http:
        r = rig(http, FakeSocket([]), **LIVE)
        with pytest.raises(VenueError, match="polymarket"):
            await r.engine.execute(req("4"), SYMBOL)
    assert not routes["gem_order"].called
    [rec] = r.engine.records.values()
    assert rec.status == "failed" and r.events == []


@respx.mock
async def test_a_trade_that_cannot_be_anchored_is_still_recorded_and_noted() -> None:
    mock_books(respx.mock)

    class Broken:
        async def commit(self, commitment_hex: str, label: str) -> str:
            raise RuntimeError("rpc down")

        async def reveal(self, anchor_ref: str, preimage: bytes) -> None:
            pass

    async with httpx.AsyncClient() as http:
        r = rig(http)
        r.engine._anchor = Broken()
        move_id = await r.engine.execute(req("1"), SYMBOL)
    assert "could not be anchored" in r.engine.records[move_id].notes[-1]
