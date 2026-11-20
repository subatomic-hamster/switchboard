"""Shared plain types for the venue clients: an order book and a signed request that has not
been sent. Prices and quantities are `Decimal`, parsed from the strings the venues send."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


class VenueError(RuntimeError):
    """A venue could not be reached or answered with an error. The message names the venue."""

    def __init__(self, venue: str, message: str) -> None:
        super().__init__(f"{venue}: {message}")
        self.venue = venue


@dataclass(frozen=True)
class Level:
    price: Decimal
    qty: Decimal


@dataclass(frozen=True)
class Book:
    """Bids best (highest) first, asks best (lowest) first."""

    bids: tuple[Level, ...] = ()
    asks: tuple[Level, ...] = ()

    @property
    def best_bid(self) -> Level | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Level | None:
        return self.asks[0] if self.asks else None

    def walk(self, side: str, qty: Decimal) -> list[Level]:
        """The levels (price, quantity taken) a market order for `qty` would consume.
        `side` is the side of the book hit: "bids" when selling, "asks" when buying.
        Shorter than `qty` in total when the book is too thin."""
        out: list[Level] = []
        left = qty
        for level in self.bids if side == "bids" else self.asks:
            if left <= 0:
                break
            take = min(level.qty, left)
            out.append(Level(level.price, take))
            left -= take
        return out


@dataclass(frozen=True)
class PreparedRequest:
    """Exactly what would go on the wire. Building one sends nothing."""

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "url": self.url,
            "headers": dict(self.headers),
            "body": self.body,
            "note": self.note,
        }


class RateLimiter:
    """Spaces calls at least 1/rate seconds apart (Polymarket US: 25 requests/s per IP)."""

    def __init__(self, rate_per_s: float) -> None:
        self._gap = 1.0 / rate_per_s
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self._gap
        if delay > 0:
            await asyncio.sleep(delay)
