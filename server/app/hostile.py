"""The hostile-content box: paste anything, and see that the worst outcome is a phone call.

Two things are shown for whatever text is submitted.

1. What actually happens. Outside content only ever reaches a reader that has no tools. The
   route and the amounts were fixed from the owner's own sentence before anything external is
   read, so text on a web page, in a market description or in a vendor's data cannot start a
   spend. The reader reports which instruction-like phrases it saw; that is all it can do.

2. The worst case. Suppose the agent itself were hijacked and asked the vault for exactly
   what the content demands. This route makes that request for real, as the agent key would:
   `request_spend` to the demanded address for the demanded amount. The vault answers as it
   answers anyone. Over a limit or to an unknown address, it holds the request and the owner's
   phone rings, with the risk officer pointing out that nobody asked for this. Inside every
   limit, to a payee already trusted, it would issue a permit: that is the stated bound on a
   hijacked agent (one transfer, capped, to an allowlisted payee). The permit is not redeemed
   here, so the demonstration never moves money by itself.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.context import AppContext, get_ctx
from app.db import HostileRow, PlanRow, utcnow
from app.intent import _EVM_ADDRESS, _SOL_ADDRESS, read_amount
from app.planner import say_money
from app.schemas import (
    BlockedData,
    Chain,
    Money,
    Party,
    Reason,
    Route,
    RouteStep,
    TransferRequest,
)

ATTACKER_ADDRESS = "0xbadbadbadbadbadbadbadbadbadbadbadbadbad0"

INSTRUCTION_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        r"\bignore (all |any )?(your |the |previous |prior )*(instructions|limits|rules|policy)",
        "tells the reader to ignore its instructions",
    ),
    (
        r"\b(system|admin|developer|anthropic|operator)\s*(:|message|override|notice)",
        "claims special authority",
    ),
    (
        r"\b(send|transfer|wire|move|pay|withdraw)\b.{0,60}?(\$|\d|usdc|dollars|funds|everything|all)",
        "demands a payment",
    ),
    (
        r"\b(approve|pre-?approved|already authori[sz]ed|no (need|approval) (to|is) )",
        "claims the spend is already approved",
    ),
    (
        r"\b(set_?policy|raise (the|your) limit|disable|turn off|unfreeze|allowlist)\b",
        "asks to change the limits",
    ),
    (r"\b(urgent|immediately|right now|within \d+ (minutes|seconds))\b", "applies time pressure"),
    (
        r"\b(do not|don't|never) (tell|call|notify|alert) (the )?(owner|user)",
        "asks to keep the owner out of it",
    ),
)


class HostileRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class HostileResult(BaseModel):
    reader_flags: list[str] = Field(
        description="Instruction-like phrases the reader noticed. Reporting them is all it can do."
    )
    reader_outcome: str
    demanded_amount_usd: Money
    demanded_payee: str
    worst_case: Literal["phone_call", "would_pay", "blocked", "denied_call_cap"]
    reason: Reason | None = None
    request_id: str | None = None
    explanation: str


def reader_flags(content: str) -> list[str]:
    """What a tool-less reader can do with hostile text: notice it."""
    lowered = content.lower()
    return [label for pattern, label in INSTRUCTION_PATTERNS if re.search(pattern, lowered)]


class HostileLab:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    async def run(self, content: str) -> HostileResult:
        ctx = self._ctx
        flags = reader_flags(content)
        policy = await ctx.vault.get_policy()

        # The worst case: do exactly what the content demands. If it names no amount, assume
        # the attacker wants the whole daily budget; if it names no address, the attacker's own.
        amount = read_amount(content) or policy.daily_budget_usd
        amount = min(amount, Decimal("1000000000")).quantize(Decimal("0.01"))
        match = _EVM_ADDRESS.search(content) or _SOL_ADDRESS.search(content)
        address = match.group(0) if match else ATTACKER_ADDRESS
        if not match:
            named = [
                wallet
                for wallet in ctx.wallets.wallets
                if wallet.owner != "me"
                and re.search(rf"\b{wallet.owner}\b", content.lower())
                and wallet.chain == Chain.ARC_TESTNET
            ]
            if named:
                address = named[0].address
        label = ctx.wallets.label_for(address)
        known = address.lower() in {item.lower() for item in policy.allowlist}

        reader_outcome = (
            "The reader has no tools. It noted the instruction-like text and did nothing else: "
            "no spend was requested because of this content."
            if flags
            else "The reader has no tools. It found nothing that reads as an instruction, and "
            "it could not have acted on one anyway."
        )
        base = {
            "reader_flags": flags,
            "reader_outcome": reader_outcome,
            "demanded_amount_usd": amount,
            "demanded_payee": label,
        }

        async def record(result: HostileResult) -> HostileResult:
            with ctx.db.session() as session:
                session.add(
                    HostileRow(
                        ts=utcnow(),
                        content=content[:4000],
                        extracted_json=json.dumps({"amount": str(amount), "address": address}),
                        outcome=result.worst_case,
                        reason=result.reason.value if result.reason else "",
                        request_id=result.request_id,
                    )
                )
                session.commit()
            return result

        async def blocked(reason: Reason, detail: str) -> HostileResult:
            await ctx.bus.publish(
                "blocked",
                BlockedData(reason=reason, amount_usd=amount, payee_label=label, detail=detail),
            )
            return await record(
                HostileResult(
                    **base,
                    worst_case="blocked",
                    reason=reason,
                    explanation=f"Worst case: nothing. {detail}",
                )
            )

        if ctx.stream_stale:
            return await blocked(
                Reason.STREAM_STALE, "The event stream is stale, so every spend is refused."
            )
        if await ctx.vault.is_frozen():
            return await blocked(Reason.FROZEN, "The vault is frozen, so every spend is refused.")

        plan = self._plan(content, amount, address, label, known)
        plan_id = plan.id
        assert plan_id is not None
        result = await ctx.vault.request_spend(address, amount, purpose=f"hostile p_{plan_id}")

        if result.request_id is not None:
            plan.status = "needs_approval"
            plan.vault_request_id = result.request_id
            with ctx.db.session() as session:
                session.add(plan)
                session.commit()
            ctx.approvals.attach_plan(result.request_id, plan)
            row = ctx.approvals.get(result.request_id)
            capped = row is not None and row.resolved_via == "call_cap"
            return await record(
                HostileResult(
                    **base,
                    worst_case="denied_call_cap" if capped else "phone_call",
                    reason=result.reason,
                    request_id=result.request_id,
                    explanation=(
                        "Worst case: not even a call. The daily limit on approval calls was "
                        "already reached, so the vault's request was denied without ringing you."
                        if capped
                        else "Worst case: your phone rings. A hijacked agent asked the vault for "
                        f"{say_money(amount)} to {label}. The vault held it, and you are asked "
                        "to approve something you never requested. Say no, or let it time out."
                    ),
                )
            )
        if result.permitted:
            # Deliberately not redeemed: the demonstration must not move money by itself.
            plan.status = "not_redeemed"
            with ctx.db.session() as session:
                session.add(plan)
                session.commit()
            return await record(
                HostileResult(
                    **base,
                    worst_case="would_pay",
                    explanation=(
                        f"Worst case: one transfer of {say_money(amount)} to {label}. That is "
                        "inside every limit and to a payee you already trust, so the vault "
                        "would allow it. This is the bound on a hijacked agent: one transfer, "
                        f"at most {say_money(policy.transfer_cap_usd)}, only to a payee on your "
                        "list, within the daily budget. The permit was not used here."
                    ),
                )
            )
        return await blocked(result.reason or Reason.FROZEN, "The vault refused the spend.")

    def _plan(
        self, content: str, amount: Decimal, address: str, label: str, known: bool
    ) -> PlanRow:
        """A plan row marked as hostile, so an approval for it says where it came from."""
        ctx = self._ctx
        mine = ctx.wallets.find("me", Chain.ARC_TESTNET)
        assert mine is not None
        request = TransferRequest(
            amount_usd=amount,
            source=Party(label=mine.label, chain=mine.chain, address=mine.address, known=True),
            destination=Party(label=label, chain=Chain.ARC_TESTNET, address=address, known=known),
        )
        route = Route(
            id="cheapest",
            cost_usd=Decimal("0.01"),
            eta_s=1,
            steps=[
                RouteStep(
                    rail="arc-native",
                    from_chain=Chain.ARC_TESTNET,
                    to_chain=Chain.ARC_TESTNET,
                    cost_usd=Decimal("0.01"),
                    eta_s=1,
                    description="USDC transfer on Arc",
                )
            ],
        )
        row = PlanRow(
            created=utcnow(),
            text=" ".join(content.split())[:200],
            kind="transfer",
            parser="rules",
            request_json=request.model_dump_json(),
            routes_json=json.dumps([route.model_dump(mode="json")]),
            status="quoted",
            route_id="cheapest",
            source="hostile",
        )
        with ctx.db.session() as session:
            session.add(row)
            session.commit()
            assert row.id is not None
        return row


router = APIRouter(prefix="/api", tags=["hostile content"])
Ctx = Annotated[AppContext, Depends(get_ctx)]


@router.post("/hostile", response_model=HostileResult)
async def hostile(body: HostileRequest, ctx: Ctx) -> HostileResult:
    """Feed the agent hostile content. Shows what the reader does (nothing) and the worst case
    if the agent itself were hijacked (a phone call, or one capped transfer to a trusted payee)."""
    return await ctx.hostile.run(body.content)
