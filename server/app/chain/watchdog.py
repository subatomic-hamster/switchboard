"""The dead man's switch: no authenticated Stream delivery for a while means spending stops.

The Stream is how this server learns what the vault did. QuickNode sends a heartbeat roughly
every 30 seconds even when the vault is quiet, so silence for `STREAM_STALE_AFTER_S` (default 90)
means the Stream is paused, broken or cut off, and the server is blind. Then `stream.stale` is
published with stale=true, and everything that reads `ctx.stream_stale` refuses to spend:

  planner.preview / execute, hostile, connectors (through the planner), the daily summary,
  and `Approvals._finalise`: an owner's yes is not settled onchain while stale. Approving is
  what releases money, so it waits for the Stream like any other spend. The request stays
  pending (and expires, which is the safe direction); denying is always allowed.

Only real transitions are published. The first delivery after a silence publishes stale=false.
At start-up there has been no delivery yet, so the Stream counts as stale until the first
heartbeat (the safe default).

`force()` is the demo override behind `/api/chain/stub/stream`: while forced, deliveries do not
clear the stale state. It is local-only like the rest of the dashboard's routes.

Only active with the real vault (`USE_P1_STUBS=false`). With stubs the same route publishes the
event directly.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

from app.context import AppContext
from app.db import as_utc, utcnow
from app.schemas import StreamStaleData

log = logging.getLogger("switchboard.chain")

CHECK_EVERY_S = 5.0


class Watchdog:
    def __init__(
        self,
        ctx: AppContext,
        clock: Callable[[], datetime] = utcnow,
        check_every_s: float = CHECK_EVERY_S,
    ) -> None:
        self._ctx = ctx
        self._clock = clock
        self._every = check_every_s
        self._started = clock()
        # Stale until the first heartbeat. Set on the context too, so a request that arrives
        # before the background task has run is already refused.
        self.stale = True
        ctx.stream_stale = True

    async def delivered(self) -> None:
        """An authenticated delivery arrived (heartbeat or logs)."""
        self._ctx.last_stream_delivery = self._clock()
        await self.check()

    async def force(self, stale: bool) -> None:
        self._ctx.stale_forced = stale
        await self.check()

    def seconds_quiet(self) -> int:
        last = self._ctx.last_stream_delivery or self._started
        return max(int((self._clock() - as_utc(last)).total_seconds()), 0)

    def _is_stale(self) -> bool:
        ctx = self._ctx
        if ctx.stale_forced or ctx.last_stream_delivery is None:
            return True
        return self.seconds_quiet() > ctx.settings.stream_stale_after_s

    async def check(self) -> None:
        """Publish `stream.stale` if, and only if, the state changed."""
        stale = self._is_stale()
        if stale == self.stale:
            return
        self.stale = stale
        quiet = self.seconds_quiet()
        if stale:
            log.warning("QuickNode Stream: no delivery for %ss, spending is paused", quiet)
        else:
            log.info("QuickNode Stream: delivering again, spending resumes")
        await self._ctx.bus.publish(
            "stream.stale", StreamStaleData(stale=stale, seconds_since_delivery=quiet)
        )

    async def run(self) -> None:
        if self.stale:
            log.warning(
                "QuickNode Stream: no delivery yet, so it counts as stale (spending paused) "
                "until the first heartbeat arrives"
            )
            await self._ctx.bus.publish("stream.stale", StreamStaleData(stale=True))
        while True:
            await asyncio.sleep(self._every)
            await self.check()
