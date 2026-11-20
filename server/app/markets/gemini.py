"""Gemini prediction markets client.

Public (no key): events, one event, and an order book per contract.
Private (HMAC-SHA384 signed): place and cancel an order, positions, balances.

Orders are built into a `PreparedRequest` and are only sent when the client was created with
`live=True`. `app.markets.move` is the only caller that does that, after its guards pass.

Endpoints, verified against the docs and the live API on 2026-11-20:
- GET  /v1/prediction-markets/events?status=active&limit=50&offset=N   (paginated; response has
  `data` and `pagination.total`)          https://developer.gemini.com/rest-api/prediction-markets/events/list-events
- GET  /v1/prediction-markets/events/{ticker}
- GET  /v1/book/{instrumentSymbol}  (public, bids/asks as strings; the docs name WebSocket
  `@depth` streams for book data, but this REST route answers for prediction-market symbols)
- POST /v1/prediction-markets/order  (body fields travel in X-GEMINI-PAYLOAD, not the HTTP body)
                                            https://developer.gemini.com/rest-api/prediction-markets/order-management/place-order
- POST /v1/prediction-markets/order/cancel   {"orderId": int}
- POST /v1/prediction-markets/positions, POST /v1/balances
- Signing: https://developer.gemini.com/authentication/api-key
  X-GEMINI-PAYLOAD = base64(json payload incl. "request" and "nonce"),
  X-GEMINI-SIGNATURE = hex(HMAC-SHA384(base64 payload, api_secret)).

Not done here: Gemini requires the prediction-market terms to be accepted by API before the
first order (GET terms status / POST accept). That is an owner action, left out on purpose.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any, Literal

import httpx

from app.markets.book import Book, Level, PreparedRequest, VenueError
from app.markets.stub import GuardRefused, UnknownMarket

GEMINI_API = "https://api.gemini.com"
PAGE = 50

# Taker fee = rate x contracts x price x (1 - price). Gemini's fee page
# (https://www.gemini.com/fees/predictions, dated 2026-03-12) gives taker 0.07 and maker
# 0.0175 for contracts listed on or after 2026-03-18; older contracts follow a legacy schedule
# (0.05 taker). Confirmed by fetching the page on 2026-11-20.
GEMINI_TAKER_RATE = Decimal("0.07")
GEMINI_MAKER_RATE = Decimal("0.0175")


def sign_payload(api_secret: str, payload: Mapping[str, Any]) -> tuple[str, str]:
    """Returns (base64 payload, hex signature)."""
    encoded = base64.b64encode(json.dumps(payload).encode())
    signature = hmac.new(api_secret.encode(), encoded, hashlib.sha384).hexdigest()
    return encoded.decode(), signature


def contract_text(contract: Mapping[str, Any]) -> str:
    """A contract's rules as plain text. Live responses carry a rich-text document; recorded
    fixtures may carry the plain string."""
    desc = contract.get("description")
    if isinstance(desc, str):
        return desc
    if isinstance(desc, Mapping):
        return " ".join(str(part.get("value", "")) for part in desc.get("content", []))
    return ""


def parse_book(raw: Mapping[str, Any]) -> Book:
    def levels(rows: list[Mapping[str, Any]], reverse: bool) -> tuple[Level, ...]:
        parsed = [Level(Decimal(str(r["price"])), Decimal(str(r["amount"]))) for r in rows]
        return tuple(sorted(parsed, key=lambda lv: lv.price, reverse=reverse))

    return Book(bids=levels(raw.get("bids", []), True), asks=levels(raw.get("asks", []), False))


class GeminiClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        api_key: str = "",
        api_secret: str = "",
        base_url: str = GEMINI_API,
        live: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._http = http
        self._key = api_key
        self._secret = api_secret
        self._base = base_url.rstrip("/")
        self.live = live
        self._clock = clock
        self._last_nonce = 0

    @property
    def has_keys(self) -> bool:
        return bool(self._key and self._secret)

    # ------------------------------------------------------------------ public

    async def _get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        try:
            resp = await self._http.get(self._base + path, params=params, timeout=20)
            if resp.status_code == 404:
                raise UnknownMarket(f"gemini has no {path}")
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise VenueError("gemini", f"{path} failed: {exc}") from exc

    async def list_events(
        self,
        *,
        status: str = "active",
        category: str | None = None,
        search: str | None = None,
        max_events: int = 2000,
        page_size: int = PAGE,
    ) -> list[dict[str, Any]]:
        """Every matching event, following `offset` until `pagination.total` is reached.
        Pagination is not stable while events open and close, so results are de-duplicated."""
        events: dict[str, dict[str, Any]] = {}
        offset = 0
        while offset < max_events:
            params: dict[str, Any] = {"status": status, "limit": page_size, "offset": offset}
            if category:
                params["category"] = category
            if search:
                params["search"] = search
            page = await self._get("/v1/prediction-markets/events", params)
            rows = page.get("data", [])
            for row in rows:
                events.setdefault(row["ticker"], row)
            offset += page_size
            if not rows or offset >= int(page.get("pagination", {}).get("total", 0)):
                break
        return list(events.values())

    async def get_event(self, ticker: str) -> dict[str, Any]:
        event: dict[str, Any] = await self._get(f"/v1/prediction-markets/events/{ticker}")
        return event

    async def book(self, symbol: str) -> Book:
        return parse_book(await self._get(f"/v1/book/{symbol}"))

    # ----------------------------------------------------------------- private

    def _nonce(self) -> str:
        """Seconds since the epoch (the recommended time-based mode: within 30 s of server
        time). Strictly increasing within this client so two calls in one second differ."""
        self._last_nonce = max(int(self._clock()), self._last_nonce + 1)
        return str(self._last_nonce)

    def _signed(self, path: str, fields: Mapping[str, Any], note: str) -> PreparedRequest:
        payload = {"request": path, "nonce": self._nonce(), **fields}
        encoded, signature = sign_payload(self._secret, payload)
        return PreparedRequest(
            method="POST",
            url=self._base + path,
            headers={
                "Content-Type": "text/plain",
                "Content-Length": "0",
                "Cache-Control": "no-cache",
                "X-GEMINI-APIKEY": self._key,
                "X-GEMINI-PAYLOAD": encoded,
                "X-GEMINI-SIGNATURE": signature,
            },
            body=None,
            note=note,
        )

    def prepare_order(
        self,
        symbol: str,
        side: Literal["buy", "sell"],
        qty: Decimal,
        price: Decimal,
        *,
        time_in_force: Literal[
            "good-til-cancel", "immediate-or-cancel", "fill-or-kill"
        ] = "immediate-or-cancel",
        maker_or_cancel: bool = False,
    ) -> PreparedRequest:
        fields: dict[str, Any] = {
            "symbol": symbol,
            "orderType": "limit",
            "side": side,
            "quantity": f"{qty.normalize():f}",
            "price": f"{price.normalize():f}",
            "outcome": "yes",
            "timeInForce": time_in_force,
        }
        if maker_or_cancel:
            fields["makerOrCancel"] = True
        return self._signed(
            "/v1/prediction-markets/order", fields, f"gemini {side} {qty:f} {symbol} @ {price:f}"
        )

    def prepare_cancel(self, order_id: int) -> PreparedRequest:
        return self._signed(
            "/v1/prediction-markets/order/cancel", {"orderId": order_id}, f"cancel {order_id}"
        )

    def prepare_positions(self) -> PreparedRequest:
        return self._signed("/v1/prediction-markets/positions", {}, "positions")

    def prepare_balances(self) -> PreparedRequest:
        return self._signed("/v1/balances", {}, "balances")

    async def send(self, prepared: PreparedRequest, *, writes: bool) -> Any:
        """Send a prepared private request. An order or cancel (`writes=True`) needs `live`."""
        if not self.has_keys:
            raise GuardRefused("Gemini API key and secret are not set")
        if writes and not self.live:
            raise GuardRefused("this Gemini client is dry-run: it will not send an order")
        try:
            resp = await self._http.request(
                prepared.method, prepared.url, headers=prepared.headers, timeout=20
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise VenueError("gemini", f"{prepared.note} failed: {exc}") from exc

    async def place_order(self, prepared: PreparedRequest) -> dict[str, Any]:
        result: dict[str, Any] = await self.send(prepared, writes=True)
        return result

    async def cancel_order(self, order_id: int) -> Any:
        return await self.send(self.prepare_cancel(order_id), writes=True)

    async def positions(self) -> list[dict[str, Any]]:
        data = await self.send(self.prepare_positions(), writes=False)
        rows: list[dict[str, Any]] = data.get("positions", [])
        return rows

    async def balances(self) -> Any:
        return await self.send(self.prepare_balances(), writes=False)
