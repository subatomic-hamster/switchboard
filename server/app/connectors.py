"""Connectors: outside assistants (ChatGPT, Claude, Grok, Gemini, ...) that may ask
Switchboard to send, and can hand out the owner's receiving addresses.

Each assistant gets its own token and its own two limits. What a connected assistant can do:

- read what it may do, who it may pay, and the owner's receiving addresses;
- get a quote;
- ask for a transfer to someone already in the wallet book;
- look up transfers it asked for.

What it can never do, whatever it is told by a page, a file or a person in its chat: approve
anything, see the one-time word, pay an address that is not in the wallet book, change a
limit, add a payee, unfreeze the vault, or see another assistant's requests. There is no
route for any of those here.

A request goes through the same planner and the same vault as the owner's own sentence. The
assistant's limits sit in front of the vault's: a spend the vault would allow, but that is
more than the assistant may send by itself, is held and the owner is asked (see `app.holds`).
Those limits are kept by this server. The vault contract knows one agent key, so a limit per
assistant is not enforced onchain; the vault's own limits are.

Two ways to reach it, both under `/api/connect/` (public, each request authenticated):
the MCP endpoint in `app.mcp_http`, and the plain JSON routes below.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.openapi.utils import get_openapi
from pydantic import Field
from sqlmodel import col, select

from app.context import AppContext, get_ctx
from app.db import (
    ApprovalRow,
    ConnectorRequestRow,
    ConnectorRow,
    PlanRow,
    as_utc,
    from_micro,
    to_micro,
    utcnow,
)
from app.intent import IntentError
from app.planner import say_money, say_time
from app.schemas import (
    Chain,
    ConnectorAuth,
    ConnectorCreate,
    ConnectorCreated,
    ConnectorList,
    ConnectorUpdate,
    ConnectorView,
    Decision,
    ExecuteStatus,
    Model,
    Money,
    OkResponse,
    Outcome,
    Party,
    Reason,
    Route,
    TransferRequest,
    Wallet,
)

log = logging.getLogger("switchboard.connectors")

PREFIX = "swb_"
# `PlanRow.source` for a plan a connector asked for is this followed by the connector's name.
CONNECTOR_SOURCE = "connector:"
OWNER = "me"
# How many times in 24 hours one assistant may cause the owner to be asked. Past this its
# requests that need approval are refused without a call, so one assistant cannot use up the
# day's approval calls or wear the owner down.
MAX_ASKS_PER_DAY = 3
# The same amount to the same payee again inside this window is refused unless the assistant
# says it is deliberate. Assistants retry tool calls that time out.
DUPLICATE_WINDOW_S = 120
CENT = Decimal("0.01")
MCP_PATH = "/api/connect/mcp"
LINK_PATH = "/api/connect/link/{token}/mcp"
API_PATH = "/api/connect/v1"

ALLOWED_NAME = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -_.")


# --------------------------------------------------------------------------- wire models
# These are what an assistant sees. Every `message` is plain language it can pass on.


class PayeeInfo(Model):
    name: str = Field(description="Use this as `to`.")
    wallets: list[str] = Field(description="The chains this payee has a wallet on.")


class WalletInfo(Model):
    label: str
    chain: Chain


class ConnectStatus(Model):
    assistant: str
    may_send_without_asking_usd: Money = Field(
        description="The most this assistant may send in one transfer without the owner "
        "being asked. 0 means the owner is asked every time."
    )
    daily_without_asking_usd: Money
    sent_today_usd: Money
    approval_requests_left_today: int
    spending_paused: bool
    paused_reason: str
    approvals_by: Literal["phone call", "dashboard"]
    simulated: bool = Field(
        description="True when this Switchboard runs on stand-in chain modules: transfers are "
        "simulated and no funds move."
    )
    payees: list[PayeeInfo]
    your_wallets: list[WalletInfo]
    message: str


class ConnectTransferRequest(Model):
    amount_usd: Money = Field(gt=0, le=1_000_000, description="US dollars, whole cents.")
    to: str = Field(
        min_length=1,
        max_length=80,
        description="A payee name from the status call, for example 'sam'. Addresses are "
        "not accepted.",
    )
    to_chain: Chain | None = Field(
        default=None, description="Which of the payee's wallets. Needed only if ambiguous."
    )
    from_chain: Chain | None = Field(
        default=None, description="Which of the owner's wallets pays. Defaults to Arc."
    )
    receive_as: Literal["USDC", "EURC"] = "USDC"


class ConnectSendRequest(ConnectTransferRequest):
    speed: Literal["cheapest", "fastest"] = "cheapest"
    allow_duplicate: bool = Field(
        default=False,
        description="Set true only when the user really wants the same amount sent to the "
        "same payee again within two minutes.",
    )


class RouteInfo(Model):
    id: Literal["cheapest", "fastest"]
    cost_usd: Money
    eta_s: int


class ConnectQuote(Model):
    outcome: Literal["would_send", "would_ask_owner", "would_block", "rejected"]
    amount_usd: Money | None = None
    to: str = ""
    routes: list[RouteInfo] = Field(default_factory=list)
    message: str


class ConnectResult(Model):
    status: Literal["sent", "needs_approval", "blocked", "rejected"]
    transfer: str | None = Field(
        default=None, description="Handle for looking this transfer up later."
    )
    amount_usd: Money | None = None
    to: str = ""
    reason: str | None = None
    message: str


class ConnectTransfer(Model):
    transfer: str
    created: datetime
    amount_usd: Money
    to: str
    status: Literal["not_started", "waiting_for_owner", "in_flight", "arrived", "denied", "blocked"]
    message: str


class ConnectTransfers(Model):
    transfers: list[ConnectTransfer]
    message: str = ""


class ReceiveWallet(Model):
    label: str
    chain: Chain
    address: str
    asset: str = "USDC"


class ConnectReceive(Model):
    wallets: list[ReceiveWallet]
    message: str


PLAN_STATUS: dict[str, tuple[str, str]] = {
    "quoted": ("not_started", "It was quoted but never sent."),
    "needs_approval": (
        "waiting_for_owner",
        "Waiting for the owner to approve it. Only the owner can, and not in this chat.",
    ),
    "executing": ("in_flight", "Sent. It has not arrived yet."),
    "done": ("arrived", "It arrived."),
    "denied": (
        "denied",
        "The owner did not approve it: denied, or nobody answered. Nothing was sent. Do not "
        "send it again unless the user asks again.",
    ),
    "blocked": ("blocked", "Switchboard refused it. Nothing was sent."),
    "not_redeemed": ("not_started", "It was never sent."),
}


class ConnectorError(LookupError):
    """A connector cannot be made or changed as asked. The message is safe to show."""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def clean_name(name: str) -> str:
    """The name is spoken on the approval call, so it is kept to plain characters."""
    cleaned = " ".join("".join(ch for ch in name if ch in ALLOWED_NAME).split())
    if not cleaned:
        raise ConnectorError("Give the connector a name made of letters or digits.")
    return cleaned[:40]


class Connectors:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    # ------------------------------------------------------------------ the registry

    def create(self, body: ConnectorCreate) -> tuple[ConnectorRow, str]:
        """Make a connector. The token is returned here and exists nowhere else afterwards."""
        token = PREFIX + secrets.token_urlsafe(32)
        row = ConnectorRow(
            name=clean_name(body.name),
            kind=body.kind,
            auth=body.auth,
            token_hash=hash_token(token),
            token_hint=token[-4:],
            created=utcnow(),
            auto_send_micro=to_micro(body.auto_send_usd),
            daily_micro=to_micro(max(body.daily_usd, body.auto_send_usd)),
        )
        with self._ctx.db.session() as session:
            session.add(row)
            session.commit()
        log.info("connector %s (%s, %s) created", row.id, row.name, row.auth)
        return row, token

    def rows(self) -> list[ConnectorRow]:
        with self._ctx.db.session() as session:
            return list(session.exec(select(ConnectorRow).order_by(col(ConnectorRow.id))).all())

    def get(self, connector_id: int) -> ConnectorRow | None:
        with self._ctx.db.session() as session:
            return session.get(ConnectorRow, connector_id)

    def live(self, connector_id: int) -> ConnectorRow | None:
        row = self.get(connector_id)
        return row if row is not None and row.revoked_at is None else None

    def _save(self, row: ConnectorRow) -> None:
        with self._ctx.db.session() as session:
            session.add(row)
            session.commit()

    def update(self, connector_id: int, body: ConnectorUpdate) -> ConnectorRow:
        row = self.live(connector_id)
        if row is None:
            raise ConnectorError("unknown connector")
        row.auto_send_micro = to_micro(body.auto_send_usd)
        row.daily_micro = to_micro(max(body.daily_usd, body.auto_send_usd))
        self._save(row)
        return row

    def revoke(self, connector_id: int) -> ConnectorRow:
        row = self.get(connector_id)
        if row is None:
            raise ConnectorError("unknown connector")
        if row.revoked_at is None:
            row.revoked_at = utcnow()
            self._save(row)
            log.info("connector %s (%s) revoked", row.id, row.name)
        return row

    def authenticate(self, token: str, via: ConnectorAuth) -> ConnectorRow | None:
        """The connector this token belongs to, if it is live and was presented the way it
        was issued. A header token is not accepted in an address, nor the other way round."""
        if not token.startswith(PREFIX) or len(token) > 100:
            return None
        with self._ctx.db.session() as session:
            row = session.exec(
                select(ConnectorRow).where(ConnectorRow.token_hash == hash_token(token))
            ).first()
            if row is None or row.revoked_at is not None or row.auth != via:
                return None
            row.last_used = utcnow()
            session.add(row)
            session.commit()
            return row

    # ------------------------------------------------------------------ accounting

    def _requests(
        self, connector_id: int, hours: int = 24
    ) -> list[tuple[ConnectorRequestRow, str]]:
        """This connector's requests in the window, each with its plan's status."""
        cutoff = utcnow() - timedelta(hours=hours)
        with self._ctx.db.session() as session:
            rows = session.exec(
                select(ConnectorRequestRow, PlanRow.status)
                .join(PlanRow, col(PlanRow.id) == col(ConnectorRequestRow.plan_id))
                .where(
                    ConnectorRequestRow.connector_id == connector_id,
                    col(ConnectorRequestRow.created) >= cutoff,
                )
                .order_by(col(ConnectorRequestRow.plan_id).desc())
            ).all()
        return [(row, str(status)) for row, status in rows]

    def sent_today(self, connector_id: int) -> Decimal:
        return sum(
            (
                from_micro(row.amount_micro)
                for row, status in self._requests(connector_id)
                if status in ("executing", "done")
            ),
            Decimal(0),
        )

    def asks_today(self, connector_id: int) -> int:
        return sum(1 for row, _ in self._requests(connector_id) if row.asked)

    def _waiting(self, connector_id: int) -> bool:
        return any(status == "needs_approval" for _, status in self._requests(connector_id))

    def view(self, row: ConnectorRow) -> ConnectorView:
        assert row.id is not None
        return ConnectorView(
            id=row.id,
            name=row.name,
            kind=row.kind,
            auth=row.auth,
            token_hint=row.token_hint,
            created=as_utc(row.created),
            revoked=row.revoked_at is not None,
            auto_send_usd=from_micro(row.auto_send_micro),
            daily_usd=from_micro(row.daily_micro),
            sent_today_usd=self.sent_today(row.id),
            asks_today=self.asks_today(row.id),
            last_used=as_utc(row.last_used) if row.last_used else None,
        )

    # ------------------------------------------------------------------ who and where

    def _payee_wallets(self, name: str) -> list[Wallet]:
        """Wallets a spoken or typed payee name refers to: an owner name ("sam", "Sam's"),
        or one wallet's exact label ("Sam's Arc wallet")."""
        key = " ".join(name.lower().split())
        book = self._ctx.wallets.wallets
        by_label = [wallet for wallet in book if wallet.label.lower() == key]
        if by_label:
            return by_label
        owner = key.removesuffix("'s").removesuffix("’s")
        if owner in ("myself", "my", "mine", "owner"):
            owner = OWNER
        return [wallet for wallet in book if wallet.owner == owner]

    def _resolve(self, body: ConnectTransferRequest) -> TransferRequest | str:
        """The transfer this request names, or a sentence saying what is wrong with it."""
        book = self._ctx.wallets
        amount = body.amount_usd
        if amount != amount.quantize(CENT):
            return "Use whole cents: an amount like 12.50, not fractions of a cent."
        from_chain = body.from_chain or Chain.ARC_TESTNET
        source = book.find(OWNER, from_chain)
        if source is None:
            return f"The owner has no wallet on {from_chain.value}."
        wallets = self._payee_wallets(body.to)
        if not wallets:
            names = ", ".join(sorted(book.owners() - {OWNER})) or "nobody yet"
            return (
                f"'{body.to[:40]}' is not someone the owner pays. A connected assistant can "
                f"only pay people already in the owner's wallet book: {names}. The owner adds "
                "payees in Switchboard, not here. Addresses are not accepted."
            )
        if body.to_chain is not None:
            wallets = [wallet for wallet in wallets if wallet.chain == body.to_chain]
            if not wallets:
                return f"That payee has no wallet on {body.to_chain.value}."
        if len(wallets) > 1:
            same_chain = [wallet for wallet in wallets if wallet.chain == from_chain]
            if len(same_chain) != 1:
                chains = ", ".join(wallet.chain.value for wallet in wallets)
                return f"That payee has several wallets. Say which with to_chain: {chains}."
            wallets = same_chain
        dest = wallets[0]
        if dest.address.lower() == source.address.lower():
            return "That is the wallet the money would come from."
        return TransferRequest(
            amount_usd=amount,
            to_asset=body.receive_as,
            source=Party(
                label=source.label, chain=source.chain, address=source.address, known=True
            ),
            destination=Party(
                label=dest.label, chain=dest.chain, address=dest.address, known=dest.allowlisted
            ),
        )

    # ------------------------------------------------------------------ the gate

    def _gate(self, row: ConnectorRow, amount: Decimal, preview: Decision) -> tuple[bool, bool]:
        """(the owner must be asked, and it is this assistant's own limit that requires it)."""
        if preview.outcome == Outcome.BLOCK:
            return False, False
        if preview.outcome == Outcome.ASK:
            return True, False
        assert row.id is not None
        over = amount > from_micro(row.auto_send_micro) or self.sent_today(
            row.id
        ) + amount > from_micro(row.daily_micro)
        return over, over

    def _ask_refusal(self, row: ConnectorRow) -> str | None:
        """Why this assistant may not have the owner asked right now, if it may not."""
        assert row.id is not None
        if self._waiting(row.id):
            return (
                f"{row.name} already has a request waiting for the owner. One at a time: wait "
                "for that one to be decided."
            )
        if self.asks_today(row.id) >= MAX_ASKS_PER_DAY:
            return (
                f"{row.name} has already had the owner asked {MAX_ASKS_PER_DAY} times in the "
                "last 24 hours, which is the limit. The owner can send this from Switchboard "
                "directly."
            )
        return None

    def _why_asked(self, row: ConnectorRow, amount: Decimal, preview: Decision) -> str:
        if preview.outcome == Outcome.ASK:
            return preview.detail
        if row.auto_send_micro == 0:
            return f"The owner has set {row.name} to ask every time."
        auto = from_micro(row.auto_send_micro)
        if amount > auto:
            return f"That is more than the {say_money(auto)} {row.name} may send without asking."
        return (
            f"That would pass the {say_money(from_micro(row.daily_micro))} a day {row.name} "
            "may send without asking."
        )

    def cases(self, plan: PlanRow, approval: ApprovalRow) -> tuple[str, str | None]:
        """For the approval call: where the request came from (the case for it, such as it
        is) and, when it is the assistant's own limit that held it, the objection.

        Both are built from the connector's name, which the owner chose, and from numbers.
        Nothing the assistant wrote is in either."""
        name = plan.source.removeprefix(CONNECTOR_SOURCE)
        case_for = (
            f"This did not come from a sentence you spoke to Switchboard. Your connected "
            f"assistant {name} asked for it."
        )
        if approval.reason != Reason.OVER_AGENT_LIMIT.value or plan.id is None:
            return case_for, None
        with self._ctx.db.session() as session:
            request = session.get(ConnectorRequestRow, plan.id)
            row = session.get(ConnectorRow, request.connector_id) if request else None
        if row is None or row.id is None:
            return case_for, None
        amount = from_micro(approval.amount_micro)
        auto, daily = from_micro(row.auto_send_micro), from_micro(row.daily_micro)
        if auto == 0:
            objection = (
                f"You set {name} to ask you every time, so nothing it sends goes without "
                "your say. If you did not just ask it for this, say deny."
            )
        elif amount > auto:
            objection = (
                f"It is {say_money(amount - auto)} over the {say_money(auto)} that {name} may "
                "send without asking you. If you did not just ask it for this, say deny."
            )
        else:
            objection = (
                f"{name} has already sent {say_money(self.sent_today(row.id))} today. This "
                f"would pass the {say_money(daily)} a day it may send without asking you."
            )
        return case_for, objection

    # ------------------------------------------------------------------ what an assistant can do

    async def status(self, row: ConnectorRow) -> ConnectStatus:
        ctx = self._ctx
        assert row.id is not None
        frozen = await ctx.vault.is_frozen()
        paused = frozen or ctx.stream_stale
        reason = "The owner has frozen the vault." if frozen else ""
        if not frozen and ctx.stream_stale:
            reason = "Switchboard's event stream has gone quiet, so spending is paused."
        by_owner: dict[str, list[str]] = {}
        for wallet in ctx.wallets.wallets:
            if wallet.owner != OWNER:
                by_owner.setdefault(wallet.owner, []).append(wallet.chain.value)
        auto = from_micro(row.auto_send_micro)
        limits = (
            "The owner is asked about every transfer."
            if auto == 0
            else f"Up to {say_money(auto)} at a time goes without asking, to a total of "
            f"{say_money(from_micro(row.daily_micro))} in 24 hours."
        )
        return ConnectStatus(
            assistant=row.name,
            may_send_without_asking_usd=auto,
            daily_without_asking_usd=from_micro(row.daily_micro),
            sent_today_usd=self.sent_today(row.id),
            approval_requests_left_today=max(MAX_ASKS_PER_DAY - self.asks_today(row.id), 0),
            spending_paused=paused,
            paused_reason=reason,
            approvals_by="phone call" if ctx.settings.calls_live else "dashboard",
            simulated=ctx.settings.use_p1_stubs,
            payees=[
                PayeeInfo(name=owner, wallets=chains) for owner, chains in sorted(by_owner.items())
            ],
            your_wallets=[
                WalletInfo(label=wallet.label, chain=wallet.chain)
                for wallet in ctx.wallets.wallets
                if wallet.owner == OWNER
            ],
            message=(
                f"{limits} Anything over that, or over the vault's own limits, needs the "
                "owner's approval, which only the owner can give and never in this chat. "
                "Only the payees listed can be paid."
                + (
                    " Transfers here are simulated; no funds move."
                    if ctx.settings.use_p1_stubs
                    else ""
                )
            ),
        )

    async def quote(self, row: ConnectorRow, body: ConnectTransferRequest) -> ConnectQuote:
        resolved = self._resolve(body)
        if isinstance(resolved, str):
            return ConnectQuote(outcome="rejected", message=resolved)
        planner = self._ctx.planner
        routes = planner.routes(resolved)
        if not routes:
            return ConnectQuote(
                outcome="rejected", message="There is no route between those two wallets."
            )
        preview = await planner.preview(resolved)
        ask, _ = self._gate(row, resolved.amount_usd, preview)
        cheapest = routes[0]
        price = (
            f"The cheapest route costs {say_money(cheapest.cost_usd)} and takes "
            f"{say_time(cheapest.eta_s)}."
        )
        outcome: Literal["would_send", "would_ask_owner", "would_block"]
        if preview.outcome == Outcome.BLOCK:
            outcome, message = "would_block", f"{preview.detail} It cannot be sent right now."
        elif ask:
            refusal = self._ask_refusal(row)
            if refusal:
                outcome, message = "would_block", refusal
            else:
                outcome = "would_ask_owner"
                message = (
                    f"{price} {self._why_asked(row, resolved.amount_usd, preview)} If you "
                    "send it, the owner is asked to approve it first."
                )
        else:
            outcome, message = (
                "would_send",
                f"{price} It is inside every limit and would go straight away.",
            )
        return ConnectQuote(
            outcome=outcome,
            amount_usd=resolved.amount_usd,
            to=resolved.destination.label,
            routes=route_summary(routes),
            message=message,
        )

    def _duplicate(self, row: ConnectorRow, request: TransferRequest) -> str | None:
        assert row.id is not None
        cutoff = utcnow() - timedelta(seconds=DUPLICATE_WINDOW_S)
        for earlier, status in self._requests(row.id, hours=1):
            if (
                as_utc(earlier.created) >= cutoff
                and earlier.amount_micro == to_micro(request.amount_usd)
                and earlier.payee_address == request.destination.address
                and status in ("executing", "done", "needs_approval")
            ):
                return f"p_{earlier.plan_id}"
        return None

    async def send(self, row: ConnectorRow, body: ConnectSendRequest) -> ConnectResult:
        ctx = self._ctx
        assert row.id is not None
        resolved = self._resolve(body)
        if isinstance(resolved, str):
            return ConnectResult(status="rejected", message=resolved)
        request = resolved
        amount, label = request.amount_usd, request.destination.label
        assert request.destination.address is not None

        def result(
            status: Literal["sent", "needs_approval", "blocked", "rejected"], **kw: Any
        ) -> ConnectResult:
            return ConnectResult(status=status, amount_usd=amount, to=label, **kw)

        if not body.allow_duplicate:
            earlier = self._duplicate(row, request)
            if earlier:
                return result(
                    "rejected",
                    transfer=earlier,
                    message=(
                        f"{row.name} asked for the same amount to the same payee less than "
                        f"two minutes ago ({earlier}), so this was not sent again. If the "
                        "user really wants a second one, repeat with allow_duplicate true."
                    ),
                )
        try:
            plan = await ctx.planner.plan_transfer(
                request,
                text=f"{row.name} asked to send {say_money(amount)} to {label}",
                source=f"{CONNECTOR_SOURCE}{row.name}",
            )
        except IntentError as exc:
            return result("rejected", message=str(exc))
        plan_row_id = int(plan.plan_id[2:])
        preview = plan.preview
        assert preview is not None
        ask, own_limit = self._gate(row, amount, preview)
        record = ConnectorRequestRow(
            plan_id=plan_row_id,
            connector_id=row.id,
            created=utcnow(),
            amount_micro=to_micro(amount),
            payee_address=request.destination.address,
        )
        with ctx.db.session() as session:
            session.add(record)
            session.commit()

        if ask:
            refusal = self._ask_refusal(row)
            if refusal:
                await ctx.planner.refuse(plan.plan_id, Reason.CALL_CAP_REACHED, refusal)
                return result(
                    "blocked",
                    transfer=plan.plan_id,
                    reason=Reason.CALL_CAP_REACHED.value,
                    message=f"Not sent. {refusal}",
                )

        outcome = await ctx.planner.execute(
            plan.plan_id, body.speed, hold=Reason.OVER_AGENT_LIMIT if own_limit else None
        )
        simulated = (
            " (Simulated: no funds move on this Switchboard.)" if ctx.settings.use_p1_stubs else ""
        )
        if outcome.status == ExecuteStatus.EXECUTING:
            route = next((r for r in plan.routes if r.id == body.speed), plan.routes[0])
            return result(
                "sent",
                transfer=plan.plan_id,
                message=(
                    f"Sent. {say_money(amount)} is on its way to {label}. The {route.id} route "
                    f"costs {say_money(route.cost_usd)} and takes {say_time(route.eta_s)}."
                    f"{simulated}"
                ),
            )
        if outcome.status == ExecuteStatus.NEEDS_APPROVAL:
            record.asked = True
            with ctx.db.session() as session:
                session.add(record)
                session.commit()
            how = (
                "Switchboard is calling the owner's phone now. Only the owner can approve it, "
                "by voice on that call."
                if ctx.settings.calls_live
                else "Phone calls are switched off on this Switchboard, so the owner approves "
                "or denies it on the Switchboard dashboard."
            )
            return result(
                "needs_approval",
                transfer=plan.plan_id,
                reason=outcome.reason.value if outcome.reason else None,
                message=(
                    f"Not sent yet. {self._why_asked(row, amount, preview)} {how} You cannot "
                    "approve it, and there is nothing to collect from the user in this chat: "
                    "do not ask them for a code or a word. If there is no decision in time it "
                    f"is denied. Look it up later with the transfer id {plan.plan_id}."
                ),
            )
        return result(
            "blocked",
            transfer=plan.plan_id,
            reason=outcome.reason.value if outcome.reason else None,
            message=f"Not sent. {outcome.detail}",
        )

    def transfers(self, row: ConnectorRow, transfer: str | None = None) -> ConnectTransfers:
        """Transfers this assistant asked for. It cannot see anyone else's."""
        assert row.id is not None
        found: list[ConnectTransfer] = []
        for request, status in self._requests(row.id, hours=24 * 7):
            plan_id = f"p_{request.plan_id}"
            if transfer is not None and plan_id != transfer.strip():
                continue
            state, message = PLAN_STATUS.get(status, ("not_started", "Its state is not known."))
            found.append(
                ConnectTransfer(
                    transfer=plan_id,
                    created=as_utc(request.created),
                    amount_usd=from_micro(request.amount_micro),
                    to=self._ctx.wallets.label_for(request.payee_address),
                    status=state,
                    message=message,
                )
            )
        if transfer is not None and not found:
            return ConnectTransfers(
                transfers=[], message=f"{row.name} has no transfer with that id in the last week."
            )
        return ConnectTransfers(transfers=found[:10])

    def receive(self) -> ConnectReceive:
        ctx = self._ctx
        return ConnectReceive(
            wallets=[
                ReceiveWallet(label=wallet.label, chain=wallet.chain, address=wallet.address)
                for wallet in ctx.wallets.wallets
                if wallet.owner == OWNER
            ],
            message=(
                "Give the sender the address for the chain they are sending on. These are "
                "test-network wallets and they take USDC. Switchboard does not watch for "
                "incoming payments yet, so it cannot confirm that one arrived."
            ),
        )


# --------------------------------------------------------------------------- routes for assistants

Ctx = Annotated[AppContext, Depends(get_ctx)]


def bearer(authorization: str | None) -> str:
    scheme, _, value = (authorization or "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def require_connector(
    ctx: Ctx, authorization: Annotated[str | None, Header()] = None
) -> ConnectorRow:
    row = ctx.connectors.authenticate(bearer(authorization), "header")
    if row is None:
        raise HTTPException(
            status_code=401,
            detail="invalid or revoked connector token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return row


Caller = Annotated[ConnectorRow, Depends(require_connector)]

router = APIRouter(prefix=API_PATH, tags=["connectors (for assistants)"])


@router.get("/status", response_model=ConnectStatus, operation_id="switchboard_status")
async def connect_status(ctx: Ctx, caller: Caller) -> ConnectStatus:
    """What this assistant may do: its limits, who it can pay, and whether spending is paused.
    Call this first."""
    return await ctx.connectors.status(caller)


@router.post("/quote", response_model=ConnectQuote, operation_id="switchboard_quote")
async def connect_quote(body: ConnectTransferRequest, ctx: Ctx, caller: Caller) -> ConnectQuote:
    """Price a transfer and say what would happen. Nothing is sent."""
    return await ctx.connectors.quote(caller, body)


@router.post("/send", response_model=ConnectResult, operation_id="switchboard_send")
async def connect_send(body: ConnectSendRequest, ctx: Ctx, caller: Caller) -> ConnectResult:
    """Ask Switchboard to send USDC to a payee. Sent at once if inside every limit; otherwise
    the owner is asked, or it is blocked. Read `status` in the answer."""
    return await ctx.connectors.send(caller, body)


@router.get("/transfers", response_model=ConnectTransfers, operation_id="switchboard_transfers")
async def connect_transfers(
    ctx: Ctx, caller: Caller, transfer: str | None = None
) -> ConnectTransfers:
    """Transfers this assistant asked for in the last week, or one of them by id."""
    return ctx.connectors.transfers(caller, transfer)


@router.get("/receive", response_model=ConnectReceive, operation_id="switchboard_receive")
async def connect_receive(ctx: Ctx, caller: Caller) -> ConnectReceive:
    """The owner's receiving addresses, to give to someone who is paying them."""
    return ctx.connectors.receive()


@router.get("/openapi.json", include_in_schema=False)
async def connect_openapi(request: Request, ctx: Ctx) -> dict[str, Any]:
    """An OpenAPI document of just these routes, for tools that import one (GPT Actions).
    It describes the routes and holds nothing private."""
    base = ctx.settings.public_base_url.rstrip("/") or str(request.base_url).rstrip("/")
    schema = get_openapi(
        title="Switchboard connector",
        version="1",
        description=(
            "Send USDC for the owner within limits the owner set, and look up the owner's "
            "receiving addresses. Authenticate with the connector token as a bearer token."
        ),
        routes=router.routes,
        servers=[{"url": base}],
    )
    schema.setdefault("components", {})["securitySchemes"] = {
        "connectorToken": {"type": "http", "scheme": "bearer"}
    }
    schema["security"] = [{"connectorToken": []}]
    return schema


# --------------------------------------------------------------------------- routes for the owner
# Under /api/connectors, which is not a public prefix: these are served on this machine only.

dashboard = APIRouter(prefix="/api/connectors", tags=["connectors (owner)"])


def addresses(ctx: AppContext, request: Request) -> tuple[str, str, bool]:
    public = ctx.settings.public_base_url.rstrip("/")
    base = public or str(request.base_url).rstrip("/")
    return f"{base}{MCP_PATH}", f"{base}{API_PATH}", bool(public)


@dashboard.get("", response_model=ConnectorList)
async def connector_list(ctx: Ctx, request: Request) -> ConnectorList:
    mcp_url, api_url, public = addresses(ctx, request)
    return ConnectorList(
        connectors=[ctx.connectors.view(row) for row in ctx.connectors.rows()],
        mcp_url=mcp_url,
        api_url=api_url,
        public=public,
        simulated=ctx.settings.use_p1_stubs,
        max_asks_per_day=MAX_ASKS_PER_DAY,
    )


@dashboard.post("", response_model=ConnectorCreated)
async def connector_create(body: ConnectorCreate, ctx: Ctx, request: Request) -> ConnectorCreated:
    """Make a connector and return its token. This is the only time the token is shown."""
    try:
        row, token = ctx.connectors.create(body)
    except ConnectorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    mcp_url, api_url, _ = addresses(ctx, request)
    if row.auth == "link":
        base = mcp_url.removesuffix(MCP_PATH)
        mcp_url = base + LINK_PATH.format(token=token)
    return ConnectorCreated(
        connector=ctx.connectors.view(row), token=token, mcp_url=mcp_url, api_url=api_url
    )


@dashboard.patch("/{connector_id}", response_model=ConnectorView)
async def connector_update(connector_id: int, body: ConnectorUpdate, ctx: Ctx) -> ConnectorView:
    try:
        return ctx.connectors.view(ctx.connectors.update(connector_id, body))
    except ConnectorError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@dashboard.delete("/{connector_id}", response_model=OkResponse)
async def connector_revoke(connector_id: int, ctx: Ctx) -> OkResponse:
    """Revoke a connector. Its token stops working at once; its history is kept."""
    try:
        ctx.connectors.revoke(connector_id)
    except ConnectorError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return OkResponse(ok=True, status="revoked")


def route_summary(routes: list[Route]) -> list[RouteInfo]:
    return [RouteInfo(id=route.id, cost_usd=route.cost_usd, eta_s=route.eta_s) for route in routes]
