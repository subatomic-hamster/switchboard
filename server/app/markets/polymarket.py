"""Polymarket US client.

Market data (public, no key) is on a different host from trading:
- GET https://gateway.polymarket.us/v1/market/slug/{slug}     one market
- GET https://gateway.polymarket.us/v1/markets?slug=...       list / filter (limit, offset)
- GET https://gateway.polymarket.us/v1/markets/{slug}/book    bids and asks: {px:{value}, qty}
- GET https://gateway.polymarket.us/v1/markets/{slug}/bbo     best bid and offer
- GET https://gateway.polymarket.us/v1/search?query=...       events, each with its markets
  (docs index: https://docs.polymarket.us/llms.txt)

Trading (Ed25519-signed) is at https://api.polymarket.us/v1:
- POST /v1/orders                   https://docs.polymarket.us/api-reference/orders/create-order
- POST /v1/order/{orderId}/cancel   https://docs.polymarket.us/api-reference/orders/cancel-order.md
- GET  /v1/account/balances, GET /v1/portfolio/positions
- WS   wss://api.polymarket.us/v1/ws/private   (fills: SUBSCRIPTION_TYPE_ORDER)
  https://docs.polymarket.us/api-reference/websocket/private
- Signing (https://docs.polymarket.us/api-reference/authentication): headers X-PM-Access-Key,
  X-PM-Timestamp (milliseconds, within 30 s of server time) and X-PM-Signature =
  base64(Ed25519(f"{timestamp}{METHOD}{path}")), path without the query string. The secret
  key is base64; its first 32 bytes are the Ed25519 seed.
- Rate limit: 25 requests/s shared across endpoints (docs: api-reference/rate-limits.md).

Orders are built into a `PreparedRequest` and sent only by a client created with `live=True`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.markets.book import Book, Level, PreparedRequest, RateLimiter, VenueError
from app.markets.stub import GuardRefused, UnknownMarket

log = logging.getLogger("switchboard.markets")

GATEWAY = "https://gateway.polymarket.us"
TRADING = "https://api.polymarket.us"
WS_PRIVATE = "wss://api.polymarket.us/v1/ws/private"
WS_PATH = "/v1/ws/private"
# 0.095 x contracts x price x (1 - price), makers pay nothing. Published in the plan and in each
# market's `feeCoefficient` (0.095 on every market read on 2026-11-20).
POLYMARKET_TAKER_RATE = Decimal("0.095")
MIN_QTY = Decimal("0.01")

Intent = Literal["ORDER_INTENT_SELL_LONG", "ORDER_INTENT_BUY_LONG"]


def signing_key(secret_b64: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(secret_b64)[:32])


def sign_request(secret_b64: str, timestamp_ms: int, method: str, path: str) -> str:
    """base64 Ed25519 signature over `{timestamp_ms}{METHOD}{path}` (no separators)."""
    message = f"{timestamp_ms}{method.upper()}{path}".encode()
    return base64.b64encode(signing_key(secret_b64).sign(message)).decode()


def parse_book(raw: Mapping[str, Any]) -> Book:
    data = raw.get("marketData", raw)

    def levels(rows: list[Mapping[str, Any]], reverse: bool) -> tuple[Level, ...]:
        parsed = [Level(Decimal(str(r["px"]["value"])), Decimal(str(r["qty"]))) for r in rows]
        return tuple(sorted(parsed, key=lambda lv: lv.price, reverse=reverse))

    return Book(
        bids=levels(data.get("bids", []), True),
        asks=levels(data.get("offers", data.get("asks", [])), False),
    )


@dataclass(frozen=True)
class Fill:
    order_id: str
    qty: Decimal
    price: Decimal


def parse_fill(message: Mapping[str, Any]) -> Fill | None:
    """A (partial) fill from a private-stream message, or None for anything else.

    Shape per the docs: orderSubscriptionUpdate.execution = {type, lastShares, lastPx:{value},
    order:{...}}. The docs do not show the order object's fields; its `id` is assumed to be the
    id `POST /v1/orders` returned.
    """
    execution = (message.get("orderSubscriptionUpdate") or {}).get("execution")
    if not isinstance(execution, Mapping):
        return None
    if execution.get("type") not in ("EXECUTION_TYPE_PARTIAL_FILL", "EXECUTION_TYPE_FILL"):
        return None
    order = execution.get("order") or {}
    try:
        return Fill(
            order_id=str(order.get("id", "")),
            qty=Decimal(str(execution["lastShares"])),
            price=Decimal(str(execution["lastPx"]["value"])),
        )
    except (KeyError, ArithmeticError):
        return None


class WsLike(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...


Connector = Callable[[], AbstractAsyncContextManager[WsLike]]


@dataclass(frozen=True)
class FillResult:
    qty: Decimal
    avg_price: Decimal | None


class FillSubscription:
    def __init__(self, ws: WsLike) -> None:
        self._ws = ws

    async def wait(self, order_id: str, qty: Decimal, timeout_s: float) -> FillResult:
        """Collect fills for `order_id` until `qty` is filled or `timeout_s` passes.
        Returns what filled (possibly nothing, possibly part)."""
        filled = Decimal(0)
        notional = Decimal(0)
        deadline = time.monotonic() + timeout_s
        while filled < qty:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=left)
            except TimeoutError:
                break
            try:
                fill = parse_fill(json.loads(raw))
            except ValueError:
                continue
            if fill is not None and fill.order_id == order_id:
                filled += fill.qty
                notional += fill.qty * fill.price
        return FillResult(filled, notional / filled if filled else None)


class PolymarketClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        access_key: str = "",
        private_key: str = "",
        gateway: str = GATEWAY,
        trading: str = TRADING,
        live: bool = False,
        rate_per_s: float = 25,
        clock: Callable[[], float] = time.time,
        connector: Connector | None = None,
    ) -> None:
        self._http = http
        self._access = access_key
        self._secret = private_key
        self._gateway = gateway.rstrip("/")
        self._trading = trading.rstrip("/")
        self.live = live
        self._limiter = RateLimiter(rate_per_s)
        self._clock = clock
        self._connector = connector

    @property
    def has_keys(self) -> bool:
        return bool(self._access and self._secret)

    # ------------------------------------------------------------------ public

    async def _get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        await self._limiter.wait()
        try:
            resp = await self._http.get(self._gateway + path, params=params, timeout=20)
            if resp.status_code == 404:
                raise UnknownMarket(f"polymarket has no {path}")
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise VenueError("polymarket", f"{path} failed: {exc}") from exc

    async def market(self, slug: str) -> dict[str, Any]:
        data = await self._get(f"/v1/market/slug/{slug}")
        market: dict[str, Any] = data["market"]
        return market

    async def book(self, slug: str) -> Book:
        return parse_book(await self._get(f"/v1/markets/{slug}/book"))

    async def bbo(self, slug: str) -> dict[str, Any]:
        data = await self._get(f"/v1/markets/{slug}/bbo")
        bbo: dict[str, Any] = data["marketData"]
        return bbo

    async def search(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        data = await self._get("/v1/search", {"query": query, "limit": limit, "status": "open"})
        events: list[dict[str, Any]] = data.get("events", [])
        return events

    async def list_markets(self, *, max_markets: int = 500, **filters: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while len(out) < max_markets:
            data = await self._get("/v1/markets", {"limit": 100, "offset": len(out), **filters})
            rows = data.get("markets", [])
            out += rows
            if len(rows) < 100:
                break
        return out

    # ----------------------------------------------------------------- private

    def auth_headers(self, method: str, path: str) -> dict[str, str]:
        ts = int(self._clock() * 1000)
        return {
            "X-PM-Access-Key": self._access,
            "X-PM-Timestamp": str(ts),
            "X-PM-Signature": sign_request(self._secret, ts, method, path),
            "Content-Type": "application/json",
        }

    def _prepare(
        self, method: str, path: str, body: Mapping[str, Any] | None, note: str
    ) -> PreparedRequest:
        return PreparedRequest(
            method=method,
            url=self._trading + path,
            headers=self.auth_headers(method, path),
            body=json.dumps(body, separators=(",", ":")) if body is not None else None,
            note=note,
        )

    def prepare_order(
        self,
        slug: str,
        intent: Intent,
        qty: Decimal,
        price: Decimal,
        *,
        tif: Literal[
            "TIME_IN_FORCE_GOOD_TILL_CANCEL", "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
        ] = "TIME_IN_FORCE_GOOD_TILL_CANCEL",
    ) -> PreparedRequest:
        if qty < MIN_QTY:
            raise ValueError(f"minimum order size is {MIN_QTY} contracts")
        body = {
            "marketSlug": slug,
            "intent": intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{price.normalize():f}", "currency": "USD"},
            # The docs type quantity as a JSON number (double); 0.01 steps are exact enough.
            "quantity": float(qty),
            "tif": tif,
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
        }
        return self._prepare(
            "POST", "/v1/orders", body, f"polymarket {intent} {qty:f} {slug} @ {price:f}"
        )

    def prepare_cancel(self, order_id: str, slug: str) -> PreparedRequest:
        return self._prepare(
            "POST", f"/v1/order/{order_id}/cancel", {"marketSlug": slug}, f"cancel {order_id}"
        )

    def prepare_balances(self) -> PreparedRequest:
        return self._prepare("GET", "/v1/account/balances", None, "balances")

    def prepare_positions(self) -> PreparedRequest:
        return self._prepare("GET", "/v1/portfolio/positions", None, "positions")

    async def send(self, prepared: PreparedRequest, *, writes: bool) -> Any:
        """Send a prepared private request. An order or cancel (`writes=True`) needs `live`."""
        if not self.has_keys:
            raise GuardRefused("Polymarket US access key and private key are not set")
        if writes and not self.live:
            raise GuardRefused("this Polymarket client is dry-run: it will not send an order")
        await self._limiter.wait()
        try:
            resp = await self._http.request(
                prepared.method,
                prepared.url,
                headers=prepared.headers,
                content=prepared.body,
                timeout=20,
            )
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except (httpx.HTTPError, ValueError) as exc:
            raise VenueError("polymarket", f"{prepared.note} failed: {exc}") from exc

    async def place_order(self, prepared: PreparedRequest) -> str:
        data = await self.send(prepared, writes=True)
        return str(data["id"])

    async def cancel_order(self, order_id: str, slug: str) -> None:
        await self.send(self.prepare_cancel(order_id, slug), writes=True)

    async def balances(self) -> Any:
        return await self.send(self.prepare_balances(), writes=False)

    async def positions(self) -> Any:
        return await self.send(self.prepare_positions(), writes=False)

    # --------------------------------------------------------------- fill feed

    def _default_connector(self) -> AbstractAsyncContextManager[WsLike]:
        import websockets  # imported here so the module loads without a socket library in use

        # Same three headers as a REST call, signed over "GET" + the stream path. The docs'
        # websocket page says only "API key authentication in the connection handshake"; this
        # follows the authentication page and the vendor SDK's behaviour.
        return websockets.connect(WS_PRIVATE, additional_headers=self.auth_headers("GET", WS_PATH))

    @asynccontextmanager
    async def watch(self, slug: str) -> AsyncIterator[FillSubscription]:
        """Open the private stream and subscribe to this market's orders. Do this before
        placing the order so no fill is missed."""
        connect = self._connector or self._default_connector
        async with connect() as ws:
            await ws.send(
                json.dumps(
                    {
                        "subscribe": {
                            "requestId": f"fills-{slug}",
                            "subscriptionType": "SUBSCRIPTION_TYPE_ORDER",
                            "marketSlugs": [slug],
                        }
                    }
                )
            )
            yield FillSubscription(ws)
