"""Escrow routes without a chain, and the Stream decoder for the Ledger's and Escrow's logs.

No chain is involved: logs are encoded here with the contracts' event signatures (needs the
built ABIs in `contracts/out`, like `test_stream.py`).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.context import AppContext
from app.db import Database
from app.main import create_app
from tests.conftest import make_settings
from tests.test_stream import AGENT, KEY, PATH, PAYEE, SECRET, make_log, sign

LEDGER = "0x" + "33" * 20
ESCROW = "0x" + "44" * 20
TUNNEL = {"Host": "random-words.trycloudflare.com", "Cf-Connecting-Ip": "203.0.113.9"}
ZERO = "0x" + "00" * 20


async def test_escrow_routes_answer_503_when_not_configured(
    client: httpx.AsyncClient, ctx: AppContext
) -> None:
    assert ctx.escrow is None
    assert (await client.post("/api/escrow/create", json={"amount_usd": 5})).status_code == 503
    assert (await client.post("/api/escrow/1/refund")).status_code == 503
    assert (await client.get("/api/escrow/1")).status_code == 503
    claim = {"escrow_id": "1", "secret": "0x" + "ab" * 32, "to": PAYEE}
    assert (await client.post("/api/escrow/claim", json=claim)).status_code == 503


async def test_only_claim_is_reachable_through_a_tunnel(client: httpx.AsyncClient) -> None:
    claim = {"escrow_id": "1", "secret": "0x" + "ab" * 32, "to": PAYEE}
    # The tunnel reaches the claim handler (503: no escrow here), and nothing else.
    assert (await client.post("/api/escrow/claim", json=claim, headers=TUNNEL)).status_code == 503
    for method, path in [
        ("POST", "/api/escrow/create"),
        ("POST", "/api/escrow/1/refund"),
        ("POST", "/api/escrow/requests"),
        ("POST", "/api/escrow/requests/1/pay"),
        ("GET", "/api/escrow/1"),
    ]:
        response = await client.request(method, path, json={}, headers=TUNNEL)
        assert response.status_code == 403, path


async def test_claim_validates_its_input(client: httpx.AsyncClient) -> None:
    bad_secret = {"escrow_id": "1", "secret": "not-hex", "to": PAYEE}
    assert (await client.post("/api/escrow/claim", json=bad_secret)).status_code == 422


@pytest.fixture
async def real_app() -> AsyncIterator[FastAPI]:
    settings = make_settings(
        use_p1_stubs=False,
        vault_address="0x" + "11" * 20,
        ledger_address=LEDGER,
        escrow_address=ESCROW,
        arc_rpc_url="http://127.0.0.1:1",  # never contacted
        owner_private_key=KEY,
        agent_private_key=KEY,
        approver_private_key=KEY,
        quicknode_stream_secret=SECRET,
    )
    application = create_app(settings, Database(None))
    yield application
    await application.state.ctx.aclose()


Deliver = Callable[..., Any]


@pytest.fixture
async def deliver(real_app: FastAPI) -> AsyncIterator[Deliver]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app), base_url="http://localhost"
    ) as http:

        async def post(*logs: dict[str, Any]) -> httpx.Response:
            body = json.dumps({"logs": list(logs)}).encode()
            return await http.post(PATH, content=body, headers=sign(body))

        yield post


def escrow_log(signature: str, types: list[str], values: list[Any], address: str = ESCROW) -> Any:
    log = make_log(address, signature, types, values)
    return {
        **log,
        "transactionHash": "0x" + f"{abs(hash(signature + str(values))):064x}"[:64],
        "logIndex": "0x0",
    }


async def test_escrow_logs_become_events(real_app: FastAPI, deliver: Deliver) -> None:
    ctx: AppContext = real_app.state.ctx
    deposited = escrow_log(
        "Deposited(uint256,address,bytes32,uint256,uint64,address)",
        ["uint256", "address", "bytes32", "uint256", "uint64", "address"],
        [1, AGENT, b"\x01" * 32, 25_500_000, 2_000_000_000, PAYEE],
    )
    committed = escrow_log(
        "ClaimCommitted(uint256,bytes32)", ["uint256", "bytes32"], [1, b"\x02" * 32]
    )
    claimed = escrow_log(
        "Claimed(uint256,address,uint256)",
        ["uint256", "address", "uint256"],
        [1, PAYEE, 25_500_000],
    )
    refunded = escrow_log(
        "Refunded(uint256,address,uint256)",
        ["uint256", "address", "uint256"],
        [2, AGENT, 3_000_000],
    )
    requested = escrow_log(
        "PaymentRequested(uint256,address,address,uint256,string)",
        ["uint256", "address", "address", "uint256", "string"],
        [4, AGENT, ZERO, 9_000_000, "lunch"],
    )
    paid = escrow_log(
        "PaymentPaid(uint256,address,address,uint256)",
        ["uint256", "address", "address", "uint256"],
        [4, AGENT, PAYEE, 9_000_000],
    )
    response = await deliver(deposited, committed, claimed, refunded, requested, paid)
    assert response.status_code == 200
    assert response.json()["status"] == "published 5"  # the commit is plumbing

    events = [(e.type, e.data) for e in ctx.bus.history()]
    assert [t for t, _ in events] == [
        "escrow.updated",
        "escrow.updated",
        "escrow.updated",
        "payment.updated",
        "payment.updated",
    ]
    assert events[0][1]["status"] == "deposited" and events[0][1]["amount_usd"] == 25.5
    assert events[1][1] == {
        "escrow_id": "1",
        "status": "claimed",
        "amount_usd": 25.5,
        "to": events[1][1]["to"],
        "expiry": None,
    }
    assert events[1][1]["to"].lower() == PAYEE
    assert events[2][1]["status"] == "refunded"
    assert events[3][1]["payer"] == "" and events[3][1]["memo"] == "lunch"  # open request
    assert events[4][1]["status"] == "paid"


async def test_ledger_logs_are_recognised_but_not_republished(
    real_app: FastAPI, deliver: Deliver
) -> None:
    """The forecast module publishes forecast.committed/revealed itself; the Stream must not
    show them a second time."""
    ctx: AppContext = real_app.state.ctx
    committed = escrow_log(
        "Committed(uint256,address,bytes32,string,uint64)",
        ["uint256", "address", "bytes32", "string", "uint64"],
        [1, AGENT, b"\x03" * 32, "forecast:x", 123],
        address=LEDGER,
    )
    revealed = escrow_log(
        "Revealed(uint256,bytes)", ["uint256", "bytes"], [1, b"{}"], address=LEDGER
    )
    response = await deliver(committed, revealed)
    assert response.json()["status"] == "published 0"
    assert ctx.bus.history() == []


async def test_logs_from_other_addresses_are_ignored(deliver: Deliver, real_app: FastAPI) -> None:
    other = escrow_log(
        "Claimed(uint256,address,uint256)",
        ["uint256", "address", "uint256"],
        [1, PAYEE, 1_000_000],
        address="0x" + "99" * 20,
    )
    assert (await deliver(other)).json()["status"] == "published 0"
