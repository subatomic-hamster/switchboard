"""The objects one running server shares: settings, database, event bus and P1's ports."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import Request

from app.config import Settings
from app.db import Database
from app.events import EventBus
from app.ports import AnchorPort, MarketsPort, TransferPort, VaultPort
from app.wallets import WalletBook

if TYPE_CHECKING:
    from app.forecast import Forecaster
    from app.market_brief import MarketDesk
    from app.passkey import Passkeys
    from app.planner import Planner
    from app.voice import Approvals

log = logging.getLogger("switchboard")


class Tasks:
    """Background coroutines owned by the server, cancelled together at shutdown."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(self._guard(coro, name), name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @staticmethod
    async def _guard(coro: Coroutine[Any, Any, Any], name: str) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("background task %s failed", name)

    async def aclose(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


@dataclass
class AppContext:
    settings: Settings
    db: Database
    bus: EventBus
    tasks: Tasks
    wallets: WalletBook
    vault: VaultPort
    transfers: TransferPort
    markets: MarketsPort
    anchor: AnchorPort
    stream_stale: bool = False
    # Services are attached by app.main.build_context once the context exists.
    planner: Planner = field(init=False)
    approvals: Approvals = field(init=False)
    passkeys: Passkeys = field(init=False)
    forecaster: Forecaster = field(init=False)
    desk: MarketDesk = field(init=False)

    async def aclose(self) -> None:
        await self.tasks.aclose()
        await self.approvals.aclose()
        await self.forecaster.aclose()
        self.db.close()


def get_ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx
