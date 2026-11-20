"""The anomaly baseline (`app/chain/anomaly.py`) and how it turns an allowed spend into an ask."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI

from app.chain.anomaly import AnomalyDetector, Spend
from app.config import Settings
from app.context import AppContext
from app.db import Database
from app.main import create_app
from tests.conftest import make_settings

SAM = "0x4ccFA9e40f035038edfe5b6A5c5111993B1bEFfA"
NOW = datetime(2026, 3, 10, 14, 30, tzinfo=UTC)


def detector(**overrides: object) -> AnomalyDetector:
    return AnomalyDetector(
        make_settings(anomaly_enabled=True, **overrides),
        Database(None),
        _NullBus(),  # type: ignore[arg-type]
    )


class _NullBus:
    def on(self, *_: object) -> None: ...


def fill(det: AnomalyDetector, amounts: list[str], *, payee: str = SAM, hours: int = 1) -> None:
    """History of one spend every `hours` hours, the last one `hours` before NOW."""
    for i, amount in enumerate(reversed(amounts), start=1):
        det.history.appendleft(
            Spend(NOW - timedelta(hours=hours * i), Decimal(amount), payee.lower())
        )


STEADY = ["2", "2", "2.50", "2", "1.50", "2", "2", "2.25"]


def test_with_too_little_history_nothing_is_flagged() -> None:
    det = detector()
    fill(det, STEADY[:7])  # one short of the eight needed
    assert det.assess("0x" + "ab" * 20, Decimal("5000"), NOW) is None


def test_a_spend_like_the_usual_ones_is_not_flagged() -> None:
    det = detector()
    fill(det, STEADY)
    assert det.assess(SAM, Decimal("2.75"), NOW) is None


def test_a_large_jump_is_flagged_with_a_sentence_the_operator_can_say() -> None:
    det = detector()
    fill(det, STEADY)
    anomaly = det.assess(SAM, Decimal("50"), NOW)
    assert anomaly is not None
    assert anomaly.reasons == ("This is 25 times your usual transfer of 2 dollars.",)
    assert anomaly.text.endswith("If you did not expect it, say deny.")


def test_five_times_is_the_line_and_the_factor_is_a_setting() -> None:
    det = detector()
    fill(det, ["2"] * 8)  # no spread at all: the factor alone decides
    assert det.assess(SAM, Decimal("9.99"), NOW) is None
    assert det.assess(SAM, Decimal("10"), NOW) is not None
    loose = detector(anomaly_amount_factor=Decimal("20"))
    fill(loose, ["2"] * 8)
    assert loose.assess(SAM, Decimal("10"), NOW) is None


def test_a_noisy_history_needs_a_bigger_jump_than_a_steady_one() -> None:
    det = detector()
    fill(det, ["1", "1", "1", "30", "30", "30", "10", "12"])  # median 11, MAD 10
    assert det.assess(SAM, Decimal("55"), NOW) is None  # five times, but z is about 3.0
    assert det.assess(SAM, Decimal("70"), NOW) is not None  # z is about 4.0


def test_a_first_time_payee_is_flagged() -> None:
    det = detector()
    fill(det, STEADY)
    anomaly = det.assess("0x" + "cd" * 20, Decimal("2"), NOW)
    assert anomaly is not None
    assert anomaly.reasons == ("This agent has never paid this address before.",)
    assert det.assess(SAM.upper().replace("0X", "0x"), Decimal("2"), NOW) is None  # case-blind


def test_a_burst_is_flagged_only_when_the_history_never_paid_that_fast() -> None:
    det = detector()
    fill(det, STEADY)
    for minutes in (2, 4, 6):  # three spends in the last ten minutes: this would be the fourth
        det.history.append(Spend(NOW - timedelta(minutes=minutes), Decimal("2"), SAM.lower()))
    anomaly = det.assess(SAM, Decimal("2"), NOW)
    assert anomaly is not None
    assert "payment number 4 in 10 minutes" in anomaly.reasons[0]

    # An agent that has always paid in bursts of four is not surprised by a fifth.
    busy = detector()
    fill(busy, STEADY)
    for minutes in (2, 4, 6):
        busy.history.appendleft(
            Spend(NOW - timedelta(days=1, minutes=minutes), Decimal("2"), SAM.lower())
        )
        busy.history.append(Spend(NOW - timedelta(minutes=minutes), Decimal("2"), SAM.lower()))
    busy.history.appendleft(Spend(NOW - timedelta(days=1), Decimal("2"), SAM.lower()))
    assert busy.assess(SAM, Decimal("2"), NOW) is None


def test_an_hour_the_agent_never_spends_in_is_flagged() -> None:
    det = detector()
    fill(det, STEADY)  # every spend in the 6 to 13 UTC range
    night = NOW.replace(hour=2)
    anomaly = det.assess(SAM, Decimal("2"), night)
    assert anomaly is not None
    assert anomaly.reasons == ("It is an hour of the day when this agent has never spent.",)
    assert det.assess(SAM, Decimal("2"), NOW.replace(hour=15)) is None  # next to the usual ones


def test_every_reason_is_listed() -> None:
    det = detector()
    fill(det, STEADY)
    anomaly = det.assess("0x" + "cd" * 20, Decimal("50"), NOW.replace(hour=2))
    assert anomaly is not None and len(anomaly.reasons) == 3


# --------------------------------------------------------------------------- switching it on


def test_on_with_the_real_vault_and_off_with_stubs_unless_set() -> None:
    assert make_settings().anomaly_on is False
    assert make_settings(use_p1_stubs=False).anomaly_on is True
    assert make_settings(anomaly_enabled=True).anomaly_on is True
    assert make_settings(use_p1_stubs=False, anomaly_enabled=False).anomaly_on is False


async def test_p2s_stub_mode_has_no_detector() -> None:
    plain = create_app(make_settings(), Database(None)).state.ctx
    assert plain.holds._anomaly is None
    await plain.aclose()


# --------------------------------------------------------------------------- the phone rings


@pytest.fixture
def settings() -> Settings:
    return make_settings(anomaly_enabled=True)


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings, Database(None))
    yield application
    await application.state.ctx.aclose()


async def _execute(client: httpx.AsyncClient, text: str) -> dict[str, object]:
    plan = (await client.post("/api/intent", json={"text": text})).json()
    response = await client.post(f"/api/plans/{plan['plan_id']}/execute", json={})
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def _history(ctx: AppContext) -> AnomalyDetector:
    det = ctx.holds._anomaly
    assert det is not None
    now = datetime.now(UTC)
    for i in range(1, 9):
        det.history.appendleft(Spend(now - timedelta(minutes=30 * i), Decimal("2"), SAM.lower()))
    return det


async def test_an_allowed_but_unusual_spend_rings_the_phone_with_the_reason(
    client: httpx.AsyncClient, ctx: AppContext
) -> None:
    _history(ctx)
    body = await _execute(client, "send 50 from my arc wallet to sam's arc wallet")
    # The vault itself would allow 50 to Sam: 50 is under the 200 cap and Sam is listed.
    assert body["status"] == "needs_approval"
    assert body["reason"] == "UNUSUAL_SPEND"
    request_id = str(body["request_id"])
    assert request_id.startswith("h")
    assert await ctx.vault.spent_today() == Decimal(0)  # nothing was asked of the vault yet

    row = ctx.approvals.get(request_id)
    assert row is not None
    assert row.case_against.startswith("This is 25 times your usual transfer of 2 dollars.")
    assert "You asked for this yourself" in row.case_for  # the sentence is still the case for

    result = await ctx.approvals.approve(request_id, Decimal(50), row.challenge)
    assert result.ok and result.status == "approved"
    assert await ctx.vault.spent_today() == Decimal(50)


async def test_a_denied_unusual_spend_moves_nothing(
    client: httpx.AsyncClient, ctx: AppContext
) -> None:
    _history(ctx)
    body = await _execute(client, "send 50 from my arc wallet to sam's arc wallet")
    await ctx.approvals.deny(str(body["request_id"]))
    assert await ctx.vault.spent_today() == Decimal(0)
    assert ctx.bus.latest("transfer.sent") is None


async def test_a_usual_spend_goes_straight_through_and_joins_the_history(
    client: httpx.AsyncClient, ctx: AppContext
) -> None:
    det = _history(ctx)
    before = len(det.history)
    body = await _execute(client, "send 2 from my arc wallet to sam's arc wallet")
    assert body["status"] == "executing"
    assert len(det.history) == before + 1
    assert det.history[-1].amount == Decimal(2) and det.history[-1].payee == SAM.lower()


async def test_what_the_vault_would_refuse_keeps_the_vaults_own_reason(
    client: httpx.AsyncClient, ctx: AppContext
) -> None:
    _history(ctx)
    body = await _execute(client, "send 250 from my arc wallet to sam's arc wallet")
    assert body["reason"] == "OVER_TRANSFER_CAP"
    assert not str(body["request_id"]).startswith("h")


async def test_a_frozen_vault_is_still_frozen_not_unusual(
    client: httpx.AsyncClient, ctx: AppContext
) -> None:
    _history(ctx)
    await ctx.vault.freeze(True)
    body = await _execute(client, "send 50 from my arc wallet to sam's arc wallet")
    assert body["status"] == "blocked" and body["reason"] == "FROZEN"


async def test_a_restart_keeps_the_baseline(client: httpx.AsyncClient, ctx: AppContext) -> None:
    await _execute(client, "send 2 from my arc wallet to sam's arc wallet")
    reloaded = AnomalyDetector(ctx.settings, ctx.db, ctx.bus)  # a new process, the same database
    assert [(s.amount, s.payee) for s in reloaded.history] == [(Decimal(2), SAM.lower())]
