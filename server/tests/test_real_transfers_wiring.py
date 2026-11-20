"""USE_REAL_TRANSFERS: what it wires in, which rails the planner then offers, and the direct route
that must not reach a real wallet. No chain is touched (every client is built lazily)."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.chain.stub import StubTransfers
from app.chain.transfer import CctpTransfers
from app.config import FIXTURES_DIR, Settings
from app.db import Database
from app.intent import IntentError
from app.main import build_context, create_app
from app.planner import load_rails
from app.schemas import Chain, Party, TransferRequest
from tests.conftest import make_settings

KEY = "0x" + "11" * 32


def real_settings(**extra: Any) -> Settings:
    return make_settings(
        use_p1_stubs=False,
        use_real_transfers=True,
        vault_address="0x" + "22" * 20,
        arc_rpc_url="http://arc.invalid",
        owner_private_key=KEY,
        agent_private_key=KEY,
        approver_private_key=KEY,
        quicknode_stream_secret="s",
        **extra,
    )


def _request(source: Chain, dest: Chain, asset: str = "USDC") -> TransferRequest:
    return TransferRequest.model_validate(
        {
            "amount_usd": "10",
            "to_asset": asset,
            "source": Party(label="a", chain=source, address="0x1", known=True),
            "destination": Party(label="b", chain=dest, address="0x2", known=True),
        }
    )


def test_the_flag_wires_in_the_real_module_only_with_the_real_vault() -> None:
    assert isinstance(build_context(real_settings(), Database(None)).transfers, CctpTransfers)
    stubbed = make_settings(use_real_transfers=True)  # USE_P1_STUBS is still true
    assert isinstance(build_context(stubbed, Database(None)).transfers, StubTransfers)
    assert isinstance(build_context(make_settings(), Database(None)).transfers, StubTransfers)


def test_every_live_rail_starts_on_arc_and_every_rail_has_a_source() -> None:
    raw = json.loads((FIXTURES_DIR / "rails.json").read_text(encoding="utf-8"))
    for item in raw["rails"]:
        assert item["source"].strip()
        assert item["basis"] in {"quoted", "documented", "estimate"}
    rails, fx = load_rails(live_only=True)
    assert rails
    assert all(r.source is Chain.ARC_TESTNET for r in rails)
    assert not fx.live
    everything, _ = load_rails()
    assert len(everything) > len(rails)  # the Sepolia and Solana sources are not live


def test_the_planner_offers_only_routes_it_can_run() -> None:
    planner = build_context(real_settings(), Database(None)).planner
    routes = planner.routes(_request(Chain.ARC_TESTNET, Chain.SOLANA_DEVNET))
    assert [s.rail for s in routes[0].steps] == ["cctp-standard"]
    assert routes[0].cost_usd == Decimal("0.23")
    same_chain = planner.routes(_request(Chain.ARC_TESTNET, Chain.ARC_TESTNET))
    assert same_chain[0].steps[0].rail == "arc-native"
    assert planner.routes(_request(Chain.ETH_SEPOLIA, Chain.ARC_TESTNET)) == []
    assert planner.routes(_request(Chain.ARC_TESTNET, Chain.ETH_SEPOLIA, "EURC")) == []


async def test_a_transfer_not_from_arc_says_what_does_work() -> None:
    planner = build_context(real_settings(), Database(None)).planner
    request = _request(Chain.ETH_SEPOLIA, Chain.SOLANA_DEVNET).model_copy(
        update={"source": Party(label="my Ethereum wallet", chain=Chain.ETH_SEPOLIA, known=True)}
    )
    with pytest.raises(
        IntentError, match=r"from your Ethereum wallet isn't live yet\..*Arc wallet"
    ):
        await planner.plan_transfer(request, "test", "test")


async def test_the_direct_transfer_route_is_closed_when_transfers_are_real() -> None:
    app = create_app(real_settings(), Database(None))
    body = {
        "from_chain": "arc-testnet",
        "to_chain": "eth-sepolia",
        "to": "0x" + "5a" * 20,
        "amount_usd": 1,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as http:
        response = await http.post("/api/chain/transfer", json=body)
    assert response.status_code == 403
    assert "vault" in response.json()["detail"]
