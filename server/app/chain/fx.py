"""USDC to EURC on Arc: Circle StableFX. BLOCKED on a permissioned API key (see RAILS.md).

What is true (developers.circle.com/stablefx, checked 2026-11-20): StableFX is "a permissioned
platform designed for financial institutions". Its API needs a key from a Circle representative
(Hub account, sandbox at hub-sandbox.circle.com); a call without one returns 401. The onchain
FxEscrow (0xd68256f4D69C6BbEcB873D8588AE0Dc6B8E22E10 on Arc testnet) settles trades, but a trade
needs a maker signature the API supplies, so there is no key-free onchain path.

So this module does two things:

- `quote`: a dry-run quote from public data (the ECB mid-rate from api.frankfurter.dev, less the
  planner's StableFX fee), always labelled `source="ecb-mid"`. With a key and `dry_run=False` it
  asks the documented quote endpoint instead and labels it `source="stablefx"`.
- `execute`: raises `FxBlocked` with the exact unblock steps. Creating a trade needs the key, an
  EIP-712 signature of the quote's typed data, a Permit2 allowance, and a funding signature.
  Not written, because nothing here can be tested against the API without a key.

A swap that does not need a Circle account exists: Arc App Kit's Swap (Arc testnet only), a
JavaScript SDK. See RAILS.md. It is not wired in here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

import httpx

log = logging.getLogger("switchboard.fx")

USDC_ARC = "0x3600000000000000000000000000000000000000"
EURC_ARC = "0x89B50855Aa3bE2F677cD6303Cec089B5F319D72a"
PUBLIC_RATE_URL = "https://api.frankfurter.dev/v1/latest?base=USD&symbols=EUR"
STABLEFX_URL = "https://api-sandbox.circle.com"
MICRO = Decimal("0.000001")

UNBLOCK = (
    "StableFX is permissioned. To unblock: ask a Circle representative (sales@circle.com) for "
    "StableFX access, create a Read and write key in hub-sandbox.circle.com under Developers, set "
    "STABLEFX_API_KEY, grant the agent wallet a USDC allowance to Permit2 "
    "(0x000000000022D473030F116dDEE9F6B43aC78BA3), then implement the trade steps in "
    "developers.circle.com/stablefx/quickstarts/fx-trade-taker."
)


class FxBlocked(RuntimeError):
    """The conversion cannot run without StableFX access. The message says how to get it."""


@dataclass(frozen=True)
class FxQuote:
    from_amount: Decimal  # USDC
    to_amount: Decimal  # EURC
    rate: Decimal  # EURC per USDC
    fee_usd: Decimal
    source: Literal["ecb-mid", "stablefx"]
    quote_id: str = ""
    executable: bool = False  # True only for a tradable StableFX quote


class StableFx:
    def __init__(
        self,
        api_key: str = "",
        *,
        dry_run: bool = True,
        fee_bps: Decimal = Decimal(5),
        api_url: str = STABLEFX_URL,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._key = api_key
        self._dry_run = dry_run or not api_key
        self._fee_bps = fee_bps
        self._url = api_url.rstrip("/")
        self._http = http

    async def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        if self._http is not None:
            return await self._http.request(method, url, timeout=20, **kwargs)  # type: ignore[arg-type]
        async with httpx.AsyncClient() as client:
            return await client.request(method, url, timeout=20, **kwargs)  # type: ignore[arg-type]

    async def quote(self, amount_usd: Decimal, recipient: str = "") -> FxQuote:
        if self._dry_run:
            return await self._public_quote(amount_usd)
        response = await self._request(
            "POST",
            f"{self._url}/v1/exchange/stablefx/quotes",
            headers={"Authorization": f"Bearer {self._key}"},
            json={
                "from": {"currency": "USDC", "amount": str(amount_usd)},
                "to": {"currency": "EURC"},
                "tenor": "instant",
                "type": "tradable",
                "recipientAddress": recipient,
            },
        )
        if response.status_code in (401, 403):
            raise FxBlocked(f"StableFX rejected the key ({response.status_code}). {UNBLOCK}")
        response.raise_for_status()
        body = response.json()
        return FxQuote(
            from_amount=Decimal(body["from"]["amount"]),
            to_amount=Decimal(body["to"]["amount"]),
            rate=Decimal(body["rate"]),
            fee_usd=Decimal(body["fee"]["amount"]),
            source="stablefx",
            quote_id=str(body["id"]),
            executable=True,
        )

    async def _public_quote(self, amount_usd: Decimal) -> FxQuote:
        response = await self._request("GET", PUBLIC_RATE_URL)
        response.raise_for_status()
        mid = Decimal(str(response.json()["rates"]["EUR"]))
        fee = (amount_usd * self._fee_bps / Decimal(10_000)).quantize(MICRO, ROUND_HALF_UP)
        to_amount = ((amount_usd - fee) * mid).quantize(MICRO, ROUND_HALF_UP)
        return FxQuote(
            from_amount=amount_usd, to_amount=to_amount, rate=mid, fee_usd=fee, source="ecb-mid"
        )

    async def execute(self, quote: FxQuote) -> str:
        raise FxBlocked(UNBLOCK)
