"""The real QuickNode Stream webhook (`app/chain/stream.py`), mounted with USE_P1_STUBS=false.

No chain is involved: the logs are encoded here with the contract's event signatures.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import itertools
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from eth_abi.abi import encode
from fastapi import FastAPI
from web3 import Web3

from app.chain.vault import decode_vault_log
from app.context import AppContext
from app.db import Database
from app.main import create_app
from tests.conftest import make_settings

SECRET = "s3cret"
VAULT = "0x" + "11" * 20
OTHER = "0x" + "22" * 20
AGENT = "0x" + "aa" * 20
PAYEE = "0x" + "bb" * 20
KEY = "0x" + "01" * 32
PATH = "/api/hooks/quicknode"


def make_log(address: str, signature: str, types: list[str], values: list[Any]) -> dict[str, Any]:
    return {
        "address": address,
        "topics": ["0x" + Web3.keccak(text=signature).hex().removeprefix("0x")],
        "data": "0x" + encode(types, values).hex(),
    }


def approval_log(address: str = VAULT, reason: int = 1, request_id: int = 7) -> dict[str, Any]:
    return make_log(
        address,
        "ApprovalRequested(uint256,address,address,uint256,uint8)",
        ["uint256", "address", "address", "uint256", "uint8"],
        [request_id, AGENT, PAYEE, 250_500_000, reason],
    )


def resolved_log(approved: bool, request_id: int = 7) -> dict[str, Any]:
    return make_log(VAULT, "Resolved(uint256,bool)", ["uint256", "bool"], [request_id, approved])


def frozen_log(frozen: bool) -> dict[str, Any]:
    return make_log(VAULT, "Frozen(bool)", ["bool"], [frozen])


def permit_log() -> dict[str, Any]:
    return make_log(
        VAULT,
        "PermitIssued(uint256,address,address,uint256,uint64)",
        ["uint256", "address", "address", "uint256", "uint64"],
        [1, AGENT, PAYEE, 50_000_000, 123],
    )


def spent_log() -> dict[str, Any]:
    return make_log(
        VAULT,
        "Spent(uint256,address,address,uint256)",
        ["uint256", "address", "address", "uint256"],
        [1, AGENT, PAYEE, 50_000_000],
    )


_nonces = itertools.count(1)


def sign(
    body: bytes, ts: int | None = None, secret: str = SECRET, nonce: str | None = None
) -> dict[str, str]:
    """Headers for one delivery. Each call gets a fresh nonce, as QuickNode's do."""
    nonce = nonce or f"n0nce-{next(_nonces)}"
    stamp = str(int(time.time()) if ts is None else ts)
    mac = hmac.new(secret.encode(), nonce.encode() + stamp.encode() + body, hashlib.sha256)
    return {"x-qn-nonce": nonce, "x-qn-timestamp": stamp, "x-qn-signature": mac.hexdigest()}


def flat(*logs: dict[str, Any]) -> bytes:
    """The shape server/quicknode/filter.js delivers."""
    return json.dumps({"logs": list(logs)}).encode()


@pytest.fixture
async def real_app() -> AsyncIterator[FastAPI]:
    settings = make_settings(
        use_p1_stubs=False,
        vault_address=VAULT,
        arc_rpc_url="http://127.0.0.1:1",  # never contacted
        owner_private_key=KEY,
        agent_private_key=KEY,
        approver_private_key=KEY,
        quicknode_stream_secret=SECRET,
    )
    application = create_app(settings, Database(None))
    yield application
    await application.state.ctx.aclose()


@pytest.fixture
def real_ctx(real_app: FastAPI) -> AppContext:
    ctx: AppContext = real_app.state.ctx
    return ctx


Hook = Callable[..., Any]


@pytest.fixture
async def hook(real_app: FastAPI) -> AsyncIterator[Hook]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app), base_url="http://localhost"
    ) as http:

        async def post(body: bytes, headers: dict[str, str]) -> httpx.Response:
            return await http.post(PATH, content=body, headers=headers)

        yield post


def published(ctx: AppContext) -> list[tuple[str, dict[str, Any]]]:
    """Vault events only: the watchdog's `stream.stale` has its own tests."""
    return [(e.type, e.data) for e in ctx.bus.history() if e.type != "stream.stale"]


def test_decode_vault_log() -> None:
    event = decode_vault_log(approval_log())
    assert event is not None
    assert event["event"] == "ApprovalRequested"
    assert (event["requestId"], event["amount"], event["reason"]) == (7, 250_500_000, 1)
    assert event["payee"] == Web3.to_checksum_address(PAYEE)


async def test_valid_delivery_publishes_typed_events(hook: Hook, real_ctx: AppContext) -> None:
    body = flat(
        approval_log(reason=3),
        resolved_log(True),
        resolved_log(False, request_id=8),
        frozen_log(True),
        permit_log(),
        spent_log(),
        approval_log(OTHER),
    )
    response = await hook(body, sign(body))
    assert response.status_code == 200
    assert response.json()["status"] == "published 4"  # permit, spent and a foreign vault: none
    events = published(real_ctx)
    assert [t for t, _ in events] == [
        "approval.requested",
        "approval.resolved",
        "approval.resolved",
        "frozen",
    ]
    assert events[0][1] == {
        "request_id": "7",
        "agent": Web3.to_checksum_address(AGENT),
        "payee": Web3.to_checksum_address(PAYEE),
        "amount_usd": 250.5,
        "reason": "PAYEE_NOT_ALLOWED",
    }
    assert events[1][1] == {"request_id": "7", "approved": True, "via": "chain"}
    assert events[2][1] == {"request_id": "8", "approved": False, "via": "chain"}
    assert events[3][1] == {"frozen": True, "via": "chain"}
    assert real_ctx.last_stream_delivery is not None


async def test_block_with_receipts_shape_and_gzip(hook: Hook, real_ctx: AppContext) -> None:
    body = json.dumps([{"receipts": [{"logs": [approval_log(reason=2)]}]}]).encode()
    headers = {**sign(body), "content-encoding": "gzip"}  # signed over the uncompressed JSON
    response = await hook(gzip.compress(body), headers)
    assert response.status_code == 200
    assert [t for t, _ in published(real_ctx)] == ["approval.requested"]


async def test_frozen_reason_publishes_nothing(hook: Hook, real_ctx: AppContext) -> None:
    body = flat(approval_log(reason=4))
    response = await hook(body, sign(body))
    assert response.status_code == 200
    assert published(real_ctx) == []


async def test_foreign_address_is_ignored(hook: Hook, real_ctx: AppContext) -> None:
    body = flat(approval_log(OTHER))
    assert (await hook(body, sign(body))).status_code == 200
    assert published(real_ctx) == []


async def test_a_redelivered_log_is_published_once(hook: Hook, real_ctx: AppContext) -> None:
    entry = {**resolved_log(True), "transactionHash": "0xabc", "logIndex": "0x2"}
    body = flat(entry)
    await hook(body, sign(body))
    await hook(body, sign(body))
    assert [t for t, _ in published(real_ctx)] == ["approval.resolved"]


async def test_bad_signature_is_rejected(hook: Hook, real_ctx: AppContext) -> None:
    body = flat(approval_log())
    headers = sign(body)
    headers["x-qn-signature"] = "00" * 32
    assert (await hook(body, headers)).status_code == 401
    assert (await hook(body, sign(body, secret="wrong"))).status_code == 401
    assert published(real_ctx) == []
    assert real_ctx.last_stream_delivery is None


async def test_missing_header_is_rejected(hook: Hook) -> None:
    body = flat(approval_log())
    headers = sign(body)
    del headers["x-qn-signature"]
    assert (await hook(body, headers)).status_code == 401


async def test_stale_timestamp_is_rejected(hook: Hook) -> None:
    body = flat(approval_log())
    assert (await hook(body, sign(body, ts=int(time.time()) - 301))).status_code == 401


async def test_unset_secret_rejects_everything(hook: Hook, real_ctx: AppContext) -> None:
    real_ctx.settings.quicknode_stream_secret = ""
    body = flat(approval_log())
    assert (await hook(body, sign(body, secret=""))).status_code == 401


async def test_gzip_bomb_is_cut_off_before_auth(hook: Hook) -> None:
    bomb = gzip.compress(b"\0" * (11 * 1024 * 1024))  # inflates past the 10 MB cap
    headers = {**sign(b"{}"), "content-encoding": "gzip"}
    assert (await hook(bomb, headers)).status_code == 413


async def test_stub_hook_serves_the_path_when_stubs_are_on(client: httpx.AsyncClient) -> None:
    response = await client.post(PATH, json={})
    assert response.status_code == 200
    assert response.json()["status"].startswith("stub")


def test_real_mode_needs_the_vault_settings() -> None:
    with pytest.raises(RuntimeError, match=r"VAULT_ADDRESS.*AGENT_PRIVATE_KEY"):
        create_app(make_settings(use_p1_stubs=False), Database(None))
