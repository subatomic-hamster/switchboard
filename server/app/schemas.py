"""Pydantic models for every request, response and event: the frozen interface.

The first half mirrors `docs/implementation-plan.md` ("Frozen interface") field for field.
Models below the "P2 additions" line are new routes and events owned by P2; they add to the
interface and change nothing above them.

Money is `Decimal` in Python. On the wire it is a JSON number, as in the plan's examples, and a
string is accepted on input too.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, WithJsonSchema


def _money_to_json(value: Decimal) -> float | int:
    return int(value) if value == value.to_integral_value() else float(value)


Money = Annotated[
    Decimal,
    PlainSerializer(_money_to_json, return_type=float, when_used="json"),
    WithJsonSchema({"type": "number"}),
]
"""A dollar amount or price. `Decimal` in Python, JSON number on the wire."""


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- enums


class Chain(StrEnum):
    ETH_SEPOLIA = "eth-sepolia"
    SOLANA_DEVNET = "solana-devnet"
    ARC_TESTNET = "arc-testnet"


class Reason(StrEnum):
    """Why a spend needs approval or is blocked.

    The first four are the contract's reason codes (1 to 4). The rest are server-side checks
    that happen before or around the contract call.
    """

    OVER_TRANSFER_CAP = "OVER_TRANSFER_CAP"
    OVER_DAILY_BUDGET = "OVER_DAILY_BUDGET"
    PAYEE_NOT_ALLOWED = "PAYEE_NOT_ALLOWED"
    FROZEN = "FROZEN"
    STREAM_STALE = "STREAM_STALE"
    CALL_CAP_REACHED = "CALL_CAP_REACHED"
    TRADING_DISABLED = "TRADING_DISABLED"
    OVER_ORDER_CAP = "OVER_ORDER_CAP"
    NOT_EQUIVALENT = "NOT_EQUIVALENT"
    UNSUPPORTED = "UNSUPPORTED"


CONTRACT_REASONS: dict[int, Reason] = {
    1: Reason.OVER_TRANSFER_CAP,
    2: Reason.OVER_DAILY_BUDGET,
    3: Reason.PAYEE_NOT_ALLOWED,
    4: Reason.FROZEN,
}


class Outcome(StrEnum):
    PAY = "pay"
    ASK = "ask"
    BLOCK = "block"


# --------------------------------------------------------------------------- intent and plans


class IntentRequest(Model):
    text: str = Field(min_length=1, max_length=500)


class Party(Model):
    """One end of a transfer."""

    label: str
    chain: Chain
    address: str | None = None
    known: bool = Field(description="True when the address is on the vault's payee allowlist.")


class TransferRequest(Model):
    amount_usd: Money = Field(gt=0)
    asset: Literal["USDC"] = "USDC"
    to_asset: Literal["USDC", "EURC"] = "USDC"
    source: Party
    destination: Party


class MarketMoveIntent(Model):
    """A request to move a prediction-market position between venues."""

    from_venue: str
    to_venue: str
    market_hint: str = Field(description="The user's words for the market, for example 'Fed'.")
    slug: str | None = None
    qty: Money | None = None


class RouteStep(Model):
    rail: str
    from_chain: Chain
    to_chain: Chain
    cost_usd: Money
    eta_s: int
    description: str


class Route(Model):
    id: Literal["cheapest", "fastest"]
    cost_usd: Money
    eta_s: int
    steps: list[RouteStep]


class Decision(Model):
    """The planner's prediction of what the vault will do. The contract has the final word."""

    outcome: Outcome
    reason: Reason | None = None
    detail: str


class IntentResponse(Model):
    plan_id: str
    kind: Literal["transfer", "market_move"] = "transfer"
    request: TransferRequest | None = None
    market_move: MarketMoveIntent | None = None
    routes: list[Route] = []
    # P2 additions (optional, so the frozen shape still validates):
    spoken_quote: str = ""
    preview: Decision | None = None
    parser: Literal["llm", "rules"] = "rules"


class ExecuteRequest(Model):
    route_id: Literal["cheapest", "fastest"] = "cheapest"


class ExecuteStatus(StrEnum):
    EXECUTING = "executing"
    NEEDS_APPROVAL = "needs_approval"
    BLOCKED = "blocked"


class ExecuteResponse(Model):
    status: ExecuteStatus
    reason: Reason | None = None
    request_id: str | None = None
    # P2 additions:
    transfer_id: str | None = None
    detail: str = ""


# --------------------------------------------------------------------------- voice tools


class VoiceRequestInfo(Model):
    amount_usd: Money
    payee_label: str
    reason: Reason
    spent_today_usd: Money
    case_for: str
    case_against: str
    # P2 additions:
    status: str = "pending"
    requires_passkey: bool = False


class VoiceApproveRequest(Model):
    request_id: str
    amount_spoken: Money
    challenge: str = Field(max_length=64)


class VoiceDenyRequest(Model):
    request_id: str


class VoiceFreezeRequest(BaseModel):
    """No fields are needed. Extra fields (a tool may send a constant) are ignored."""

    model_config = ConfigDict(extra="ignore")


class OkResponse(Model):
    ok: bool
    # P2 additions:
    status: str = ""
    error: str = ""


# --------------------------------------------------------------------------- P1-owned bodies


class ChainTransferRequest(Model):
    from_chain: Chain
    to_chain: Chain
    to: str
    amount_usd: Money = Field(gt=0)


class ChainTransferResponse(Model):
    transfer_id: str


CheckResult = Literal["same", "different", "unknown", "n/a"]


class MatchChecks(Model):
    event: CheckResult
    strike: CheckResult
    direction: CheckResult
    time: CheckResult
    source: CheckResult


class MarketMatchResponse(Model):
    verdict: Literal["match", "refuse"]
    checks: MatchChecks
    gemini_symbol: str | None = None
    notes: list[str] = []


class SellQuote(Model):
    bid: Money
    post_at: Money
    taker_fee: Money


class BuyQuote(Model):
    ask: Money
    taker_fee: Money


class MarketQuoteResponse(Model):
    sell: SellQuote
    buy: BuyQuote
    move_cost_pct: Money
    bid_depth: Money


class MarketMoveRequest(Model):
    slug: str
    qty: Money = Field(gt=0)
    mode: Literal["post", "take"] = "post"


class MarketMoveResponse(Model):
    move_id: str


# --------------------------------------------------------------------------- events
#
# The plan freezes the nine event names. Payload shapes were not specified; these are P2's
# proposal, recorded in OVERNIGHT_LOG.md for P1 to confirm.


class TransferSentData(Model):
    transfer_id: str
    plan_id: str | None = None
    from_chain: Chain
    to_chain: Chain
    amount_usd: Money
    payee_label: str = ""
    route_id: str = ""
    eta_s: int = 0


class TransferArrivedData(Model):
    transfer_id: str
    to_chain: Chain
    amount_usd: Money
    tx_hash: str = ""


class ApprovalRequestedData(Model):
    request_id: str
    agent: str = ""
    payee: str
    amount_usd: Money
    reason: Reason


class ApprovalResolvedData(Model):
    request_id: str
    approved: bool
    via: str = Field(default="", description="voice, passkey, expiry, call_cap, dashboard")


class BlockedData(Model):
    reason: Reason
    plan_id: str | None = None
    amount_usd: Money | None = None
    payee_label: str = ""
    detail: str = ""


class TradeData(Model):
    move_id: str
    venue: str
    slug: str
    side: Literal["sell", "buy"]
    qty: Money
    price: Money
    dry_run: bool = True


class StreamStaleData(Model):
    stale: bool
    seconds_since_delivery: int = 0


class FrozenData(Model):
    frozen: bool
    via: str = ""


# P2 additions


class CallData(Model):
    request_id: str
    dry_run: bool
    status: str = Field(description="started, answered, no_answer, failed, capped, ended")
    detail: str = ""


class PolicyData(Model):
    transfer_cap_usd: Money
    daily_budget_usd: Money
    allowlist_size: int
    via: str = ""
    rule_text: str = ""


class ForecastData(Model):
    receipt_id: str
    slug: str
    commitment: str
    probability: Money | None = None
    outcome: bool | None = None
    brier: Money | None = None


class _EventBase(Model):
    id: int
    ts: datetime


class TransferSentEvent(_EventBase):
    type: Literal["transfer.sent"] = "transfer.sent"
    data: TransferSentData


class TransferArrivedEvent(_EventBase):
    type: Literal["transfer.arrived"] = "transfer.arrived"
    data: TransferArrivedData


class ApprovalRequestedEvent(_EventBase):
    type: Literal["approval.requested"] = "approval.requested"
    data: ApprovalRequestedData


class ApprovalResolvedEvent(_EventBase):
    type: Literal["approval.resolved"] = "approval.resolved"
    data: ApprovalResolvedData


class BlockedEvent(_EventBase):
    type: Literal["blocked"] = "blocked"
    data: BlockedData


class TradePostedEvent(_EventBase):
    type: Literal["trade.posted"] = "trade.posted"
    data: TradeData


class TradeFilledEvent(_EventBase):
    type: Literal["trade.filled"] = "trade.filled"
    data: TradeData


class StreamStaleEvent(_EventBase):
    type: Literal["stream.stale"] = "stream.stale"
    data: StreamStaleData


class FrozenEvent(_EventBase):
    type: Literal["frozen"] = "frozen"
    data: FrozenData


class CallStartedEvent(_EventBase):
    type: Literal["call.started"] = "call.started"
    data: CallData


class CallEndedEvent(_EventBase):
    type: Literal["call.ended"] = "call.ended"
    data: CallData


class PolicyUpdatedEvent(_EventBase):
    type: Literal["policy.updated"] = "policy.updated"
    data: PolicyData


class ForecastCommittedEvent(_EventBase):
    type: Literal["forecast.committed"] = "forecast.committed"
    data: ForecastData


class ForecastRevealedEvent(_EventBase):
    type: Literal["forecast.revealed"] = "forecast.revealed"
    data: ForecastData


SwitchboardEvent = Annotated[
    TransferSentEvent
    | TransferArrivedEvent
    | ApprovalRequestedEvent
    | ApprovalResolvedEvent
    | BlockedEvent
    | TradePostedEvent
    | TradeFilledEvent
    | StreamStaleEvent
    | FrozenEvent
    | CallStartedEvent
    | CallEndedEvent
    | PolicyUpdatedEvent
    | ForecastCommittedEvent
    | ForecastRevealedEvent,
    Field(discriminator="type"),
]

EVENT_PAYLOADS: dict[str, type[Model]] = {
    "transfer.sent": TransferSentData,
    "transfer.arrived": TransferArrivedData,
    "approval.requested": ApprovalRequestedData,
    "approval.resolved": ApprovalResolvedData,
    "blocked": BlockedData,
    "trade.posted": TradeData,
    "trade.filled": TradeData,
    "stream.stale": StreamStaleData,
    "frozen": FrozenData,
    "call.started": CallData,
    "call.ended": CallData,
    "policy.updated": PolicyData,
    "forecast.committed": ForecastData,
    "forecast.revealed": ForecastData,
}

FROZEN_EVENT_TYPES: tuple[str, ...] = (
    "transfer.sent",
    "transfer.arrived",
    "approval.requested",
    "approval.resolved",
    "blocked",
    "trade.posted",
    "trade.filled",
    "stream.stale",
    "frozen",
)

EVENT_TYPES: tuple[str, ...] = tuple(EVENT_PAYLOADS)


class EventHistory(Model):
    events: list[SwitchboardEvent]


# --------------------------------------------------------------------------- P2 additions: routes


class Policy(Model):
    transfer_cap_usd: Money
    daily_budget_usd: Money
    allowlist: list[str]
    approval_expiry_s: int


class ApprovalStats(Model):
    total: int
    approved: int
    denied: int
    rate_pct: Money | None = Field(description="Approved share of resolved requests, 0 to 100.")
    warning: str = ""


class StatsResponse(Model):
    policy: Policy
    spent_today_usd: Money
    budget_remaining_usd: Money
    approvals: ApprovalStats
    calls_today: int
    max_calls_per_day: int
    frozen: bool
    stream_stale: bool
    trading_enabled: bool
    max_order_usd: Money
    calls_live: bool
    using_p1_stubs: bool
    passkey_threshold_usd: Money
    passkey_registered: bool


class ApprovalView(Model):
    """What the owner's dashboard shows for an approval request.

    The challenge word appears here and nowhere else: it is never sent to the voice agent.
    """

    request_id: str
    plan_id: str | None
    amount_usd: Money
    payee_label: str
    reason: Reason
    status: str
    challenge: str | None
    expires_at: datetime
    requires_passkey: bool
    voice_ok: bool
    passkey_ok: bool
    case_for: str
    case_against: str
    call_status: str
    source: str


class ApprovalList(Model):
    approvals: list[ApprovalView]


class SimulatedVoiceRequest(Model):
    amount_spoken: Money
    challenge: str = Field(max_length=64)


class Wallet(Model):
    owner: str
    label: str
    chain: Chain
    address: str
    allowlisted: bool


class WalletList(Model):
    wallets: list[Wallet]


class Venue(Model):
    id: str
    name: str
    status: Literal["connected", "fixtures", "planned"]
    note: str = ""


class VenueList(Model):
    venues: list[Venue]


class StreamToggleRequest(Model):
    stale: bool


class FreezeRequest(Model):
    frozen: bool


class JsonObject(Model):
    """An untyped JSON object, used where a third party defines the body."""

    model_config = ConfigDict(extra="allow")


AnyJson = dict[str, Any]
