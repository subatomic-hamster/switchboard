"""StableFX is blocked on a permissioned key: the dry-run quote works, execution says why not."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import respx

from app.chain.fx import PUBLIC_RATE_URL, STABLEFX_URL, FxBlocked, StableFx

QUOTES = f"{STABLEFX_URL}/v1/exchange/stablefx/quotes"


@respx.mock
async def test_dry_run_quote_uses_the_public_mid_rate_less_the_fee() -> None:
    respx.get(PUBLIC_RATE_URL).mock(return_value=httpx.Response(200, json={"rates": {"EUR": 0.9}}))
    quote = await StableFx().quote(Decimal(100))
    assert quote.source == "ecb-mid" and not quote.executable
    assert quote.fee_usd == Decimal("0.05")  # 5 bps
    assert quote.to_amount == Decimal("89.955")  # (100 - 0.05) * 0.9


@respx.mock
async def test_a_key_without_dry_run_asks_the_stablefx_quote_endpoint() -> None:
    route = respx.post(QUOTES).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "q1",
                "rate": "0.9150",
                "from": {"currency": "USDC", "amount": "1000.00"},
                "to": {"currency": "EURC", "amount": "915.00"},
                "fee": {"currency": "USDC", "amount": "1.50"},
            },
        )
    )
    quote = await StableFx("key", dry_run=False).quote(Decimal(1000), "0xabc")
    assert route.calls[0].request.headers["authorization"] == "Bearer key"
    assert (quote.source, quote.quote_id, quote.executable) == ("stablefx", "q1", True)
    assert quote.to_amount == Decimal("915.00")


@respx.mock
async def test_without_a_key_dry_run_is_forced() -> None:
    respx.get(PUBLIC_RATE_URL).mock(return_value=httpx.Response(200, json={"rates": {"EUR": 1}}))
    assert (await StableFx("", dry_run=False).quote(Decimal(10))).source == "ecb-mid"


@respx.mock
async def test_a_rejected_key_says_how_to_unblock() -> None:
    respx.post(QUOTES).mock(return_value=httpx.Response(401))
    with pytest.raises(FxBlocked, match=r"sales@circle\.com"):
        await StableFx("bad", dry_run=False).quote(Decimal(10))


async def test_execution_is_blocked() -> None:
    fx = StableFx()
    with respx.mock:
        respx.get(PUBLIC_RATE_URL).mock(
            return_value=httpx.Response(200, json={"rates": {"EUR": 1}})
        )
        quote = await fx.quote(Decimal(10))
    with pytest.raises(FxBlocked, match="STABLEFX_API_KEY"):
        await fx.execute(quote)
