"""Calling in (demo mode only): the owner rings the AgentPhone number and talks to the agent.

Each thing the caller says goes through the same planner as the dashboard's sentence box. The
agent reads back the quote (a send, a buy) or, for a move, the verdict from the five-field match
with Grok's wording, then waits for a yes or a no. Nothing runs without a yes, and the vault's
limits still apply: a send over the cap asks for approval the usual way.

Demo only, and any caller is taken to be the owner: caller ID can be faked, and the money here is
simulated. Off demo mode an inbound call is refused (`call_script.NOT_OUR_CALL`).
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.intent import IntentError
from app.markets.book import VenueError
from app.markets.stub import UnknownMarket
from app.planner import PlanError
from app.schemas import ExecuteStatus, MarketMoveRequest

if TYPE_CHECKING:
    from app.context import AppContext

log = logging.getLogger("switchboard.phone")

GREETING = (
    "Switchboard here. You can send money, put money into a market, or move a position. "
    "What would you like to do?"
)
ANYTHING_ELSE = "Anything else?"
YES = re.compile(
    r"\b(yes|yeah|yep|yup|sure|ok(ay)?|go ahead|do it|confirm(ed)?|send it|go for it|please do)\b",
    re.I,
)
NO = re.compile(r"\b(no|nope|cancel|stop|don'?t|never ?mind|hold off|wait)\b", re.I)
HELLO = re.compile(r"\W*(hi|hello|hey|hiya|good (morning|afternoon|evening))( there)?\W*", re.I)
BYE = re.compile(r"\b(bye|goodbye|that'?s (all|it)|hang up)\b", re.I)
# No request at all: "nothing", "nothing else", "no thanks", "I'm good", "all set".
NOTHING = re.compile(
    r"^\W*(no,? )?(nothing( else)?|no thanks?( you)?|i'?m (good|fine|all set)|all set|"
    r"that'?s everything)\b",
    re.I,
)
GOODBYE: dict[str, Any] = {"text": "Thank you, goodbye.", "hangup": True}


@dataclass
class _Pending:
    """What a yes would run: a quoted plan, or a move of `qty` contracts in `slug`."""

    plan_id: str = ""
    slug: str = ""
    qty: Decimal = Decimal(0)


STAGED = re.compile(r"simulat|practice|\bdemo|dry.?run|testnet|devnet|no real (order|money)", re.I)


def _unstaged(text: str) -> str:
    """`text` without the sentences that say the money is not real (shared quotes and Grok's
    wording can carry them)."""
    sentences = re.split(r"(?<=[.?!])\s+", text)
    return " ".join(s for s in sentences if not STAGED.search(s))


def _cents(price: Decimal) -> str:
    return f"{(price * 100).quantize(Decimal('0.1')).normalize():f} cents"


class PhoneLine:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        # ponytail: per-call state in memory, lost on restart; a restart ends the call anyway.
        self._pending: dict[str, _Pending] = {}
        self._greeted: set[str] = set()

    async def turn(self, data: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """One thing the caller said. Yields what to say back (`interim` chunks, then one).
        The caller hears it as the real thing: no sentence about practice money is spoken."""
        async for chunk in self._turn(data):
            yield {**chunk, "text": _unstaged(chunk["text"])}

    async def _turn(self, data: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        call_id = str(data.get("callId") or "")
        said = str(data.get("transcript") or "").strip()
        pending = self._pending.get(call_id)
        if not said and call_id in self._greeted and pending is None:
            # Greeted already and still no request: end the call.
            yield GOODBYE
            return
        if not said or HELLO.fullmatch(said):
            self._greeted.add(call_id)
            yield {"text": GREETING}
            return
        if pending is None and (NO.fullmatch(said.strip(" .!,")) or NOTHING.search(said)):
            # "No" to "Anything else?", or "nothing": no request, so end the call.
            yield GOODBYE
            return
        pending = self._pending.pop(call_id, None)
        if pending is not None:
            if NO.search(said):
                yield {"text": f"Okay, I won't. {ANYTHING_ELSE}"}
                return
            if YES.search(said):
                yield {"text": "On it.", "interim": True}
                yield {"text": f"{await self._run(pending)} {ANYTHING_ELSE}"}
                return
            # Neither: treat it as a new request, and the old one lapses.
        if BYE.search(said):
            yield GOODBYE
            return
        yield {"text": "One moment.", "interim": True}
        try:
            plan = await self._ctx.planner.create_plan(said, source="phone")
        except IntentError as exc:
            yield {"text": f"{exc} What would you like to do?"}
            return
        if plan.kind == "market_move" and plan.market_move is not None:
            move = plan.market_move
            text, ask = await self._move(move.slug or "", move.from_venue)
            if ask is not None:
                self._pending[call_id] = ask
            yield {"text": text}
            return
        self._pending[call_id] = _Pending(plan_id=plan.plan_id)
        yield {"text": plan.spoken_quote or "Shall I go ahead?"}

    async def _move(self, slug: str, from_venue: str) -> tuple[str, _Pending | None]:
        """The verdict on moving a position, and the move a yes would run (None: no move)."""
        demo = self._ctx.demo
        held = demo.held(slug) if demo is not None else None
        if demo is None or held is None or from_venue != "polymarket":
            return (
                "On a call I can move a position you hold on Polymarket US to Gemini. "
                f"I did not find one like that. {ANYTHING_ELSE}",
                None,
            )
        insight = await demo.insight(held.id)
        if insight.verdict == "against":
            return f"{insight.body} So I won't move it. {ANYTHING_ELSE}", None
        qty = held.contracts.quantize(Decimal(1), rounding="ROUND_DOWN")
        return (
            f"{insight.body} Shall I move all {qty:f} contracts to Gemini?",
            _Pending(slug=slug, qty=qty),
        )

    async def _run(self, pending: _Pending) -> str:
        ctx = self._ctx
        if pending.slug:
            try:
                match = await ctx.markets.match("polymarket", pending.slug)
                if match.verdict != "match":
                    return "The markets no longer match, so I did not move it."
                await ctx.markets.move(
                    MarketMoveRequest(slug=pending.slug, qty=pending.qty, mode="take")
                )
            except (VenueError, UnknownMarket) as exc:
                log.warning("phone move failed: %s", exc)
                return "The move did not go through. Try again in a moment."
            return f"Done. {pending.qty:f} contracts now sit on Gemini."
        try:
            result = await ctx.planner.execute(pending.plan_id, "cheapest")
        except PlanError as exc:
            return str(exc)
        if result.status == ExecuteStatus.NEEDS_APPROVAL:
            return (
                "That is over your limit, so the vault wants your approval. I will call you to "
                "approve it after this call."
            )
        if result.status == ExecuteStatus.BLOCKED:
            return f"The vault blocked it. {result.detail}"
        return self._feedback(pending.plan_id) or f"Done. {result.detail}"

    def _feedback(self, plan_id: str) -> str:
        """After a buy: what is now held and what the market says about it."""
        demo = self._ctx.demo
        buy = self._ctx.planner.market_buy(plan_id)
        held = demo.held(buy.slug) if demo is not None and buy is not None else None
        if buy is None or held is None:
            return ""
        chance = int((held.price * 100).quantize(Decimal(1)))
        return (
            f"Done. You bought {buy.contracts.normalize():f} {held.side} contracts at "
            f"{_cents(buy.price)}. You now hold {held.contracts.normalize():f} at an average of "
            f"{_cents(held.avg)}, and the market gives {held.side} about a {chance} percent "
            f"chance. If it wins you collect {held.contracts.quantize(Decimal(1)):f} dollars."
        )
