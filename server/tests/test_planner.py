"""Planner: routes, the pay / ask / block decision, and execution against the stub vault."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx

from app.context import AppContext
from app.planner import find_routes, load_rails, say_money, say_time
from app.schemas import Chain, Party, TransferRequest

NEW_ADDRESS = "0x9c1e8a4f2b7d6c5a3e1f0b9d8c7a6e5f4d3c4f2c"
SENTENCE = "Move two hundred dollars from my Ethereum wallet to Sam's Solana wallet."


def _request(
    source: Chain, dest: Chain, amount: str = "200", asset: str = "USDC"
) -> TransferRequest:
    return TransferRequest.model_validate(
        {
            "amount_usd": amount,
            "to_asset": asset,
            "source": Party(label="a", chain=source, address="0x1", known=True),
            "destination": Party(label="b", chain=dest, address="0x2", known=True),
        }
    )


def test_cheapest_and_fastest_routes_differ_when_the_rails_do() -> None:
    rails, fx = load_rails()
    routes = find_routes(rails, fx, _request(Chain.ETH_SEPOLIA, Chain.SOLANA_DEVNET))
    cheapest, fastest = routes
    assert (cheapest.id, fastest.id) == ("cheapest", "fastest")
    # Cheapest goes Ethereum -> Arc -> Solana on standard rails: 0.25 + 0.05.
    assert cheapest.cost_usd == Decimal("0.30")
    assert [step.to_chain for step in cheapest.steps] == [Chain.ARC_TESTNET, Chain.SOLANA_DEVNET]
    # Fastest is the direct fast transfer.
    assert fastest.eta_s == 20
    assert fastest.cost_usd == Decimal("0.60")
    assert cheapest.cost_usd <= fastest.cost_usd
    assert fastest.eta_s <= cheapest.eta_s


def test_same_chain_transfer_uses_the_native_rail() -> None:
    rails, fx = load_rails()
    routes = find_routes(rails, fx, _request(Chain.ARC_TESTNET, Chain.ARC_TESTNET, "20"))
    assert [step.rail for step in routes[0].steps] == ["arc-native"]


def test_euro_conversion_routes_through_arc() -> None:
    rails, fx = load_rails()
    routes = find_routes(
        rails, fx, _request(Chain.ETH_SEPOLIA, Chain.SOLANA_DEVNET, "1000", "EURC")
    )
    rails_used = [step.rail for step in routes[0].steps]
    assert "stablefx" in rails_used
    fx_step = next(step for step in routes[0].steps if step.rail == "stablefx")
    assert fx_step.cost_usd == Decimal("0.50")  # 5 basis points of 1,000


def test_speaking_money_and_time() -> None:
    assert say_money(Decimal("0.40")) == "40 cents"
    assert say_money(Decimal("0.01")) == "1 cent"
    assert say_money(Decimal("1.25")) == "1 dollar 25"
    assert say_money(Decimal("200")) == "200 dollars"
    assert say_money(Decimal("1250")) == "1,250 dollars"
    assert say_time(20) == "about 20 seconds"
    assert say_time(960) == "about 16 minutes"


async def test_intent_returns_a_plan_with_quoted_routes(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/intent", json={"text": SENTENCE})
    assert response.status_code == 200
    body = response.json()
    assert body["plan_id"] == "p_1"
    assert body["request"]["amount_usd"] == 200
    assert [route["id"] for route in body["routes"]] == ["cheapest", "fastest"]
    assert body["routes"][0]["cost_usd"] == 0.3
    assert body["preview"]["outcome"] == "pay"
    assert "Cheapest route is 30 cents" in body["spoken_quote"]
    assert body["spoken_quote"].endswith("Go ahead?")


async def test_intent_rejects_a_sentence_it_cannot_use(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/intent", json={"text": "send 20 to alex"})
    assert response.status_code == 422
    assert "Alex" in response.json()["detail"]


async def test_pay_inside_limits_executes_and_arrives(
    client: httpx.AsyncClient, ctx: AppContext
) -> None:
    plan = (await client.post("/api/intent", json={"text": SENTENCE})).json()
    response = await client.post(
        f"/api/plans/{plan['plan_id']}/execute", json={"route_id": "fastest"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "executing"
    assert body["transfer_id"] == "t_1"
    await asyncio.sleep(0.2)
    assert [event.type for event in ctx.bus.history()] == ["transfer.sent", "transfer.arrived"]
    assert await ctx.vault.spent_today() == Decimal("200")
    ledger = ctx.planner.ledger()
    assert len(ledger) == 1
    assert ledger[0].status == "arrived"
    assert ledger[0].cost_micro == 600_000

    again = await client.post(f"/api/plans/{plan['plan_id']}/execute", json={})
    assert again.status_code == 409


async def test_over_the_transfer_cap_needs_approval(
    client: httpx.AsyncClient, ctx: AppContext
) -> None:
    plan = (
        await client.post(
            "/api/intent", json={"text": "send 250 from my arc wallet to sam's arc wallet"}
        )
    ).json()
    assert plan["preview"] == {
        "outcome": "ask",
        "reason": "OVER_TRANSFER_CAP",
        "detail": "That is over your 200 dollars transfer limit.",
    }
    body = (await client.post(f"/api/plans/{plan['plan_id']}/execute", json={})).json()
    assert body["status"] == "needs_approval"
    assert body["reason"] == "OVER_TRANSFER_CAP"
    assert body["request_id"] == "1"
    requested = ctx.bus.latest("approval.requested")
    assert requested is not None
    assert requested.data["amount_usd"] == 250
    assert requested.data["reason"] == "OVER_TRANSFER_CAP"
    assert await ctx.vault.spent_today() == Decimal("0")
    assert not [event for event in ctx.bus.history() if event.type == "transfer.sent"]


async def test_a_new_payee_needs_approval(client: httpx.AsyncClient) -> None:
    plan = (
        await client.post(
            "/api/intent", json={"text": f"send 20 from my arc wallet to {NEW_ADDRESS}"}
        )
    ).json()
    assert plan["preview"]["reason"] == "PAYEE_NOT_ALLOWED"
    body = (await client.post(f"/api/plans/{plan['plan_id']}/execute", json={})).json()
    assert body["status"] == "needs_approval"
    assert body["reason"] == "PAYEE_NOT_ALLOWED"


async def test_the_daily_budget_is_enforced(client: httpx.AsyncClient) -> None:
    for _ in range(2):
        plan = (await client.post("/api/intent", json={"text": SENTENCE})).json()
        body = (await client.post(f"/api/plans/{plan['plan_id']}/execute", json={})).json()
        assert body["status"] == "executing"
    plan = (await client.post("/api/intent", json={"text": SENTENCE})).json()
    assert plan["preview"]["reason"] == "OVER_DAILY_BUDGET"
    body = (await client.post(f"/api/plans/{plan['plan_id']}/execute", json={})).json()
    assert body["status"] == "needs_approval"
    assert body["reason"] == "OVER_DAILY_BUDGET"


async def test_frozen_vault_blocks_without_asking(
    client: httpx.AsyncClient, ctx: AppContext
) -> None:
    await ctx.vault.freeze(True, via="test")
    plan = (await client.post("/api/intent", json={"text": SENTENCE})).json()
    assert plan["preview"]["outcome"] == "block"
    assert "cannot send it right now" in plan["spoken_quote"]
    body = (await client.post(f"/api/plans/{plan['plan_id']}/execute", json={})).json()
    assert body == {
        "status": "blocked",
        "reason": "FROZEN",
        "request_id": None,
        "transfer_id": None,
        "detail": "The vault is frozen.",
    }
    blocked = ctx.bus.latest("blocked")
    assert blocked is not None and blocked.data["reason"] == "FROZEN"
    assert ctx.bus.latest("approval.requested") is None


async def test_stale_stream_blocks_spending_until_it_recovers(
    client: httpx.AsyncClient, ctx: AppContext
) -> None:
    await client.post("/api/chain/stub/stream", json={"stale": True})
    plan = (await client.post("/api/intent", json={"text": SENTENCE})).json()
    body = (await client.post(f"/api/plans/{plan['plan_id']}/execute", json={})).json()
    assert body["status"] == "blocked"
    assert body["reason"] == "STREAM_STALE"

    await client.post("/api/chain/stub/stream", json={"stale": False})
    plan = (await client.post("/api/intent", json={"text": SENTENCE})).json()
    body = (await client.post(f"/api/plans/{plan['plan_id']}/execute", json={})).json()
    assert body["status"] == "executing"


async def test_market_move_sentence_resolves_a_listed_market(client: httpx.AsyncClient) -> None:
    body = (
        await client.post(
            "/api/intent", json={"text": "Move my Rockets position from Polymarket to Gemini"}
        )
    ).json()
    assert body["kind"] == "market_move"
    assert body["market_move"]["slug"] == "aec-nba-hou-den-2026-11-20"
    assert body["routes"] == []
    run = await client.post(f"/api/plans/{body['plan_id']}/execute", json={})
    assert run.status_code == 409


async def test_unknown_plan_is_a_404(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/plans/p_99/execute", json={})).status_code == 404
    assert (await client.post("/api/plans/nonsense/execute", json={})).status_code == 404
