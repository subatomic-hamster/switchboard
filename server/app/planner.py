"""Routes, costs, and the pay / ask / block decision.

The planner fixes the route and the amount from the owner's own sentence before anything from
outside is read. Executing a plan asks the vault for permission; the vault, not the planner,
decides. The planner's `preview` is only a prediction, so the quote can say what will happen.

Every decision resolves to one of three outcomes with a stated reason:

- pay:   within policy. The vault issues a single-use permit and the transfer starts.
- ask:   over the per-transfer cap, over the daily budget, or a payee that is not on the
         allowlist. The vault holds a pending request and the owner's phone rings.
- block: the vault is frozen, or the Stream is stale. Nothing is requested and nothing rings.
"""

from __future__ import annotations

import itertools
import json
import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from sqlmodel import col, select

from app.config import FIXTURES_DIR
from app.context import AppContext
from app.db import LedgerRow, PlanRow, to_micro, utcnow
from app.events import StoredEvent
from app.intent import IntentError, IntentParser
from app.schemas import (
    BlockedData,
    Chain,
    ChainTransferRequest,
    Decision,
    ExecuteResponse,
    ExecuteStatus,
    IntentResponse,
    MarketMoveIntent,
    Outcome,
    Reason,
    Route,
    RouteStep,
    TransferRequest,
)

log = logging.getLogger("switchboard.planner")

CENT = Decimal("0.01")
MAX_HOPS = 2


class PlanError(LookupError):
    """The plan does not exist or cannot be executed. The message is safe to show."""


@dataclass(frozen=True)
class Rail:
    rail: str
    source: Chain
    dest: Chain
    cost_usd: Decimal
    eta_s: int
    description: str


@dataclass(frozen=True)
class FxRail:
    rail: str
    chain: Chain
    cost_bps: Decimal
    eta_s: int
    description: str


def load_rails(path: Path | None = None) -> tuple[list[Rail], FxRail]:
    raw = json.loads((path or FIXTURES_DIR / "rails.json").read_text(encoding="utf-8"))
    rails = [
        Rail(
            rail=item["rail"],
            source=Chain(item["from"]),
            dest=Chain(item["to"]),
            cost_usd=Decimal(item["cost_usd"]),
            eta_s=int(item["eta_s"]),
            description=item["description"],
        )
        for item in raw["rails"]
    ]
    fx = raw["fx"]
    return rails, FxRail(
        rail=fx["rail"],
        chain=Chain(fx["chain"]),
        cost_bps=Decimal(fx["cost_bps"]),
        eta_s=int(fx["eta_s"]),
        description=fx["description"],
    )


def _paths(rails: list[Rail], source: Chain, dest: Chain) -> list[list[Rail]]:
    """Every path from source to dest of at most MAX_HOPS rails that never revisits a chain."""
    if source == dest:
        return [[rail] for rail in rails if rail.source == source and rail.dest == dest]
    found: list[list[Rail]] = []

    def walk(at: Chain, visited: tuple[Chain, ...], path: list[Rail]) -> None:
        for rail in rails:
            if rail.source != at or rail.dest == rail.source or rail.dest in visited:
                continue
            if rail.dest == dest:
                found.append([*path, rail])
            elif len(path) + 1 < MAX_HOPS:
                walk(rail.dest, (*visited, rail.dest), [*path, rail])

    walk(source, (source,), [])
    return found


def _fx_step(fx: FxRail, amount: Decimal) -> RouteStep:
    cost = (amount * fx.cost_bps / Decimal(10_000)).quantize(CENT, rounding=ROUND_HALF_UP)
    return RouteStep(
        rail=fx.rail,
        from_chain=fx.chain,
        to_chain=fx.chain,
        cost_usd=cost,
        eta_s=fx.eta_s,
        description=fx.description,
    )


def _steps(path: list[Rail]) -> list[RouteStep]:
    return [
        RouteStep(
            rail=rail.rail,
            from_chain=rail.source,
            to_chain=rail.dest,
            cost_usd=rail.cost_usd,
            eta_s=rail.eta_s,
            description=rail.description,
        )
        for rail in path
    ]


def find_routes(rails: list[Rail], fx: FxRail, request: TransferRequest) -> list[Route]:
    """The cheapest and the fastest route. Both are always returned, even when identical."""
    source, dest = request.source.chain, request.destination.chain
    candidates: list[list[RouteStep]] = []
    if request.to_asset == "EURC":
        # Conversion happens on Arc, so the route passes through it.
        first_legs = [[]] if source == fx.chain else _paths(rails, source, fx.chain)
        last_legs = [[]] if dest == fx.chain else _paths(rails, fx.chain, dest)
        for first, last in itertools.product(first_legs, last_legs):
            candidates.append([*_steps(first), _fx_step(fx, request.amount_usd), *_steps(last)])
    else:
        candidates = [_steps(path) for path in _paths(rails, source, dest)]
    if not candidates:
        return []

    def cost(steps: list[RouteStep]) -> Decimal:
        return sum((step.cost_usd for step in steps), Decimal(0))

    def eta(steps: list[RouteStep]) -> int:
        return sum(step.eta_s for step in steps)

    cheapest = min(candidates, key=lambda steps: (cost(steps), eta(steps)))
    fastest = min(candidates, key=lambda steps: (eta(steps), cost(steps)))
    return [
        Route(id="cheapest", cost_usd=cost(cheapest), eta_s=eta(cheapest), steps=cheapest),
        Route(id="fastest", cost_usd=cost(fastest), eta_s=eta(fastest), steps=fastest),
    ]


# --------------------------------------------------------------------------- speaking


def say_money(amount: Decimal) -> str:
    """A dollar amount the way an operator says it: "40 cents", "1 dollar 25", "200 dollars"."""
    amount = amount.quantize(CENT, rounding=ROUND_HALF_UP)
    dollars, cents = int(amount), int((amount - int(amount)) * 100)
    if dollars == 0:
        return f"{cents} cent{'' if cents == 1 else 's'}"
    unit = "dollar" if dollars == 1 else "dollars"
    return f"{dollars:,} {unit}" if cents == 0 else f"{dollars:,} {unit} {cents:02d}"


def say_time(seconds: int) -> str:
    if seconds < 90:
        return f"about {max(seconds, 1)} second{'' if seconds == 1 else 's'}"
    minutes = round(seconds / 60)
    return f"about {minutes} minute{'' if minutes == 1 else 's'}"


def spoken_quote(request: TransferRequest, routes: list[Route], decision: Decision) -> str:
    """The quote the operator reads back before anything executes."""
    if not routes:
        return "I have no route between those two wallets."
    cheapest, fastest = routes[0], routes[-1]
    target = f"{say_money(request.amount_usd)} to {request.destination.label}"
    if cheapest.steps == fastest.steps:
        quote = (
            f"Sending {target}. The cheapest route is also the fastest: "
            f"{say_money(cheapest.cost_usd)} and {say_time(cheapest.eta_s)}."
        )
    else:
        quote = (
            f"Sending {target}. Cheapest route is {say_money(cheapest.cost_usd)} and "
            f"{say_time(cheapest.eta_s)}. Fastest is {say_money(fastest.cost_usd)} and "
            f"{say_time(fastest.eta_s)}."
        )
    if decision.outcome == Outcome.PAY:
        return f"{quote} It is inside your limits. Go ahead?"
    if decision.outcome == Outcome.ASK:
        return f"{quote} {decision.detail} If you go ahead, I will call you to approve it."
    return f"{quote} {decision.detail} I cannot send it right now."


# --------------------------------------------------------------------------- the planner


class Planner:
    def __init__(self, ctx: AppContext, parser: IntentParser) -> None:
        self._ctx = ctx
        self._parser = parser
        self._rails, self._fx = load_rails()
        ctx.bus.on("transfer.sent", self._on_transfer_sent)
        ctx.bus.on("transfer.arrived", self._on_transfer_arrived)
        ctx.bus.on("approval.resolved", self._on_approval_resolved)
        ctx.bus.on("trade.filled", self._on_trade_filled)

    async def warm_up(self) -> None:
        """Parse one harmless sentence so the first real request is fast."""
        try:
            await self._parser.parse("send 1 dollar from my arc wallet to sam")
        except IntentError:
            log.info("intent warm-up did not parse; continuing")

    def routes(self, request: TransferRequest) -> list[Route]:
        return find_routes(self._rails, self._fx, request)

    async def preview(self, request: TransferRequest) -> Decision:
        """Predict what the vault will say. The same checks, in the contract's order."""
        ctx = self._ctx
        if await ctx.vault.is_frozen():
            return Decision(
                outcome=Outcome.BLOCK,
                reason=Reason.FROZEN,
                detail="The vault is frozen.",
            )
        if ctx.stream_stale:
            return Decision(
                outcome=Outcome.BLOCK,
                reason=Reason.STREAM_STALE,
                detail="The event stream has gone quiet, so spending is paused.",
            )
        policy = await ctx.vault.get_policy()
        amount = request.amount_usd
        if amount > policy.transfer_cap_usd:
            return Decision(
                outcome=Outcome.ASK,
                reason=Reason.OVER_TRANSFER_CAP,
                detail=f"That is over your {say_money(policy.transfer_cap_usd)} transfer limit.",
            )
        spent = await ctx.vault.spent_today()
        if spent + amount > policy.daily_budget_usd:
            return Decision(
                outcome=Outcome.ASK,
                reason=Reason.OVER_DAILY_BUDGET,
                detail=(
                    f"That would take today's spending past your "
                    f"{say_money(policy.daily_budget_usd)} daily budget."
                ),
            )
        address = (request.destination.address or "").lower()
        if address not in {allowed.lower() for allowed in policy.allowlist}:
            return Decision(
                outcome=Outcome.ASK,
                reason=Reason.PAYEE_NOT_ALLOWED,
                detail="That destination is not one you have paid before.",
            )
        return Decision(outcome=Outcome.PAY, detail="Within your limits.")

    async def create_plan(self, text: str, source: str = "user") -> IntentResponse:
        """Parse the sentence, plan routes, store the plan. Nothing is executed."""
        parsed = await self._parser.parse(text)
        row = PlanRow(created=utcnow(), text=text, kind=parsed.kind, parser=parsed.parser)
        row.source = source
        if parsed.kind == "market_move":
            assert parsed.market_move is not None
            move = self._resolve_market(parsed.market_move)
            row.request_json = move.model_dump_json()
            plan_id = self._save(row)
            return IntentResponse(
                plan_id=plan_id,
                kind="market_move",
                market_move=move,
                parser=parsed.parser,
                spoken_quote=(
                    "Let me check that the two markets are the same bet before I quote the move."
                ),
            )

        assert parsed.transfer is not None
        request = parsed.transfer
        routes = self.routes(request)
        if not routes:
            raise IntentError("I have no route between those two wallets.")
        decision = await self.preview(request)
        row.request_json = request.model_dump_json()
        row.routes_json = json.dumps([route.model_dump(mode="json") for route in routes])
        plan_id = self._save(row)
        return IntentResponse(
            plan_id=plan_id,
            kind="transfer",
            request=request,
            routes=routes,
            spoken_quote=spoken_quote(request, routes, decision),
            preview=decision,
            parser=parsed.parser,
        )

    def _resolve_market(self, move: MarketMoveIntent) -> MarketMoveIntent:
        """Attach a market slug when the owner's words name a market we hold a listing for."""
        listing = getattr(self._ctx.markets, "listing", None)
        if move.slug or listing is None:
            return move
        hint_words = {word for word in move.market_hint.lower().split() if len(word) > 2}
        for market in listing():
            haystack = (
                f"{market.get('title', '')} {market.get('kind', '')} {market['slug']}".lower()
            )
            if hint_words and any(word in haystack for word in hint_words):
                return move.model_copy(update={"slug": market["slug"]})
        return move

    def _save(self, row: PlanRow) -> str:
        with self._ctx.db.session() as session:
            session.add(row)
            session.commit()
            assert row.id is not None
            return f"p_{row.id}"

    @staticmethod
    def _row_id(plan_id: str) -> int:
        if not plan_id.startswith("p_") or not plan_id[2:].isdigit():
            raise PlanError("unknown plan")
        return int(plan_id[2:])

    def _load(self, plan_id: str) -> PlanRow:
        with self._ctx.db.session() as session:
            row = session.get(PlanRow, self._row_id(plan_id))
        if row is None:
            raise PlanError("unknown plan")
        return row

    def _update(self, row: PlanRow) -> None:
        with self._ctx.db.session() as session:
            session.add(row)
            session.commit()

    async def execute(self, plan_id: str, route_id: str) -> ExecuteResponse:
        ctx = self._ctx
        row = self._load(plan_id)
        if row.kind != "transfer":
            raise PlanError("this plan is a market move; use the markets panel to run it")
        if row.status != "quoted":
            raise PlanError(f"this plan was already run (status: {row.status})")
        request = TransferRequest.model_validate_json(row.request_json)
        routes = [Route.model_validate(item) for item in json.loads(row.routes_json)]
        route = next((r for r in routes if r.id == route_id), None)
        if route is None:
            raise PlanError("unknown route")
        row.route_id = route.id

        async def block(reason: Reason, detail: str) -> ExecuteResponse:
            row.status = "blocked"
            self._update(row)
            await ctx.bus.publish(
                "blocked",
                BlockedData(
                    reason=reason,
                    plan_id=plan_id,
                    amount_usd=request.amount_usd,
                    payee_label=request.destination.label,
                    detail=detail,
                ),
            )
            return ExecuteResponse(status=ExecuteStatus.BLOCKED, reason=reason, detail=detail)

        if ctx.stream_stale:
            return await block(
                Reason.STREAM_STALE,
                "The event stream has gone quiet, so spending is paused until it recovers.",
            )
        if await ctx.vault.is_frozen():
            return await block(Reason.FROZEN, "The vault is frozen.")

        assert request.destination.address is not None
        result = await ctx.vault.request_spend(
            request.destination.address, request.amount_usd, purpose=f"plan {plan_id}"
        )
        if result.permitted:
            assert result.permit_id is not None
            row.permit_id = result.permit_id
            transfer_id = await self._redeem_and_send(row, request, route)
            return ExecuteResponse(
                status=ExecuteStatus.EXECUTING,
                transfer_id=transfer_id,
                detail="Within your limits. Sending now.",
            )
        if result.request_id is not None:
            row.status = "needs_approval"
            row.vault_request_id = result.request_id
            self._update(row)
            ctx.approvals.attach_plan(result.request_id, row)
            return ExecuteResponse(
                status=ExecuteStatus.NEEDS_APPROVAL,
                reason=result.reason,
                request_id=result.request_id,
                detail="The vault needs your approval. Your phone will ring.",
            )
        return await block(result.reason or Reason.FROZEN, "The vault refused the spend.")

    async def _redeem_and_send(self, row: PlanRow, request: TransferRequest, route: Route) -> str:
        ctx = self._ctx
        assert row.permit_id is not None and row.id is not None
        assert request.destination.address is not None
        await ctx.vault.redeem(row.permit_id)
        row.status = "executing"
        self._update(row)
        transfer_id = await ctx.transfers.start(
            ChainTransferRequest(
                from_chain=request.source.chain,
                to_chain=request.destination.chain,
                to=request.destination.address,
                amount_usd=request.amount_usd,
            ),
            plan_id=f"p_{row.id}",
            payee_label=request.destination.label,
            route_id=route.id,
            eta_s=route.eta_s,
        )
        row.transfer_id = transfer_id
        self._update(row)
        self._ledger_cost(row.id, transfer_id, route.cost_usd)
        return transfer_id

    def plan_for_request(self, request_id: str) -> PlanRow | None:
        with self._ctx.db.session() as session:
            return session.exec(
                select(PlanRow).where(PlanRow.vault_request_id == request_id)
            ).first()

    async def continue_after_approval(self, request_id: str, permit_id: str) -> str | None:
        """Called once the owner has approved: redeem the new permit and send."""
        row = self.plan_for_request(request_id)
        if row is None or row.status != "needs_approval":
            return None
        request = TransferRequest.model_validate_json(row.request_json)
        routes = [Route.model_validate(item) for item in json.loads(row.routes_json)]
        route = next((r for r in routes if r.id == row.route_id), routes[0])
        row.permit_id = permit_id
        return await self._redeem_and_send(row, request, route)

    # ----------------------------------------------------------------- bus handlers

    async def _on_approval_resolved(self, event: StoredEvent) -> None:
        if event.data.get("approved"):
            return
        row = self.plan_for_request(str(event.data.get("request_id")))
        if row is not None and row.status == "needs_approval":
            row.status = "denied"
            self._update(row)

    async def _on_transfer_sent(self, event: StoredEvent) -> None:
        data = event.data
        plan_id = str(data.get("plan_id") or "")
        with self._ctx.db.session() as session:
            existing = session.exec(
                select(LedgerRow).where(
                    LedgerRow.ref == data["transfer_id"], LedgerRow.kind == "transfer"
                )
            ).first()
            row = existing or LedgerRow(ts=event.ts, kind="transfer", ref=data["transfer_id"])
            row.plan_id = int(plan_id[2:]) if plan_id[2:].isdigit() else None
            row.from_place = data["from_chain"]
            row.to_place = data["to_chain"]
            row.amount_micro = to_micro(Decimal(str(data["amount_usd"])))
            row.payee_label = data.get("payee_label", "")
            row.status = "sent"
            session.add(row)
            session.commit()

    def _ledger_cost(self, plan_row_id: int, transfer_id: str, cost: Decimal) -> None:
        with self._ctx.db.session() as session:
            row = session.exec(
                select(LedgerRow).where(LedgerRow.ref == transfer_id, LedgerRow.kind == "transfer")
            ).first()
            if row is None:
                row = LedgerRow(ts=utcnow(), kind="transfer", ref=transfer_id, plan_id=plan_row_id)
            row.cost_micro = to_micro(cost)
            session.add(row)
            session.commit()

    async def _on_transfer_arrived(self, event: StoredEvent) -> None:
        transfer_id = event.data["transfer_id"]
        with self._ctx.db.session() as session:
            ledger = session.exec(
                select(LedgerRow).where(LedgerRow.ref == transfer_id, LedgerRow.kind == "transfer")
            ).first()
            if ledger is not None:
                ledger.status = "arrived"
                ledger.note = event.data.get("tx_hash", "")
                session.add(ledger)
            plan = session.exec(select(PlanRow).where(PlanRow.transfer_id == transfer_id)).first()
            if plan is not None:
                plan.status = "done"
                session.add(plan)
            session.commit()

    async def _on_trade_filled(self, event: StoredEvent) -> None:
        data = event.data
        with self._ctx.db.session() as session:
            session.add(
                LedgerRow(
                    ts=event.ts,
                    kind="trade",
                    ref=data["move_id"],
                    from_place=data["venue"] if data["side"] == "sell" else "",
                    to_place=data["venue"] if data["side"] == "buy" else "",
                    amount_micro=to_micro(Decimal(str(data["qty"])) * Decimal(str(data["price"]))),
                    status="dry_run" if data.get("dry_run") else "filled",
                    note=f"{data['side']} {data['qty']} {data['slug']} at {data['price']}",
                )
            )
            session.commit()

    def ledger(self, limit: int = 200) -> list[LedgerRow]:
        with self._ctx.db.session() as session:
            return list(
                session.exec(
                    select(LedgerRow).order_by(col(LedgerRow.id).desc()).limit(limit)
                ).all()
            )
