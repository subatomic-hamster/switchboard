"""The objects one running server shares: settings, database, event bus and P1's ports."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastapi import Request

from app.config import Settings
from app.db import Database
from app.events import EventBus
from app.ports import AnchorPort, MarketsPort, TransferPort, VaultPort
from app.wallets import WalletBook

if TYPE_CHECKING:
    from app.bots import Bots
    from app.chain.escrow import ChainEscrow
    from app.connectors import Connectors
    from app.forecast import Forecaster
    from app.holds import HoldingVault
    from app.hostile import HostileLab
    from app.market_brief import MarketDesk
    from app.passkey import Passkeys
    from app.planner import Planner
    from app.rules import Rules
    from app.speech import Speech
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
    # The real Escrow client; None on stubs or until ESCROW_ADDRESS is set (routes answer 503).
    escrow: ChainEscrow | None = None
    stream_stale: bool = False
    # Set by the real Stream webhook (app/chain/stream.py); the Phase 4 dead man's switch reads it.
    last_stream_delivery: datetime | None = None
    stream_seen: OrderedDict[tuple[str, int], None] = field(default_factory=OrderedDict)
    # Services are attached by app.main.build_context once the context exists.
    planner: Planner = field(init=False)
    approvals: Approvals = field(init=False)
    passkeys: Passkeys = field(init=False)
    forecaster: Forecaster = field(init=False)
    desk: MarketDesk = field(init=False)
    rules: Rules = field(init=False)
    hostile: HostileLab = field(init=False)
    bots: Bots = field(init=False)
    speech: Speech = field(init=False)
    connectors: Connectors = field(init=False)
    # The same object as `vault`, typed so the planner can place a server-side hold.
    holds: HoldingVault = field(init=False)

    async def aclose(self) -> None:
        await self.tasks.aclose()
        await self.approvals.aclose()
        await self.forecaster.aclose()
        await self.bots.aclose()
        await self.speech.aclose()
        self.db.close()


def get_ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx
