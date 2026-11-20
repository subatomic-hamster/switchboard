"""Venue clients: signing against vectors computed with openssl, request building, the
public-data parsers on recorded responses, pagination, the rate limiter, and the fill feed.
Nothing here reaches the network (respx) and nothing sends an order to a real venue."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal

import httpx
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.markets.book import RateLimiter, VenueError
from app.markets.gemini import GEMINI_API, GeminiClient, parse_book, sign_payload
from app.markets.polymarket import (
    GATEWAY,
    TRADING,
    PolymarketClient,
    parse_fill,
    sign_request,
)
from app.markets.polymarket import (
    parse_book as pm_parse_book,
)
from app.markets.stub import GuardRefused
from tests.markets_fixtures import load

# Vectors computed independently with `openssl dgst -sha384 -hmac` and
# `openssl pkeyutl -sign -rawin` (the Ed25519 seed is RFC 8032 test 1).
GEMINI_PAYLOAD = {
    "request": "/v1/prediction-markets/order",
    "nonce": "1700000000",
    "symbol": "GEMI-TEST-EVENT-YES",
    "orderType": "limit",
    "side": "buy",
    "quantity": "1.5",
    "price": "0.45",
    "outcome": "yes",
    "timeInForce": "immediate-or-cancel",
}
GEMINI_B64 = (
    "eyJyZXF1ZXN0IjogIi92MS9wcmVkaWN0aW9uLW1hcmtldHMvb3JkZXIiLCAibm9uY2UiOiAiMTcwMDAwMDAwMCIs"
    "ICJzeW1ib2wiOiAiR0VNSS1URVNULUVWRU5ULVlFUyIsICJvcmRlclR5cGUiOiAibGltaXQiLCAic2lkZSI6ICJi"
    "dXkiLCAicXVhbnRpdHkiOiAiMS41IiwgInByaWNlIjogIjAuNDUiLCAib3V0Y29tZSI6ICJ5ZXMiLCAidGltZUlu"
    "Rm9yY2UiOiAiaW1tZWRpYXRlLW9yLWNhbmNlbCJ9"
)
GEMINI_SIG = (
    "5de9be5582144766e384d82c2627d719e2edb87710a08af87af0b2597b955f54"
    "8928be3d218805b92425b602338a8440"
)
PM_SECRET = "nWGxne/9WmC6hEr0kuwsxERJxWl7MmkZcDusAxyuf2A="  # RFC 8032 test 1 seed, base64
PM_SIG = "l/ry9TVLSH7BiT8MhbMrgAvrZzz61esR9V567RoRuEXADHNFk1rza0I+erHgAdQjTsT5Hke29D+9cYizzg01DA=="


# ------------------------------------------------------------------------------ signing


def test_gemini_signature_matches_the_openssl_vector() -> None:
    encoded, signature = sign_payload("test-secret", GEMINI_PAYLOAD)
    assert encoded == GEMINI_B64
    assert signature == GEMINI_SIG


def test_polymarket_signature_matches_the_openssl_vector() -> None:
    assert sign_request(PM_SECRET, 1700000000000, "post", "/v1/orders") == PM_SIG


def test_polymarket_signature_verifies_and_accepts_a_64_byte_keypair_secret() -> None:
    seed = base64.b64decode(PM_SECRET)
    public = Ed25519PrivateKey.from_private_bytes(seed).public_key()
    public.verify(base64.b64decode(PM_SIG), b"1700000000000POST/v1/orders")  # raises if wrong
    keypair = base64.b64encode(seed + bytes(32)).decode()  # the SDK's 64-byte form
    assert sign_request(keypair, 1700000000000, "POST", "/v1/orders") == PM_SIG


def test_polymarket_signs_the_path_without_the_query_string() -> None:
    client = PolymarketClient(
        httpx.AsyncClient(), access_key="k", private_key=PM_SECRET, clock=lambda: 1700000000.0
    )
    headers = client.auth_headers("GET", "/v1/portfolio/positions")
    assert headers["X-PM-Timestamp"] == "1700000000000"
    assert headers["X-PM-Access-Key"] == "k"
    assert headers["X-PM-Signature"] == sign_request(
        PM_SECRET, 1700000000000, "GET", "/v1/portfolio/positions"
    )


# ------------------------------------------------------------------- request building


def gemini(live: bool = False) -> GeminiClient:
    return GeminiClient(
        httpx.AsyncClient(),
        api_key="DUMMY",
        api_secret="test-secret",
        live=live,
        clock=lambda: 1700000000.0,
    )


def test_gemini_order_request_is_signed_and_carries_no_body() -> None:
    prepared = gemini().prepare_order("GEMI-TEST-EVENT-YES", "buy", Decimal("1.5"), Decimal("0.45"))
    assert prepared.method == "POST"
    assert prepared.url == f"{GEMINI_API}/v1/prediction-markets/order"
    assert prepared.body is None
    assert prepared.headers["X-GEMINI-PAYLOAD"] == GEMINI_B64
    assert prepared.headers["X-GEMINI-SIGNATURE"] == GEMINI_SIG
    assert prepared.headers["X-GEMINI-APIKEY"] == "DUMMY"
    assert prepared.headers["Content-Length"] == "0"


def test_gemini_nonce_increases_within_one_second() -> None:
    client = gemini()
    first, second = client.prepare_positions(), client.prepare_balances()
    nonces = [
        json.loads(base64.b64decode(p.headers["X-GEMINI-PAYLOAD"]))["nonce"]
        for p in (first, second)
    ]
    assert nonces == ["1700000000", "1700000001"]


async def test_a_dry_run_gemini_client_refuses_to_send_an_order() -> None:
    client = gemini(live=False)
    prepared = client.prepare_order("S", "buy", Decimal(1), Decimal("0.5"))
    with respx.mock(assert_all_called=False) as router:
        route = router.post(f"{GEMINI_API}/v1/prediction-markets/order")
        with pytest.raises(GuardRefused):
            await client.place_order(prepared)
        assert not route.called


async def test_a_client_without_keys_refuses_even_reads() -> None:
    client = GeminiClient(httpx.AsyncClient())
    with pytest.raises(GuardRefused, match="not set"):
        await client.balances()


def pm_client(live: bool = False, **kw: object) -> PolymarketClient:
    return PolymarketClient(
        httpx.AsyncClient(),
        access_key="DUMMY",
        private_key=PM_SECRET,
        live=live,
        clock=lambda: 1700000000.0,
        rate_per_s=1000,
        **kw,  # type: ignore[arg-type]
    )


def test_polymarket_sell_order_body_matches_the_documented_shape() -> None:
    prepared = pm_client().prepare_order(
        "aec-nba-hou-den-2026-11-20", "ORDER_INTENT_SELL_LONG", Decimal("4"), Decimal("0.46")
    )
    assert prepared.url == f"{TRADING}/v1/orders"
    assert json.loads(prepared.body or "") == {
        "marketSlug": "aec-nba-hou-den-2026-11-20",
        "intent": "ORDER_INTENT_SELL_LONG",
        "type": "ORDER_TYPE_LIMIT",
        "price": {"value": "0.46", "currency": "USD"},
        "quantity": 4.0,
        "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
    }
    assert prepared.headers["X-PM-Signature"] == sign_request(
        PM_SECRET, 1700000000000, "POST", "/v1/orders"
    )


def test_minimum_order_size_is_enforced_on_polymarket() -> None:
    with pytest.raises(ValueError, match="minimum"):
        pm_client().prepare_order("s", "ORDER_INTENT_SELL_LONG", Decimal("0.009"), Decimal("0.5"))


async def test_a_dry_run_polymarket_client_refuses_to_send_an_order() -> None:
    client = pm_client(live=False)
    prepared = client.prepare_order("s", "ORDER_INTENT_SELL_LONG", Decimal(1), Decimal("0.5"))
    with pytest.raises(GuardRefused):
        await client.place_order(prepared)


# -------------------------------------------------------------- public data on fixtures


def test_polymarket_book_parses_into_decimals_best_first() -> None:
    raw = load("nba_hou_den")["books"]["polymarket"]["aec-nba-hou-den-2026-11-20"]
    book = pm_parse_book(raw)
    assert book.best_bid is not None and book.best_ask is not None
    assert isinstance(book.best_bid.price, Decimal)
    assert book.best_bid.price < book.best_ask.price
    assert [lv.price for lv in book.bids] == sorted((lv.price for lv in book.bids), reverse=True)


def test_gemini_book_parses_into_decimals_best_first() -> None:
    raw = load("nba_hou_den")["books"]["gemini"]["GEMI-NBA-2611210300-HOU-DEN-M-HOU"]
    book = parse_book(raw)
    assert book.best_bid is not None and book.best_ask is not None
    assert book.best_bid.price < book.best_ask.price


@respx.mock
async def test_gemini_events_follow_pagination_and_drop_duplicates() -> None:
    def page(offset: int, tickers: list[str]) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [{"ticker": t, "contracts": []} for t in tickers],
                "pagination": {"limit": 2, "offset": offset, "total": 5},
            },
        )

    route = respx.get(f"{GEMINI_API}/v1/prediction-markets/events")
    route.side_effect = [page(0, ["A", "B"]), page(2, ["B", "C"]), page(4, ["D"])]
    async with httpx.AsyncClient() as http:
        client = GeminiClient(http)
        events = await client.list_events(page_size=2)  # 50 in production
    assert [e["ticker"] for e in events] == ["A", "B", "C", "D"]
    offsets = [int(c.request.url.params["offset"]) for c in route.calls]
    assert offsets == [0, 2, 4]
    assert all(c.request.url.params["status"] == "active" for c in route.calls)


@respx.mock
async def test_an_unreachable_venue_raises_a_clear_error() -> None:
    respx.get(f"{GATEWAY}/v1/market/slug/x").mock(side_effect=httpx.ConnectError("boom"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(VenueError, match="polymarket: /v1/market/slug/x failed"):
            await PolymarketClient(http, rate_per_s=1000).market("x")


async def test_rate_limiter_spaces_calls() -> None:
    limiter = RateLimiter(100)  # 10 ms apart
    start = time.monotonic()
    for _ in range(6):
        await limiter.wait()
    assert time.monotonic() - start >= 0.045


# ------------------------------------------------------------------------- fill feed


def execution(order_id: str, shares: str, kind: str = "EXECUTION_TYPE_PARTIAL_FILL") -> str:
    return json.dumps(
        {
            "requestId": "fills",
            "subscriptionType": "SUBSCRIPTION_TYPE_ORDER",
            "orderSubscriptionUpdate": {
                "execution": {
                    "id": "e1",
                    "order": {"id": order_id},
                    "lastShares": shares,
                    "lastPx": {"value": "0.46", "currency": "USD"},
                    "type": kind,
                    "tradeId": "t1",
                }
            },
        }
    )


class FakeSocket:
    def __init__(self, frames: list[str], then_silence: bool = True) -> None:
        self.frames = list(frames)
        self.sent: list[str] = []
        self._silence = then_silence

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if self.frames:
            return self.frames.pop(0)
        if self._silence:
            await __import__("asyncio").sleep(10)
        raise AssertionError("no frame")


def fake_connector(sock: FakeSocket):  # type: ignore[no-untyped-def]
    @asynccontextmanager
    async def connect() -> AsyncIterator[FakeSocket]:
        yield sock

    return connect


def test_parse_fill_ignores_other_messages() -> None:
    assert parse_fill({"orderSubscriptionSnapshot": {}}) is None
    assert parse_fill(json.loads(execution("o", "1", "EXECUTION_TYPE_NEW"))) is None
    fill = parse_fill(json.loads(execution("o9", "0.25")))
    assert fill is not None and (fill.order_id, fill.qty, fill.price) == (
        "o9",
        Decimal("0.25"),
        Decimal("0.46"),
    )


async def test_watch_subscribes_then_collects_partial_fills_until_complete() -> None:
    sock = FakeSocket(
        [
            execution("other", "9"),
            execution("o1", "1"),
            "not json",
            execution("o1", "3", "EXECUTION_TYPE_FILL"),
        ]
    )
    client = pm_client(connector=fake_connector(sock))
    async with client.watch("slug-x") as sub:
        result = await sub.wait("o1", Decimal("4"), timeout_s=2)
    assert result.qty == Decimal("4") and result.avg_price == Decimal("0.46")
    subscribe = json.loads(sock.sent[0])["subscribe"]
    assert subscribe["subscriptionType"] == "SUBSCRIPTION_TYPE_ORDER"
    assert subscribe["marketSlugs"] == ["slug-x"]


async def test_watch_returns_the_partial_amount_when_time_runs_out() -> None:
    sock = FakeSocket([execution("o1", "1")])
    client = pm_client(connector=fake_connector(sock))
    async with client.watch("s") as sub:
        result = await sub.wait("o1", Decimal("4"), timeout_s=0.05)
    assert result.qty == Decimal("1")


async def test_watch_returns_nothing_when_nothing_fills() -> None:
    client = pm_client(connector=fake_connector(FakeSocket([])))
    async with client.watch("s") as sub:
        result = await sub.wait("o1", Decimal("4"), timeout_s=0.05)
    assert result.qty == 0 and result.avg_price is None
