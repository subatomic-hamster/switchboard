"""In-process event bus and the `/events` server-sent-events endpoint.

Anything that happens to money is published here once. Three kinds of consumer read it:
handlers inside the server (for example, the voice module starts a call on
`approval.requested`), browsers over SSE, and the flight recorder, which replays the stored log.

P1 publishes from the Stream webhook with the same call:

    await bus.publish("approval.requested", ApprovalRequestedData(...))
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request
from sqlmodel import col, select
from sse_starlette import EventSourceResponse

from app.db import Database, EventRow, as_utc, utcnow
from app.schemas import EVENT_PAYLOADS, EventHistory, Model

log = logging.getLogger("switchboard.events")

MAX_HISTORY = 5000


@dataclass(frozen=True)
class StoredEvent:
    id: int
    type: str
    ts: datetime
    data: dict[str, Any]

    def to_wire(self) -> dict[str, Any]:
        return {"id": self.id, "type": self.type, "ts": self.ts.isoformat(), "data": self.data}


Handler = Callable[[StoredEvent], Awaitable[None]]


class EventBus:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._handlers: dict[str, list[Handler]] = {}
        self._listeners: set[asyncio.Queue[StoredEvent]] = set()

    def on(self, event_type: str, handler: Handler) -> None:
        """Register an in-process handler. Use "*" to receive every event."""
        if event_type != "*" and event_type not in EVENT_PAYLOADS:
            raise ValueError(f"unknown event type {event_type!r}")
        self._handlers.setdefault(event_type, []).append(handler)

    async def publish(self, event_type: str, data: Model | Mapping[str, Any]) -> StoredEvent:
        """Validate, store, then deliver to handlers (in order, awaited) and SSE listeners.

        Handlers are awaited so that by the time `publish` returns, their effects exist. A
        handler that fails is logged and does not stop the others.
        """
        payload_model = EVENT_PAYLOADS.get(event_type)
        if payload_model is None:
            raise ValueError(f"unknown event type {event_type!r}")
        payload = (
            data if isinstance(data, payload_model) else payload_model.model_validate(dict(data))
        )
        body = payload.model_dump(mode="json")
        ts = utcnow()
        with self._db.session() as session:
            row = EventRow(type=event_type, ts=ts, data=json.dumps(body))
            session.add(row)
            session.commit()
            assert row.id is not None
            event = StoredEvent(id=row.id, type=event_type, ts=ts, data=body)

        for queue in list(self._listeners):
            queue.put_nowait(event)
        for handler in [*self._handlers.get(event_type, []), *self._handlers.get("*", [])]:
            try:
                await handler(event)
            except Exception:
                log.exception("handler failed for %s #%s", event.type, event.id)
        return event

    def subscribe(self) -> asyncio.Queue[StoredEvent]:
        queue: asyncio.Queue[StoredEvent] = asyncio.Queue()
        self._listeners.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[StoredEvent]) -> None:
        self._listeners.discard(queue)

    def history(
        self,
        *,
        after_id: int = 0,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = MAX_HISTORY,
    ) -> list[StoredEvent]:
        statement = select(EventRow).where(col(EventRow.id) > after_id)
        if since is not None:
            statement = statement.where(col(EventRow.ts) >= since)
        if until is not None:
            statement = statement.where(col(EventRow.ts) <= until)
        statement = statement.order_by(col(EventRow.id)).limit(min(limit, MAX_HISTORY))
        with self._db.session() as session:
            rows = session.exec(statement).all()
        return [
            StoredEvent(id=row.id or 0, type=row.type, ts=as_utc(row.ts), data=json.loads(row.data))
            for row in rows
        ]

    def latest(self, event_type: str) -> StoredEvent | None:
        statement = (
            select(EventRow)
            .where(EventRow.type == event_type)
            .order_by(col(EventRow.id).desc())
            .limit(1)
        )
        with self._db.session() as session:
            row = session.exec(statement).first()
        if row is None:
            return None
        return StoredEvent(
            id=row.id or 0, type=row.type, ts=as_utc(row.ts), data=json.loads(row.data)
        )


async def event_stream(bus: EventBus, after_id: int) -> AsyncGenerator[dict[str, str]]:
    """Yield SSE messages: first anything stored after `after_id`, then live events.

    The listener queue is opened before the replay query, so no event falls in the gap;
    duplicates are dropped by id.
    """
    queue = bus.subscribe()
    try:
        last_sent = after_id
        if after_id > 0:
            for event in bus.history(after_id=after_id):
                last_sent = event.id
                yield _sse(event)
        while True:
            event = await queue.get()
            if event.id <= last_sent:
                continue
            last_sent = event.id
            yield _sse(event)
    finally:
        bus.unsubscribe(queue)


def _sse(event: StoredEvent) -> dict[str, str]:
    return {"event": event.type, "id": str(event.id), "data": json.dumps(event.to_wire())}


router = APIRouter()


@router.get("/events", include_in_schema=False)
async def events(
    request: Request,
    last_event_id: Annotated[str | None, Header()] = None,
) -> EventSourceResponse:
    """Live event stream. Each message's `event` field is the event type and its `data` is the
    JSON envelope `{id, type, ts, data}`. A reconnecting browser sends `Last-Event-ID` and gets
    what it missed."""
    bus: EventBus = request.app.state.ctx.bus
    after_id = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0
    return EventSourceResponse(event_stream(bus, after_id), ping=15)


@router.get("/api/events/history", response_model=EventHistory, tags=["events"])
async def events_history(
    request: Request,
    after_id: Annotated[int, Query(ge=0)] = 0,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_HISTORY)] = 1000,
) -> dict[str, Any]:
    """Stored events in order, for the flight recorder's scrub bar and for bots."""
    bus: EventBus = request.app.state.ctx.bus
    events_ = bus.history(after_id=after_id, since=since, until=until, limit=limit)
    return {"events": [event.to_wire() for event in events_]}
