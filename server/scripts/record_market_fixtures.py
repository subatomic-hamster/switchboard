"""Record real public responses from Polymarket US and Gemini into server/fixtures/markets/.

    uv run python -m scripts.record_market_fixtures

Public GETs only, no keys. Each case file holds the markets, events and order books of one
pair, trimmed to the fields the tests read (long rules text is cut). Re-running overwrites the
files with whatever the venues list now, so a case whose market has settled keeps its response
only if the venue still serves it. Commit the output; the tests never touch the network.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.config import FIXTURES_DIR

PM = "https://gateway.polymarket.us/v1"
GEM = "https://api.gemini.com/v1"
OUT = FIXTURES_DIR / "markets"

# name -> Polymarket slugs, Gemini event tickers, and the contracts whose books to keep.
CASES: dict[str, dict[str, Any]] = {
    "nba_hou_den": {
        "pm": ["aec-nba-hou-den-2026-11-20"],
        "gem": ["NBA-2611210300-HOU-DEN-M"],
        "gem_books": ["GEMI-NBA-2611210300-HOU-DEN-M-HOU"],
    },
    "laliga_atm_get": {
        "pm": ["atc-lal-get-atm-2026-11-22-atm"],
        "gem": ["LALIGA-2611222000-ATM-GET-M"],
        "gem_books": ["GEMI-LALIGA-2611222000-ATM-GET-M-ATM"],
    },
    "btc_63000": {
        "pm": ["cpc-btc-above-day-2026-11-19-63000"],
        "gem": ["BTC2611192200"],
        "keep_strikes": ["62500", "62750", "63000", "63250", "63500"],
    },
    "wx_sfo_offset": {
        "pm": ["tc-temp-sfohigh-2026-11-20-gte56lt57f", "tc-temp-sfohigh-2026-11-20-gte58lt59f"],
        "gem": ["WXHIGH-SFO-2611210459"],
    },
    "wx_lax_aligned": {
        "pm": ["tc-temp-laxhigh-2026-11-20-gte66lt67f"],
        "gem": ["WXHIGH-LA-2611210459"],
    },
    "wx_mdw_open_band": {
        "pm": ["tc-temp-mdwhigh-2026-11-20-lt64f", "tc-temp-mdwhigh-2026-11-20-gte66lt67f"],
        "gem": ["WXHIGH-CHI-2611210459"],
    },
    "nfl_no_chi": {
        "pm": ["aec-nfl-no-chi-2026-11-22"],
        "gem": [
            "NFL-2611221800-NO-CHI-M",
            "NFL-2611221800-NO-CHI-T",
            "NFL-2611221800-NO-CHI-TT",
        ],
    },
}

GEM_EVENT_KEYS = [
    "id",
    "title",
    "ticker",
    "status",
    "category",
    "template",
    "startTime",
    "expiryDate",
    "series",
    "source",
    "sourceDetails",
    "participants",
    "description",
    "volume",
]
GEM_CONTRACT_KEYS = [
    "label",
    "ticker",
    "instrumentSymbol",
    "status",
    "marketState",
    "strike",
    "source",
    "sportsMarket",
    "target",
    "sportsSide",
    "prices",
    "expiryDate",
    "quantityMinimum",
    "priceIncrement",
]
PM_KEYS = [
    "id",
    "slug",
    "question",
    "title",
    "description",
    "status",
    "active",
    "closed",
    "marketType",
    "sportsMarketType",
    "gameStartTime",
    "endDate",
    "assetPriceTerms",
    "bestBidQuote",
    "bestAskQuote",
    "minimumTradeQty",
    "feeCoefficient",
    "orderPriceMinTickSize",
]


def trim_gemini(event: dict[str, Any], keep_strikes: list[str] | None) -> dict[str, Any]:
    out = {k: event[k] for k in GEM_EVENT_KEYS if k in event}
    out["contracts"] = []
    for contract in event["contracts"]:
        if keep_strikes and (contract.get("strike") or {}).get("value") not in keep_strikes:
            continue
        row = {k: contract[k] for k in GEM_CONTRACT_KEYS if k in contract}
        text = " ".join(p.get("value", "") for p in contract["description"]["content"])
        row["description"] = text[:700]
        out["contracts"].append(row)
    return out


def trim_polymarket(market: dict[str, Any]) -> dict[str, Any]:
    out = {k: market[k] for k in PM_KEYS if k in market}
    terms = out.get("assetPriceTerms")
    if isinstance(terms, dict):
        out["assetPriceTerms"] = {k: v for k, v in terms.items() if k != "chart"}
    out["marketSides"] = [
        {
            "long": s.get("long"),
            "description": s.get("description"),
            "price": s.get("price"),
            "team": {
                k: (s.get("team") or {}).get(k)
                for k in ("name", "abbreviation", "league", "ordering")
            }
            if s.get("team")
            else None,
        }
        for s in market.get("marketSides", [])
    ]
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    with httpx.Client(timeout=30) as http:

        def get(url: str) -> Any:
            resp = http.get(url)
            resp.raise_for_status()
            return resp.json()

        for name, case in CASES.items():
            sources: list[str] = []
            markets = []
            books: dict[str, Any] = {"polymarket": {}, "gemini": {}}
            for slug in case["pm"]:
                sources.append(f"{PM}/market/slug/{slug}")
                markets.append(trim_polymarket(get(sources[-1])["market"]))
            events = []
            for ticker in case["gem"]:
                sources.append(f"{GEM}/prediction-markets/events/{ticker}")
                events.append(trim_gemini(get(sources[-1]), case.get("keep_strikes")))
            if case.get("gem_books"):
                for slug in case["pm"][:1]:
                    sources.append(f"{PM}/markets/{slug}/book")
                    raw = get(sources[-1])
                    data = raw["marketData"]
                    data["bids"], data["offers"] = data["bids"][:12], data["offers"][:12]
                    data.pop("stats", None)
                    books["polymarket"][slug] = raw
                for symbol in case["gem_books"]:
                    sources.append(f"{GEM}/book/{symbol}")
                    books["gemini"][symbol] = get(sources[-1])
            doc = {
                "recorded_at": stamp,
                "synthetic": False,
                "sources": sources,
                "polymarket": markets,
                "gemini": events,
                "books": books,
            }
            path: Path = OUT / f"{name}.json"
            path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
            sys.stdout.write(
                f"{path.relative_to(OUT.parent.parent)}  {path.stat().st_size} bytes\n"
            )


if __name__ == "__main__":
    main()
