"""Mock demo: drive a running server through a realistic day, with no chain, venue or phone.

    uv run python -m app.demo_mock            # paced for watching (about a minute)
    uv run python -m app.demo_mock --fast     # no pauses
    uv run python -m app.demo_mock --base-url http://127.0.0.1:8787

Start the server first (`uv run python -m app`) with USE_P1_STUBS=true and calls in dry run,
and open the dashboard. Every step goes through the real routes, so what appears on `/events`
is produced by the same code a real request runs: the intent parser, the planner, the stub
vault, the approval rules. Where a phone call would happen, the script stands in for the owner
through the dry-run "simulate the call" route, reading the challenge word the way the owner
would read it off the dashboard.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

import httpx

from app.config import get_settings

NEW_ADDRESS = "0x9c1e8a4f2b7d6c5a3e1f0b9d8c7a6e5f4d3c4f2c"
NBA = "aec-nba-hou-den-2026-11-20"
BTC = "cpc-btc-above-day-2026-11-19-63000"


class Demo:
    def __init__(self, client: httpx.AsyncClient, pace: float) -> None:
        self._client = client
        self._pace = pace

    async def pause(self, seconds: float) -> None:
        if self._pace > 0:
            await asyncio.sleep(seconds * self._pace)

    def say(self, text: str) -> None:
        print(f"\n== {text}")

    async def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self._client.post(path, json=body or {})
        if response.status_code >= 400:
            print(f"   {path} -> {response.status_code} {response.text[:160]}")
            return {}
        return dict(response.json())

    async def get(self, path: str, **params: Any) -> dict[str, Any]:
        response = await self._client.get(path, params=params)
        if response.status_code >= 400:
            print(f"   {path} -> {response.status_code} {response.text[:160]}")
            return {}
        return dict(response.json())

    async def sentence(self, text: str, route: str = "cheapest") -> dict[str, Any]:
        print(f'   "{text}"')
        plan = await self.post("/api/intent", {"text": text})
        if not plan:
            return {}
        print(f"   operator: {plan.get('spoken_quote')}")
        await self.pause(2)
        if plan.get("kind") != "transfer":
            return plan
        result = await self.post(f"/api/plans/{plan['plan_id']}/execute", {"route_id": route})
        reason = f" ({result['reason']})" if result.get("reason") else ""
        print(f"   -> {result.get('status')}{reason}")
        return result

    async def pending(self, request_id: str) -> dict[str, Any]:
        approvals = (await self.get("/api/approvals")).get("approvals", [])
        return next((a for a in approvals if a["request_id"] == request_id), {})

    async def run(self) -> None:
        stats = await self.get("/api/stats")
        if not stats:
            raise SystemExit("The server did not answer. Start it with `uv run python -m app`.")
        if stats["calls_live"]:
            raise SystemExit(
                "Calls are live (CALLS_ENABLED=true). The mock demo only runs in dry run, "
                "so that it can never ring a phone."
            )
        if stats["frozen"]:
            await self.post("/api/freeze", {"frozen": False})
        if stats["stream_stale"]:
            await self.post("/api/chain/stub/stream", {"stale": False})

        self.say("1. A transfer inside the limits: it just goes.")
        await self.sentence(
            "Move one hundred fifty dollars from my Ethereum wallet to Sam's Solana wallet",
            route="fastest",
        )
        await self.pause(6)

        self.say("2. Over the per-transfer limit: the vault holds it and the phone would ring.")
        result = await self.sentence("Send 250 from my Arc wallet to Sam's Arc wallet")
        if result.get("request_id"):
            await self.pause(5)
            approval = await self.pending(result["request_id"])
            print(f"   risk officer: {approval.get('case_against')}")
            print("   owner says only 'yes' -> not enough; then the wrong amount:")
            wrong = await self.post(
                f"/api/approvals/{result['request_id']}/simulate-voice",
                {"amount_spoken": 200, "challenge": approval.get("challenge", "")},
            )
            print(f"   -> {wrong.get('error')}")
            await self.pause(3)
            print("   owner says the amount and the word on the screen:")
            done = await self.post(
                f"/api/approvals/{result['request_id']}/simulate-voice",
                {"amount_spoken": 250, "challenge": approval.get("challenge", "")},
            )
            print(f"   -> {done.get('status')}")
        await self.pause(5)

        self.say("3. A destination never paid before: held, and this time the owner says no.")
        result = await self.sentence(f"Send 20 from my Arc wallet to {NEW_ADDRESS}")
        if result.get("request_id"):
            await self.pause(5)
            await self.post(f"/api/approvals/{result['request_id']}/deny")
            print("   -> denied")
        await self.pause(4)

        self.say("4. Markets: a real match, then a look-alike that is refused.")
        match = await self.get("/api/markets/match", venue="polymarket", slug=NBA)
        print(f"   {NBA}: {match.get('verdict')} {match.get('notes')}")
        quote = await self.get("/api/markets/quote", **{"from": "polymarket"}, slug=NBA, qty=4)
        print(
            f"   moving 4 contracts costs {quote.get('move_cost_pct')}% "
            f"(bid depth {quote.get('bid_depth')})"
        )
        await self.pause(3)
        move = await self.post("/api/markets/move", {"slug": NBA, "qty": 4, "mode": "post"})
        print(f"   dry-run move {move.get('move_id')}: no order was placed")
        await self.pause(4)
        refuse = await self.get("/api/markets/match", venue="polymarket", slug=BTC)
        print(f"   {BTC}: {refuse.get('verdict')} {refuse.get('notes')}")
        await self.pause(4)

        self.say("5. Kill the stream: the next transfer is refused until it comes back.")
        await self.post("/api/chain/stub/stream", {"stale": True})
        await self.pause(2)
        await self.sentence("Send 40 from my Arc wallet to Sam's Arc wallet")
        await self.pause(4)
        await self.post("/api/chain/stub/stream", {"stale": False})
        await self.pause(2)

        self.say("6. Freeze: the kill switch. Then unfreeze and carry on.")
        await self.post("/api/freeze", {"frozen": True})
        await self.pause(2)
        await self.sentence("Send 40 from my Arc wallet to Sam's Arc wallet")
        await self.pause(4)
        await self.post("/api/freeze", {"frozen": False})
        await self.pause(2)
        await self.sentence("Send 40 from my Arc wallet to Sam's Arc wallet")
        await self.pause(3)

        stats = await self.get("/api/stats")
        approvals = stats.get("approvals", {})
        print(
            f"\nDone. Spent today: ${stats.get('spent_today_usd')}. "
            f"Approved {approvals.get('approved')} of {approvals.get('total')} requests."
        )


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay a realistic sequence on /events.")
    parser.add_argument("--base-url", default=f"http://127.0.0.1:{get_settings().port}")
    parser.add_argument("--fast", action="store_true", help="no pauses between steps")
    args = parser.parse_args(argv)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=30) as client:
        try:
            await Demo(client, pace=0 if args.fast else 1).run()
        except httpx.ConnectError:
            print(
                f"Cannot reach {args.base_url}. Start the server with `uv run python -m app`.",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
