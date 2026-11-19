"""Phase 0 proof: fetch one market from Polymarket US and Gemini and print the matched pair.

    uv run python -m scripts.phase0_markets                 # the plan's NBA example
    uv run python -m scripts.phase0_markets <polymarket-slug>
    uv run python -m scripts.phase0_markets --save          # also record the responses

Read-only and keyless: both venues publish market data without authentication. This is a
standalone proof that the two public APIs can be joined on a game. It is deliberately naive
(team codes and start time only). The real five-field equivalence check is P1's
`markets/match.py`.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from typing import Any

import httpx

from app.config import FIXTURES_DIR

POLYMARKET = "https://gateway.polymarket.us/v1"
GEMINI = "https://api.gemini.com/v1/prediction-markets"
DEFAULT_SLUG = "aec-nba-hou-den-2026-11-20"
SAVE_PATH = FIXTURES_DIR / "phase0_pair.json"


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fetch_polymarket(client: httpx.Client, slug: str) -> tuple[dict[str, Any], dict[str, Any]]:
    listing = client.get(f"{POLYMARKET}/markets", params={"slug": slug}).raise_for_status().json()
    markets = [m for m in listing.get("markets", []) if m.get("slug") == slug]
    if not markets:
        raise SystemExit(f"Polymarket US has no market with slug {slug!r}")
    bbo = client.get(f"{POLYMARKET}/markets/{slug}/bbo").raise_for_status().json()
    return markets[0], bbo.get("marketData", {})


def team_codes(slug: str) -> tuple[str, str] | None:
    """`aec-nba-hou-den-2026-11-20` -> ("HOU", "DEN")."""
    match = re.match(r"^[a-z]+-[a-z0-9]+-([a-z]{2,4})-([a-z]{2,4})-\d{4}-\d{2}-\d{2}", slug)
    return (match.group(1).upper(), match.group(2).upper()) if match else None


def find_gemini_event(
    client: httpx.Client, market: dict[str, Any], codes: tuple[str, str]
) -> dict[str, Any] | None:
    """Search Gemini by each team name and keep the event whose ticker carries both codes."""
    names = [side.get("description", "") for side in market.get("marketSides", [])]
    for name in filter(None, names):
        response = client.get(
            f"{GEMINI}/events", params={"status": "active", "limit": 20, "search": name}
        )
        response.raise_for_status()
        for event in response.json().get("data", []):
            parts = str(event.get("ticker", "")).split("-")
            if set(codes) <= set(parts) and parts[-1] == "M":
                return dict(event)
    return None


def gemini_contract_for(event: dict[str, Any], code: str) -> dict[str, Any] | None:
    for contract in event.get("contracts", []):
        if str(contract.get("instrumentSymbol", "")).endswith(f"-{code}"):
            return dict(contract)
    return None


def main() -> int:
    args = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    slug = args[0] if args else DEFAULT_SLUG
    with httpx.Client(timeout=30, headers={"User-Agent": "switchboard-phase0"}) as client:
        market, bbo = fetch_polymarket(client, slug)
        codes = team_codes(slug)
        if codes is None:
            raise SystemExit(f"cannot read team codes from slug {slug!r}")
        event = find_gemini_event(client, market, codes)

    print("Polymarket US")
    print(f"  slug        {market['slug']}")
    print(f"  question    {market.get('question')}")
    print(
        f"  game start  {market.get('gameStartTime')}   (listed end date: {market.get('endDate')})"
    )
    best_bid = bbo.get("bestBid", {}).get("value")
    best_ask = bbo.get("bestAsk", {}).get("value")
    print(f"  best bid    {best_bid}   best ask {best_ask}")
    print(f"  taker fee coefficient  {market.get('feeCoefficient')}")

    if event is None:
        print("\nGemini: no active event found for the same two teams.")
        return 1

    long_side = next(
        (side for side in market.get("marketSides", []) if side.get("long")),
        market["marketSides"][0],
    )
    long_code = str(long_side.get("team", {}).get("abbreviation") or codes[0]).upper()
    contract = gemini_contract_for(event, long_code) or gemini_contract_for(event, codes[0])
    print("\nGemini")
    print(f"  event       {event.get('ticker')}  ({event.get('title')})")
    print(f"  start time  {event.get('startTime')}   (listed expiry: {event.get('expiryDate')})")
    if contract is not None:
        prices = contract.get("prices", {})
        print(f"  contract    {contract.get('instrumentSymbol')}  ({contract.get('label')})")
        print(f"  best bid    {prices.get('bestBid')}   best ask {prices.get('bestAsk')}")

    same_time = (
        bool(market.get("gameStartTime"))
        and bool(event.get("startTime"))
        and parse_time(market["gameStartTime"]) == parse_time(event["startTime"])
    )
    print(f"\nSame two teams: yes.  Same start time: {'yes' if same_time else 'NO'}.")
    print("Naive result:", "matched pair" if same_time else "not the same game")

    if "--save" in sys.argv:
        keep = ("ticker", "title", "startTime", "expiryDate", "category", "source", "sourceDetails")
        record = {
            "_note": "Recorded by scripts/phase0_markets.py from the venues' public endpoints.",
            "polymarket": {"market": market, "bbo": bbo},
            "gemini": {
                "event": {key: event.get(key) for key in keep},
                "contract": contract,
            },
        }
        SAVE_PATH.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print(f"saved {SAVE_PATH.name}")
    return 0 if same_time else 1


if __name__ == "__main__":
    raise SystemExit(main())
