"""Move a position: post a limit sell on Polymarket US, wait for the fill on its private
WebSocket, then buy the same quantity on Gemini. Cash is not moved (neither venue documents a
withdrawal endpoint); a rebalance instruction is queued instead.

Guards, checked in this order before any signed request is built or any order is sent:
1. quantity at least the 0.01 contract minimum;
2. notional (the larger of the sale and the purchase) at most `effective_max_order_usd`,
   which can never exceed the hard $2 ceiling in `app.config`;
3. `TRADING_ENABLED` false means a dry run: both legs are built and signed with a dummy key,
   logged and recorded, nothing is sent, and the events carry `dry_run=true`;
4. `TRADING_ENABLED` true but a key missing is refused.
The only reads before the guards are the two public order books, needed to know the prices.

Every move, dry or live, ends in a hash of the trade record committed through `AnchorPort`
("record each trade onchain"). Gemini trades settle on Gemini, not onchain; this is the record.

Outcomes: `filled`, `partial` (the sale filled in part; only that part is bought), `unfilled`
(nothing sold before the timeout; the order is cancelled), `stranded` (the sale filled but the
buy did not: the unmatched quantity is reported and a rebalance instruction queued) and
`dry_run`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import itertools
import json
import logging
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

import httpx

from app.config import Settings
from app.events import EventBus
from app.markets.book import VenueError
from app.markets.gemini import GeminiClient
from app.markets.polymarket import MIN_QTY, Connector, PolymarketClient
from app.markets.quote import TICK, post_price
from app.markets.stub import GuardRefused, UnknownMarket
from app.ports import AnchorPort
from app.schemas import MarketMoveRequest, TradeData

log = logging.getLogger("switchboard.markets")

# A throwaway Ed25519 seed and HMAC secret: dry-run requests are signed with these so they have
# the real shape, and can never authenticate anywhere.
DUMMY_PM_SECRET = base64.b64encode(b"switchboard-dry-run-dummy-key!!!").decode()
DUMMY_GEMINI_SECRET = "dry-run-dummy-secret"  # noqa: S105


@dataclass
class RebalanceInstruction:
    """Cash is on the wrong venue after a move. Someone has to act on this."""

    kind: str  # "cash" or "stranded_leg"
    from_venue: str
    to_venue: str
    amount_usd: Decimal
    text: str


@dataclass
class MoveRecord:
    move_id: str
    slug: str
    gemini_symbol: str
    mode: str
    qty: Decimal
    dry_run: bool
    status: str = "pending"
    sold_qty: Decimal = Decimal(0)
    sold_price: Decimal | None = None
    bought_qty: Decimal = Decimal(0)
    bought_price: Decimal | None = None
    pm_order_id: str = ""
    gemini_order_id: str = ""
    notes: list[str] = field(default_factory=list)
    rebalance: list[RebalanceInstruction] = field(default_factory=list)
    anchor_ref: str = ""
    commitment: str = ""
    # The exact requests built, signed with the dummy key in a dry run. Not hashed (signatures).
    requests: list[dict[str, Any]] = field(default_factory=list)

    def commitment_payload(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("requests", "anchor_ref", "commitment"):
            data.pop(key)
        payload: dict[str, Any] = json.loads(json.dumps(data, default=str))
        return payload


class MoveEngine:
    def __init__(
        self,
        bus: EventBus,
        settings: Settings,
        anchor: AnchorPort,
        http: httpx.AsyncClient,
        *,
        connector: Connector | None = None,
        first_id: int = 1,
        post_timeout_s: float = 300,
        take_timeout_s: float = 15,
    ) -> None:
        self._bus, self._settings, self._anchor = bus, settings, anchor
        live = settings.trading_enabled
        self._pm = PolymarketClient(
            http,
            access_key=settings.polymarket_access_key,
            private_key=settings.polymarket_private_key,
            live=live,
            connector=connector,
        )
        self._gem = GeminiClient(
            http,
            api_key=settings.gemini_api_key,
            api_secret=settings.gemini_api_secret,
            live=live,
        )
        self._pm_dry = PolymarketClient(
            http, access_key="DRY-RUN-DUMMY", private_key=DUMMY_PM_SECRET, live=False
        )
        self._gem_dry = GeminiClient(
            http, api_key="DRY-RUN-DUMMY", api_secret=DUMMY_GEMINI_SECRET, live=False
        )
        self._ids = itertools.count(first_id)
        self._lock = asyncio.Lock()
        self._post_timeout_s, self._take_timeout_s = post_timeout_s, take_timeout_s
        self.records: dict[str, MoveRecord] = {}
        self.rebalance_queue: list[RebalanceInstruction] = []

    @property
    def keys_present(self) -> bool:
        return self._pm.has_keys and self._gem.has_keys

    async def execute(
        self, request: MarketMoveRequest, gemini_symbol: str, tick: Decimal = TICK
    ) -> str:
        """Run one move. The caller has already checked the two markets are equivalent."""
        if request.qty < MIN_QTY:
            raise GuardRefused(f"the minimum size is {MIN_QTY} contracts")
        async with self._lock:
            pm_book = await self._pm.book(request.slug)
            gem_book = await self._gem.book(gemini_symbol)
            if pm_book.best_bid is None or pm_book.best_ask is None:
                raise UnknownMarket(f"{request.slug} has no two-sided book on Polymarket US")
            if gem_book.best_ask is None:
                raise UnknownMarket(f"{gemini_symbol} has no offers on Gemini")
            sell_price = (
                pm_book.best_bid.price if request.mode == "take" else post_price(pm_book, tick)
            )
            buy_price = gem_book.best_ask.price
            notional = max(sell_price, buy_price) * request.qty
            limit = self._settings.effective_max_order_usd
            if notional > limit:
                raise GuardRefused(f"order of ${notional:.2f} is over MAX_ORDER_USD (${limit})")
            dry_run = not self._settings.trading_enabled
            if not dry_run and not self.keys_present:
                raise GuardRefused(
                    "TRADING_ENABLED is true but the Gemini and Polymarket US keys are not all set"
                )
            record = MoveRecord(
                move_id=f"m_{next(self._ids)}",
                slug=request.slug,
                gemini_symbol=gemini_symbol,
                mode=request.mode,
                qty=request.qty,
                dry_run=dry_run,
            )
            self.records[record.move_id] = record
            if dry_run:
                await self._dry_run(record, sell_price, buy_price)
            else:
                await self._live(record, sell_price, buy_price)
            await self._anchor_record(record)
            return record.move_id

    # ------------------------------------------------------------------ dry run

    async def _dry_run(self, rec: MoveRecord, sell_price: Decimal, buy_price: Decimal) -> None:
        sell = self._pm_dry.prepare_order(
            rec.slug,
            "ORDER_INTENT_SELL_LONG",
            rec.qty,
            sell_price,
            tif=(
                "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
                if rec.mode == "take"
                else "TIME_IN_FORCE_GOOD_TILL_CANCEL"
            ),
        )
        buy = self._gem_dry.prepare_order(rec.gemini_symbol, "buy", rec.qty, buy_price)
        for prepared in (sell, buy):
            rec.requests.append(prepared.as_dict())
            log.info("DRY RUN, not sent: %s", json.dumps(prepared.as_dict()))
        rec.sold_qty, rec.sold_price = rec.qty, sell_price
        rec.bought_qty, rec.bought_price = rec.qty, buy_price
        rec.status = "dry_run"
        rec.notes.append("dry run: nothing was sent; both legs assumed to fill at their limit")
        await self._publish(
            "trade.posted", rec, "polymarket", rec.slug, "sell", rec.qty, sell_price
        )
        await self._publish(
            "trade.filled", rec, "polymarket", rec.slug, "sell", rec.qty, sell_price
        )
        await self._publish(
            "trade.posted", rec, "gemini", rec.gemini_symbol, "buy", rec.qty, buy_price
        )
        await self._publish(
            "trade.filled", rec, "gemini", rec.gemini_symbol, "buy", rec.qty, buy_price
        )
        self._queue_rebalance(rec)

    # --------------------------------------------------------------------- live

    async def _live(self, rec: MoveRecord, sell_price: Decimal, buy_price: Decimal) -> None:
        take = rec.mode == "take"
        sell_req = self._pm.prepare_order(
            rec.slug,
            "ORDER_INTENT_SELL_LONG",
            rec.qty,
            sell_price,
            tif=("TIME_IN_FORCE_IMMEDIATE_OR_CANCEL" if take else "TIME_IN_FORCE_GOOD_TILL_CANCEL"),
        )
        timeout = self._take_timeout_s if take else self._post_timeout_s
        try:
            async with self._pm.watch(rec.slug) as feed:
                rec.pm_order_id = await self._pm.place_order(sell_req)
                await self._publish(
                    "trade.posted", rec, "polymarket", rec.slug, "sell", rec.qty, sell_price
                )
                result = await feed.wait(rec.pm_order_id, rec.qty, timeout)
        except VenueError as exc:
            rec.status = "failed"
            rec.notes.append(f"Polymarket US leg failed before any fill was seen: {exc}")
            await self._anchor_record(rec)
            raise
        if result.qty < rec.qty and not take:
            await self._cancel(rec)
        if result.qty == 0:
            rec.status = "unfilled"
            rec.notes.append(
                f"nothing sold within {timeout:g}s; the order was "
                f"{'cancelled' if not take else 'immediate-or-cancel'}"
            )
            return
        rec.sold_qty, rec.sold_price = result.qty, result.avg_price
        assert result.avg_price is not None
        await self._publish(
            "trade.filled", rec, "polymarket", rec.slug, "sell", result.qty, result.avg_price
        )
        await self._buy(rec, buy_price)

    async def _cancel(self, rec: MoveRecord) -> None:
        try:
            await self._pm.cancel_order(rec.pm_order_id, rec.slug)
        except VenueError as exc:
            rec.notes.append(
                f"CANCEL FAILED for Polymarket US order {rec.pm_order_id}: {exc}. "
                "Check open orders by hand; it may still fill."
            )
            return
        # ponytail: a fill that lands between the timeout and the cancel is not seen here,
        # because the feed is already closed. Reconcile with the venue's positions if it matters.
        rec.notes.append(f"unfilled remainder of order {rec.pm_order_id} cancelled")

    async def _buy(self, rec: MoveRecord, buy_price: Decimal) -> None:
        qty = rec.sold_qty
        req = self._gem.prepare_order(rec.gemini_symbol, "buy", qty, buy_price)
        try:
            data = await self._gem.place_order(req)
            filled = Decimal(str(data.get("filledQuantity", "0")))
            avg = Decimal(str(data.get("avgExecutionPrice") or buy_price))
            rec.gemini_order_id = str(data.get("orderId", ""))
        except (VenueError, ArithmeticError) as exc:
            filled, avg = Decimal(0), buy_price
            rec.notes.append(f"Gemini leg failed: {exc}")
        await self._publish("trade.posted", rec, "gemini", rec.gemini_symbol, "buy", qty, buy_price)
        if filled > 0:
            rec.bought_qty, rec.bought_price = filled, avg
            await self._publish(
                "trade.filled", rec, "gemini", rec.gemini_symbol, "buy", filled, avg
            )
        if filled < qty:
            self._strand(rec, qty - filled)
        else:
            rec.status = "filled" if qty == rec.qty else "partial"
        self._queue_rebalance(rec)

    # ------------------------------------------------------------------ helpers

    def _strand(self, rec: MoveRecord, qty: Decimal) -> None:
        """The sale filled; `qty` of it was not bought on Gemini. The cash is on Polymarket US
        and no position exists anywhere for that quantity."""
        rec.status = "stranded"
        price = rec.sold_price or Decimal(0)
        text = (
            f"STRANDED: sold {rec.sold_qty:f} of {rec.slug} on Polymarket US at {price:f}, but "
            f"{qty:f} contracts were not bought on Gemini ({rec.gemini_symbol}). The cash is on "
            "Polymarket US and that quantity is held nowhere. Buy it on Gemini by hand, or buy "
            "back on Polymarket US."
        )
        rec.notes.append(text)
        rec.rebalance.append(
            RebalanceInstruction("stranded_leg", "polymarket", "gemini", qty * price, text)
        )
        log.error(text)

    def _queue_rebalance(self, rec: MoveRecord) -> None:
        """The purchase drew cash from Gemini, the sale credited Polymarket US."""
        if rec.bought_qty > 0 and rec.bought_price is not None:
            amount = rec.bought_qty * rec.bought_price
            text = (
                f"Move ${amount:.2f} from Polymarket US to Gemini: the sale credited Polymarket US "
                f"and the purchase of {rec.bought_qty:f} contracts drew the cash from Gemini."
            )
            rec.rebalance.append(RebalanceInstruction("cash", "polymarket", "gemini", amount, text))
        if rec.dry_run:
            # Imaginary money stays on the record; it does not go in the real queue.
            for item in rec.rebalance:
                item.text = f"DRY RUN: {item.text}"
        else:
            self.rebalance_queue.extend(rec.rebalance)

    async def _publish(
        self,
        event: str,
        rec: MoveRecord,
        venue: str,
        slug: str,
        side: str,
        qty: Decimal,
        price: Decimal,
    ) -> None:
        data = TradeData(
            move_id=rec.move_id,
            venue=venue,
            slug=slug,
            side=side,
            qty=qty,
            price=price,
            dry_run=rec.dry_run,
        )
        await self._bus.publish(event, data)

    async def _anchor_record(self, rec: MoveRecord) -> None:
        """Hash the trade record and commit the hash. Failure is logged, never hides the trade."""
        canonical = json.dumps(rec.commitment_payload(), sort_keys=True, separators=(",", ":"))
        rec.commitment = hashlib.sha256(canonical.encode()).hexdigest()
        try:
            rec.anchor_ref = await self._anchor.commit(rec.commitment, f"trade:{rec.move_id}")
        except Exception:
            log.exception("could not anchor trade %s", rec.move_id)
            rec.notes.append("the trade record could not be anchored onchain")
