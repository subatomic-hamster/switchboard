"""The objects one running server shares: settings, database, event bus and P1's ports."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

from app.config import Settings
from app.db import Database
from app.events import EventBus
from app.ports import AnchorPort, MarketsPort, TransferPort, VaultPort
from app.wallets import WalletBook

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
    services: dict[str, Any] = field(default_factory=dict)

    async def aclose(self) -> None:
        await self.tasks.aclose()
        self.db.close()


def get_ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx
