"""Rules by voice: speak a rule, hear it read back, confirm, and the policy changes.

A rule is one change to the vault policy: the per-transfer limit, the daily budget, or the
payee allowlist. The sentence is parsed by fixed patterns, not by a model, so what is read back
is exactly what will be applied.

Confirming. A change that loosens the policy (a higher limit, a new payee) needs the one-time
word from the owner's dashboard, the same as approving a transfer. A change that tightens it (a
lower limit, removing a payee) needs only "confirm": making the policy stricter is always safe.

The change is applied through `VaultPort.set_policy`, which is an owner-key operation; the
agent key can never call it.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
from datetime import timedelta
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import col, select

from app.context import AppContext, get_ctx
from app.db import RuleProposalRow, as_utc, utcnow
from app.intent import _EVM_ADDRESS, _SOL_ADDRESS, read_amount
from app.planner import say_money
from app.schemas import Chain, Money, OkResponse, Policy
from app.voice import CHALLENGE_WORDS, normalise_word, require_voice_token

PROPOSAL_TTL_S = 180
MAX_ATTEMPTS = 3
MAX_LIMIT = Decimal("1000000")

_CHAINS: dict[str, Chain] = {
    "eth": Chain.ETH_SEPOLIA,
    "ethereum": Chain.ETH_SEPOLIA,
    "sol": Chain.SOLANA_DEVNET,
    "solana": Chain.SOLANA_DEVNET,
    "arc": Chain.ARC_TESTNET,
}


class RuleError(ValueError):
    """The rule could not be understood or is not allowed. The message is safe to speak."""


class RuleChange(BaseModel):
    kind: Literal["transfer_cap", "daily_budget", "allow_payee", "remove_payee"]
    amount_usd: Money | None = None
    address: str | None = None
    label: str = ""
    loosens: bool


class RuleProposeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str = Field(min_length=1, max_length=300)


class RuleConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    proposal_id: str
    challenge: str = Field(default="", max_length=64)


class RuleWord(BaseModel):
    challenge: str = Field(default="", max_length=64)


class RuleProposal(BaseModel):
    """What the agent may see: the read-back, and whether a word is needed. Never the word."""

    proposal_id: str
    readback: str
    needs_word: bool
    status: str = "pending"


class RuleView(RuleProposal):
    """What the owner's dashboard sees, including the word."""

    text: str
    challenge: str | None
    expires_at: str


class RuleList(BaseModel):
    rules: list[RuleView]


def parse_rule(text: str, policy: Policy, ctx: AppContext) -> tuple[RuleChange, str]:
    """One sentence -> one policy change and its read-back."""
    lowered = text.lower()
    amount = read_amount(text)

    if re.search(
        r"\b(per[- ]transfer|transfer|single|each)\b.*\b(limit|cap|maximum|max)\b", lowered
    ):
        if amount is None:
            raise RuleError("What should the transfer limit be? I did not catch an amount.")
        _check_amount(amount)
        if amount > policy.daily_budget_usd:
            raise RuleError(
                f"A transfer limit of {say_money(amount)} would be above your daily budget of "
                f"{say_money(policy.daily_budget_usd)}. Raise the budget first."
            )
        loosens = amount > policy.transfer_cap_usd
        return (
            RuleChange(kind="transfer_cap", amount_usd=amount, loosens=loosens),
            f"You want to {'raise' if loosens else 'lower'} your per-transfer limit from "
            f"{say_money(policy.transfer_cap_usd)} to {say_money(amount)}.",
        )

    if re.search(r"\b(daily|day|per[- ]day|24[- ]hour)\b.*\b(budget|limit|cap|spend)\b", lowered):
        if amount is None:
            raise RuleError("What should the daily budget be? I did not catch an amount.")
        _check_amount(amount)
        if amount < policy.transfer_cap_usd:
            raise RuleError(
                f"A daily budget of {say_money(amount)} would be below your transfer limit of "
                f"{say_money(policy.transfer_cap_usd)}. Lower the limit first."
            )
        loosens = amount > policy.daily_budget_usd
        return (
            RuleChange(kind="daily_budget", amount_usd=amount, loosens=loosens),
            f"You want to {'raise' if loosens else 'lower'} your daily budget from "
            f"{say_money(policy.daily_budget_usd)} to {say_money(amount)}.",
        )

    removing = bool(re.search(r"\b(remove|block|stop paying|never pay|untrust|forget)\b", lowered))
    adding = bool(re.search(r"\b(allow|add|trust|whitelist|approve payments to)\b", lowered))
    if removing or adding:
        address, label = _payee(text, ctx)
        listed = address.lower() in {item.lower() for item in policy.allowlist}
        if removing:
            if not listed:
                raise RuleError(
                    f"{label} is not on your list of payees, so there is nothing to remove."
                )
            return (
                RuleChange(kind="remove_payee", address=address, label=label, loosens=False),
                f"You want to remove {label} from the payees I can pay without calling you.",
            )
        if listed:
            raise RuleError(f"{label} is already on your list of payees.")
        return (
            RuleChange(kind="allow_payee", address=address, label=label, loosens=True),
            f"You want to add {label} to the payees I can pay without calling you.",
        )

    raise RuleError(
        "I can change your per-transfer limit, your daily budget, or the payees I may pay "
        "without calling you. I did not understand that as one of those."
    )


def _check_amount(amount: Decimal) -> None:
    if amount <= 0 or amount > MAX_LIMIT:
        raise RuleError("That amount is not one I can set as a limit.")


def _payee(text: str, ctx: AppContext) -> tuple[str, str]:
    match = _EVM_ADDRESS.search(text) or _SOL_ADDRESS.search(text)
    if match:
        address = match.group(0)
        return address, ctx.wallets.label_for(address)
    lowered = text.lower()
    owners = [owner for owner in ctx.wallets.owners() if re.search(rf"\b{owner}\b", lowered)]
    chain = next((c for word, c in _CHAINS.items() if re.search(rf"\b{word}\b", lowered)), None)
    if not owners:
        raise RuleError("Which payee? Give me a name I know or a wallet address.")
    if chain is None:
        raise RuleError(f"Which of {owners[0].capitalize()}'s wallets: Ethereum, Solana or Arc?")
    wallet = ctx.wallets.find(owners[0], chain)
    if wallet is None:
        raise RuleError(f"{owners[0].capitalize()} has no wallet on {chain.value}.")
    return wallet.address, wallet.label


def apply_change(policy: Policy, change: RuleChange) -> Policy:
    if change.kind == "transfer_cap":
        assert change.amount_usd is not None
        return policy.model_copy(update={"transfer_cap_usd": change.amount_usd})
    if change.kind == "daily_budget":
        assert change.amount_usd is not None
        return policy.model_copy(update={"daily_budget_usd": change.amount_usd})
    assert change.address is not None
    if change.kind == "allow_payee":
        return policy.model_copy(update={"allowlist": [*policy.allowlist, change.address]})
    return policy.model_copy(
        update={"allowlist": [a for a in policy.allowlist if a.lower() != change.address.lower()]}
    )


class Rules:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    async def propose(self, text: str) -> RuleProposal:
        policy = await self._ctx.vault.get_policy()
        change, readback = parse_rule(text, policy, self._ctx)
        now = utcnow()
        ask = (
            "To confirm, say confirm and the word on your Switchboard screen."
            if change.loosens
            else "To confirm, say confirm."
        )
        row = RuleProposalRow(
            created=now,
            expires_at=now + timedelta(seconds=PROPOSAL_TTL_S),
            text=text.strip(),
            readback=f"{readback} {ask}",
            change_json=change.model_dump_json(),
            challenge=secrets.choice(CHALLENGE_WORDS) if change.loosens else "",
        )
        with self._ctx.db.session() as session:
            # One open proposal at a time, so "confirm" can only mean the latest.
            for old in session.exec(
                select(RuleProposalRow).where(RuleProposalRow.status == "pending")
            ).all():
                old.status = "superseded"
                session.add(old)
            session.add(row)
            session.commit()
            assert row.id is not None
        return self._proposal(row)

    @staticmethod
    def _proposal(row: RuleProposalRow) -> RuleProposal:
        return RuleProposal(
            proposal_id=f"r_{row.id}",
            readback=row.readback,
            needs_word=bool(row.challenge),
            status=row.status,
        )

    def _row(self, proposal_id: str) -> RuleProposalRow | None:
        if not proposal_id.startswith("r_") or not proposal_id[2:].isdigit():
            return None
        with self._ctx.db.session() as session:
            return session.get(RuleProposalRow, int(proposal_id[2:]))

    def _save(self, row: RuleProposalRow) -> None:
        with self._ctx.db.session() as session:
            session.add(row)
            session.commit()

    async def confirm(self, proposal_id: str, challenge: str, via: str = "voice") -> OkResponse:
        row = self._row(proposal_id)
        if row is None:
            return OkResponse(ok=False, error="unknown_proposal")
        if row.status != "pending":
            return OkResponse(ok=False, status=row.status, error="already_resolved")
        if utcnow() > as_utc(row.expires_at):
            row.status = "expired"
            self._save(row)
            return OkResponse(ok=False, status="expired", error="expired")
        if row.challenge and not hmac.compare_digest(
            normalise_word(challenge), normalise_word(row.challenge)
        ):
            row.attempts += 1
            if row.attempts >= MAX_ATTEMPTS:
                row.status = "denied"
            self._save(row)
            return OkResponse(
                ok=False,
                status=row.status,
                error="too_many_attempts" if row.status == "denied" else "challenge_mismatch",
            )
        change = RuleChange.model_validate(json.loads(row.change_json))
        policy = await self._ctx.vault.get_policy()
        row.status = "applied"
        self._save(row)
        await self._ctx.vault.set_policy(apply_change(policy, change), via=via, rule_text=row.text)
        return OkResponse(ok=True, status="applied")

    def cancel(self, proposal_id: str) -> OkResponse:
        row = self._row(proposal_id)
        if row is None or row.status != "pending":
            return OkResponse(ok=False, error="unknown_proposal")
        row.status = "cancelled"
        self._save(row)
        return OkResponse(ok=True, status="cancelled")

    def views(self, limit: int = 5) -> list[RuleView]:
        now = utcnow()
        with self._ctx.db.session() as session:
            rows = session.exec(
                select(RuleProposalRow).order_by(col(RuleProposalRow.id).desc()).limit(limit)
            ).all()
        views = []
        for row in rows:
            status = (
                "expired"
                if row.status == "pending" and now > as_utc(row.expires_at)
                else row.status
            )
            views.append(
                RuleView(
                    proposal_id=f"r_{row.id}",
                    readback=row.readback,
                    needs_word=bool(row.challenge),
                    status=status,
                    text=row.text,
                    challenge=row.challenge if status == "pending" and row.challenge else None,
                    expires_at=as_utc(row.expires_at).isoformat(),
                )
            )
        return views


Ctx = Annotated[AppContext, Depends(get_ctx)]

voice_router = APIRouter(
    prefix="/api/voice/rules", tags=["voice tools"], dependencies=[Depends(require_voice_token)]
)


@voice_router.post("/propose", response_model=RuleProposal)
async def voice_propose(body: RuleProposeRequest, ctx: Ctx) -> RuleProposal:
    """Voice tool. Parse the spoken rule and return the read-back. Nothing changes yet."""
    try:
        return await ctx.rules.propose(body.text)
    except RuleError as exc:
        return RuleProposal(proposal_id="", readback=str(exc), needs_word=False, status="rejected")


@voice_router.post("/confirm", response_model=OkResponse)
async def voice_confirm(body: RuleConfirmRequest, ctx: Ctx) -> OkResponse:
    """Voice tool. Apply the proposed rule. Loosening needs the word from the owner's screen."""
    return await ctx.rules.confirm(body.proposal_id, body.challenge)


router = APIRouter(prefix="/api/rules", tags=["rules"])


@router.get("", response_model=RuleList)
async def list_rules(ctx: Ctx) -> RuleList:
    """Recent rule proposals for the dashboard, with the word for one that is pending."""
    return RuleList(rules=ctx.rules.views())


@router.post("/propose", response_model=RuleProposal)
async def propose_rule(body: RuleProposeRequest, ctx: Ctx) -> RuleProposal:
    """Propose a rule from the dashboard (typed, or spoken into the microphone)."""
    try:
        return await ctx.rules.propose(body.text)
    except RuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{proposal_id}/confirm", response_model=OkResponse)
async def confirm_rule(proposal_id: str, body: RuleWord, ctx: Ctx) -> OkResponse:
    """Dry-run only: stand in for the spoken confirmation. Refused when calls are live."""
    if ctx.settings.calls_live:
        raise HTTPException(status_code=403, detail="calls are live; confirm on the phone")
    return await ctx.rules.confirm(proposal_id, body.challenge, via="dashboard (dry run)")


@router.post("/{proposal_id}/cancel", response_model=OkResponse)
async def cancel_rule(proposal_id: str, ctx: Ctx) -> OkResponse:
    return ctx.rules.cancel(proposal_id)
