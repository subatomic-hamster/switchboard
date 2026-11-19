"""SQLModel tables and the database handle.

Amounts are stored as integer micro-dollars (six decimals, the same unit as USDC), so SQLite
never holds a binary float for money.
"""

from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Field, Session, SQLModel, create_engine

MICRO = Decimal(1_000_000)


def to_micro(amount: Decimal) -> int:
    return int((amount * MICRO).to_integral_value(rounding=ROUND_HALF_EVEN))


def from_micro(value: int) -> Decimal:
    return Decimal(value) / MICRO


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes; everything stored is UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class EventRow(SQLModel, table=True):
    """Every event published on the bus, in order. Feeds SSE replay and the flight recorder."""

    id: int | None = Field(default=None, primary_key=True)
    type: str = Field(index=True)
    ts: datetime = Field(index=True)
    data: str


class PlanRow(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    created: datetime
    text: str
    kind: str = "transfer"
    parser: str = "rules"
    request_json: str = ""
    routes_json: str = "[]"
    status: str = "quoted"
    route_id: str = ""
    vault_request_id: str | None = Field(default=None, index=True)
    permit_id: str | None = None
    transfer_id: str | None = None
    source: str = "user"


class ApprovalRow(SQLModel, table=True):
    request_id: str = Field(primary_key=True)
    plan_id: int | None = None
    amount_micro: int
    payee_label: str
    payee_address: str
    reason: str
    challenge: str
    status: str = Field(default="pending", index=True)
    created: datetime
    expires_at: datetime
    voice_ok: bool = False
    passkey_required: bool = False
    passkey_ok: bool = False
    attempts: int = 0
    resolved_via: str = ""
    case_for: str = ""
    case_against: str = ""
    call_status: str = ""
    conversation_id: str = ""
    source: str = "user"


class CallRow(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    request_id: str = Field(index=True)
    ts: datetime = Field(index=True)
    dry_run: bool
    status: str
    conversation_id: str = ""
    detail: str = ""


class PhoneSessionRow(SQLModel, table=True):
    """One AgentPhone call, and how far its scripted approval conversation has got.

    `amount_heard` and `word_heard` are what the caller has said so far, kept so the amount
    and the word can arrive in separate turns. The expected word is never stored here.
    """

    call_id: str = Field(primary_key=True)
    request_id: str = Field(index=True)
    created: datetime
    state: str = "stated"
    turns: int = 0
    amount_heard: str = ""
    word_heard: str = ""


class LedgerRow(SQLModel, table=True):
    """One line per movement of money or position, for the daily summary and reconciliation."""

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(index=True)
    kind: str
    ref: str = Field(index=True)
    plan_id: int | None = None
    from_place: str = ""
    to_place: str = ""
    amount_micro: int = 0
    cost_micro: int = 0
    payee_label: str = ""
    status: str = ""
    note: str = ""


class PasskeyRow(SQLModel, table=True):
    credential_id: str = Field(primary_key=True)
    public_key: str
    sign_count: int = 0
    created: datetime


class ForecastRow(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    created: datetime
    slug: str = Field(index=True)
    question: str = ""
    market_price_micro: int
    probability_micro: int
    opinion: str
    models_json: str = "[]"
    salt: str
    commitment: str = Field(index=True)
    anchor_ref: str = ""
    event_time: datetime | None = None
    revealed: bool = False
    outcome: bool | None = None
    brier_micro: int | None = None
    overridden: bool = False


class RuleProposalRow(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    created: datetime
    expires_at: datetime
    text: str
    readback: str
    change_json: str
    challenge: str
    status: str = "pending"
    attempts: int = 0


class BotNoteRow(SQLModel, table=True):
    """Text posted by a Grok Bot routine. Stored and shown as untrusted content; never acted on."""

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(index=True)
    routine: str = Field(index=True)
    subject: str = ""
    body: str


class ConnectorRow(SQLModel, table=True):
    """One outside assistant (ChatGPT, Claude, Grok, ...) that may ask Switchboard to send.

    The token itself is never stored: only its SHA-256, and its last four characters so the
    owner can tell two connectors apart. The two limits are what this assistant may send
    without the owner being asked; they sit in front of the vault's own limits, never in
    place of them.
    """

    id: int | None = Field(default=None, primary_key=True)
    name: str
    kind: str = "other"
    # "header": the token is sent as `Authorization: Bearer`. "link": the token is part of the
    # address, for assistants whose settings take only a URL. Each token works one way only.
    auth: str = "header"
    token_hash: str = Field(index=True, unique=True)
    token_hint: str = ""
    created: datetime
    revoked_at: datetime | None = None
    auto_send_micro: int = 0
    daily_micro: int = 0
    last_used: datetime | None = None


class ConnectorRequestRow(SQLModel, table=True):
    """Links a plan to the connector that asked for it. `asked` is true when the owner had
    to be asked about it."""

    plan_id: int = Field(primary_key=True)
    connector_id: int = Field(index=True)
    created: datetime = Field(index=True)
    amount_micro: int
    payee_address: str
    asked: bool = False


class HostileRow(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    ts: datetime
    content: str
    extracted_json: str
    outcome: str
    reason: str = ""
    request_id: str | None = None


class Database:
    """Owns the engine. `path=None` gives a private in-memory database for tests."""

    def __init__(self, path: Path | None) -> None:
        if path is None:
            self.engine: Engine = create_engine(
                "sqlite://",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.engine = create_engine(
                f"sqlite:///{path.as_posix()}",
                connect_args={"check_same_thread": False},
            )
        SQLModel.metadata.create_all(self.engine)

    def session(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    def close(self) -> None:
        self.engine.dispose()
