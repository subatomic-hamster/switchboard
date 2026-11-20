"""The QuickNode Streams webhook: vault logs in, bus events out. Mounted when the real vault is in
use (`USE_P1_STUBS=false`); the stub handler in `stub.py` serves the same path otherwise.

The Stream's dataset is Logs, narrowed by `server/quicknode/filter.js` to `{"logs": [...]}`. The
parser does not depend on that shape: it finds every log-shaped object (`address` and `topics`)
anywhere in the JSON, so a list of blocks, receipts or logs also works.

Published here, from the vault's logs, and nowhere else:
  ApprovalRequested (reason 1 to 3)  ->  approval.requested
  Resolved                           ->  approval.resolved   (via "chain")
  Frozen                             ->  frozen              (via "chain")
The Ledger (LEDGER_ADDRESS) and Escrow (ESCROW_ADDRESS) are watched too, by log address:
  Escrow Deposited / Claimed / Refunded  ->  escrow.updated
  Escrow PaymentRequested / PaymentPaid  ->  payment.updated
  Ledger Committed / Revealed            ->  nothing (the forecast module publishes its own
                                             forecast.committed and forecast.revealed)
Not published: ApprovalRequested with reason 4 (FROZEN: nothing to ask the owner), and Spent and
PermitIssued, because the planner's `transfers.start` already publishes `transfer.sent`.

Signature: HMAC-SHA256 with the Stream's security token over nonce + timestamp + the decoded
body, headers X-QN-Nonce, X-QN-Timestamp and X-QN-Signature. A gzip body is inflated first,
because Streams signs the uncompressed JSON.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import zlib
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request

from app.chain.contract import load_artifact
from app.chain.vault import REASONS, decode_vault_log, from_units
from app.context import AppContext, get_ctx
from app.schemas import (
    ApprovalRequestedData,
    ApprovalResolvedData,
    EscrowData,
    FrozenData,
    OkResponse,
    PaymentData,
    Reason,
)

log = logging.getLogger("switchboard.chain")

MAX_SKEW_S = 300
MAX_INFLATE_BYTES = 10 * 1024 * 1024
SEEN_LIMIT = 4096

Decoder = Callable[[dict[str, Any]], dict[str, Any] | None]
Publisher = Callable[[AppContext, dict[str, Any]], Awaitable[bool]]

router = APIRouter(prefix="/api", tags=["chain"])
Ctx = Annotated[AppContext, Depends(get_ctx)]


def verify(secret: str, headers: Any, body: bytes) -> bytes:
    """The decoded payload if the signature checks out; 401 if not, 413 if it inflates too big."""
    nonce, timestamp, signature = (
        headers.get("x-qn-nonce"),
        headers.get("x-qn-timestamp"),
        headers.get("x-qn-signature"),
    )
    if not secret or not (nonce and timestamp and signature):
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        if abs(time.time() - int(timestamp)) > MAX_SKEW_S:
            raise HTTPException(status_code=401, detail="stale timestamp")
        if headers.get("content-encoding", "").lower() == "gzip":
            # Inflated before authentication, so cap it: an unsigned gzip bomb must not be able
            # to exhaust memory.
            inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
            body = inflater.decompress(body, MAX_INFLATE_BYTES)
            if inflater.unconsumed_tail:
                raise HTTPException(status_code=413, detail="payload too large")
    except (ValueError, OSError, EOFError, zlib.error) as exc:
        raise HTTPException(status_code=401, detail="unauthorized") from exc
    mac = hmac.new(secret.encode(), nonce.encode() + timestamp.encode() + body, hashlib.sha256)
    if not hmac.compare_digest(mac.hexdigest(), signature.lower()):
        raise HTTPException(status_code=401, detail="bad signature")
    return body


def reject_replay(ctx: AppContext, nonce: str, timestamp: str) -> None:
    """A signed delivery re-sent inside the skew window is a replay: 401 on a repeated
    (nonce, timestamp). In memory and bounded: entries older than the window are dropped (the
    skew check refuses them anyway), and the cache never holds more than SEEN_LIMIT."""
    seen = ctx.stream_nonces
    cutoff = time.time() - MAX_SKEW_S
    while seen and (next(iter(seen.values())) < cutoff or len(seen) >= SEEN_LIMIT):
        seen.popitem(last=False)
    key = (nonce, timestamp)
    if key in seen:
        raise HTTPException(status_code=401, detail="replayed delivery")
    seen[key] = int(timestamp)


def walk_logs(node: object) -> Iterator[dict[str, Any]]:
    """Every log-shaped dict (address and topics) anywhere in the payload."""
    if isinstance(node, dict):
        if "address" in node and "topics" in node:
            yield node
        else:
            for value in node.values():
                yield from walk_logs(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk_logs(value)


def _already_seen(ctx: AppContext, entry: dict[str, Any]) -> bool:
    """QuickNode retries a delivery it thinks failed, so a log can arrive twice. Remember the
    (transaction, log index) pairs already published. In memory: a restart forgets them."""
    seen = ctx.stream_seen
    tx_hash, index = entry.get("transactionHash"), entry.get("logIndex")
    if tx_hash is None or index is None:
        return False
    try:
        key = (str(tx_hash).lower(), int(str(index), 0))
    except ValueError:
        return False
    if key in seen:
        return True
    seen[key] = None
    while len(seen) > SEEN_LIMIT:
        seen.popitem(last=False)
    return False


async def publish_log(ctx: AppContext, event: dict[str, Any]) -> bool:
    """Publish one decoded vault event as the bus event it maps to. True if something was sent."""
    name = event["event"]
    if name == "ApprovalRequested":
        reason = REASONS.get(event["reason"])
        if reason is None or reason is Reason.FROZEN:
            return False
        await ctx.bus.publish(
            "approval.requested",
            ApprovalRequestedData(
                request_id=str(event["requestId"]),
                agent=event["agent"],
                payee=event["payee"],
                amount_usd=from_units(event["amount"]),
                reason=reason,
            ),
        )
    elif name == "Resolved":
        await ctx.bus.publish(
            "approval.resolved",
            ApprovalResolvedData(
                request_id=str(event["requestId"]), approved=event["approved"], via="chain"
            ),
        )
    elif name == "Frozen":
        await ctx.bus.publish("frozen", FrozenData(frozen=event["frozen"], via="chain"))
    else:
        return False
    return True


async def publish_ledger_log(ctx: AppContext, event: dict[str, Any]) -> bool:
    """Ledger events are recognised but not published: the forecast module already publishes
    `forecast.committed` and `forecast.revealed` itself when it commits and reveals, so doing it
    here too would show each one twice."""
    return False


async def publish_escrow_log(ctx: AppContext, event: dict[str, Any]) -> bool:
    """Escrow events -> `escrow.updated` and `payment.updated`."""
    name = event["event"]
    zero = "0x" + "00" * 20
    if name == "Deposited":
        await ctx.bus.publish(
            "escrow.updated",
            EscrowData(
                escrow_id=str(event["id"]),
                status="deposited",
                amount_usd=from_units(event["amount"]),
                expiry=datetime.fromtimestamp(event["expiry"], UTC),
            ),
        )
    elif name in ("Claimed", "Refunded"):
        await ctx.bus.publish(
            "escrow.updated",
            EscrowData(
                escrow_id=str(event["id"]),
                status="claimed" if name == "Claimed" else "refunded",
                amount_usd=from_units(event["amount"]),
                to=event["to"] if name == "Claimed" else event["refundTo"],
            ),
        )
    elif name == "PaymentRequested":
        payer = event["payer"]
        await ctx.bus.publish(
            "payment.updated",
            PaymentData(
                request_id=str(event["id"]),
                status="requested",
                amount_usd=from_units(event["amount"]),
                requester=event["requester"],
                payer="" if payer.lower() == zero else payer,
                memo=event["memo"],
            ),
        )
    elif name == "PaymentPaid":
        await ctx.bus.publish(
            "payment.updated",
            PaymentData(
                request_id=str(event["id"]),
                status="paid",
                amount_usd=from_units(event["amount"]),
                requester=event["requester"],
                payer=event["payer"],
            ),
        )
    else:
        return False  # ClaimCommitted is plumbing, not news
    return True


@router.post("/hooks/quicknode", response_model=OkResponse)
async def quicknode_hook(request: Request, ctx: Ctx) -> OkResponse:
    """QuickNode Streams delivery of the vault's logs."""
    body = verify(ctx.settings.quicknode_stream_secret, request.headers, await request.body())
    key = (request.headers["x-qn-nonce"], request.headers["x-qn-timestamp"])
    reject_replay(ctx, *key)
    try:
        return await _handle(ctx, body)
    except Exception:
        # A delivery we failed to handle must stay retryable: QuickNode retries with the same
        # nonce, and answering every retry 401 would terminate the Stream after 10 tries.
        ctx.stream_nonces.pop(key, None)
        raise


async def _handle(ctx: AppContext, body: bytes) -> OkResponse:
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc
    # Every authenticated delivery, heartbeat or logs, feeds the dead man's switch.
    assert ctx.watchdog is not None
    await ctx.watchdog.delivered()
    s = ctx.settings
    # Each watched contract has its own decoder and publisher, chosen by the log's address.
    watched: dict[str, tuple[Decoder, Publisher]] = {}
    if s.vault_address:
        watched[s.vault_address.lower()] = (decode_vault_log, publish_log)
    if s.ledger_address:
        watched[s.ledger_address.lower()] = (load_artifact("Ledger").decode, publish_ledger_log)
    if s.escrow_address:
        watched[s.escrow_address.lower()] = (load_artifact("Escrow").decode, publish_escrow_log)
    published = 0
    for entry in walk_logs(payload):
        handler = watched.get(str(entry["address"]).lower())
        if handler is None:
            continue
        decode_log, publish = handler
        event = decode_log(entry)
        if event is None or _already_seen(ctx, entry):
            continue
        if await publish(ctx, event):
            published += 1
    return OkResponse(ok=True, status=f"published {published}")
