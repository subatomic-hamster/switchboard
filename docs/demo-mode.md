# Demo mode

A scripted stage for the pitch. Money moves are simulated by the stub vault and stub transfers;
market data (Polymarket US, Gemini, a real linked Polymarket account's public positions) is live. No order
is placed and no testnet funds move. The real-rails build is unchanged when `DEMO_MODE` is off.

## Start

```
cd server && DEMO_MODE=true USE_P1_STUBS=true USE_REAL_TRANSFERS=false USE_REAL_MARKETS=true \
  DATABASE_PATH=./data/demo.sqlite POLICY_TRANSFER_CAP_USD=500 POLICY_DAILY_BUDGET_USD=2000 \
  MAX_CALLS_PER_DAY=50 uv run python -m app
```

Open http://127.0.0.1:8787/ (or the Vite dev server). Controls → Demo has Reset, Show the Gemini
offer, and Settle Rockets as a win.

## The script

1. Home shows live suggestions, your positions with live odds (a linked Polymarket account's
   real positions plus the scripted ones; linked ones show a similar Gemini market), and recent
   money actions.
2. "Put 30 dollars from my Ethereum wallet into the Bitcoin 5 minute market on Polymarket"
   → plan card (live price, contracts, fee) → Confirm → position appears with live odds.
3. Controls → Demo → Show the Gemini offer (3% rebate for 10 minutes). A banner and new
   suggestions appear on Home.
4. "Move my Bitcoin 5 minute position to Gemini" → Research says **against**: Polymarket US
   settles on CF Benchmarks BRTI, Gemini on Lukka's index; in a 5-minute window the gap between
   indexes can decide the bet, and the rebate does not pay for that. Suggests the next Polymarket
   window instead. (This is the real five-field check, not a script.)
5. "Move my Rockets position to Gemini" → same bet on both (5 of 5 checks), the rebate makes the
   move cheaper → Move → the position now sits on Gemini.
6. Controls → Demo → Settle Rockets as a win → winnings land in the Ethereum wallet.
7. "Send 150 dollars from my Ethereum wallet to Sam's Solana wallet to cover my debt" → the
   cheapest and fastest routes with fees and times → Send → the transfer runs through its legs.

## API (all local-only, all 404 unless `DEMO_MODE=true`)

`GET /api/demo/portfolio`

```
{
  "account": {"venue": "Polymarket", "name", "wallet", "profile_url", "linked": true} | null,
  "positions": [{"id", "venue": "Polymarket US" | "Polymarket" | "Gemini", "slug", "title",
                 "side", "contracts", "avg_price", "price", "value_usd", "pnl_usd",
                 "ends_at": iso | null, "status": "open" | "won" | "lost" | "moved", "url",
                 "adjacent": {"venue": "Gemini", "title", "symbol", "price"} | null}],
  "offer": {"venue": "Gemini", "headline", "rebate_pct", "expires_at": iso} | null,
  "balances": [{"place", "usd"}]
}
```

Prices are 0..1 (the live best bid you could sell at). We pretend a Polymarket account is
linked: `account` is a real public Polymarket (international) account, `DEMO_TRADER_WALLET` or
else the first weekly-PNL leader with 3+ open positions (preferring sports/BTC/ETH titles), and
its top 6 open positions (by current value, refreshed every 15 s, ids `pm-<n>`, `venue:
"Polymarket"`, price = the data API's `current_price`) join the scripted ones. Polymarket US has
no public per-user positions, so the account comes from `data-api.polymarket.com`. `adjacent` is
a *similar* Gemini market by keyword overlap (never the same bet; no five-field claim) and only
set on linked positions. `account` is null, and only scripted positions show, when the data API
is unreachable. The "Polymarket" balance row is a fixed $60 cash figure (positions not added).

`GET /api/demo/suggestions` → `{"suggestions": [{"label", "text", "why"}]}` (at most 4; `text`
is the sentence placed in the command box).

`GET /api/demo/insight?position_id=…` → `{"verdict": "go" | "against" | "neutral", "headline",
"body", "model", "source": "grok" | "fallback", "checks": [{"field", "same", "detail"}],
"alternative": str | null}`. The verdict comes from code (five-field match and the cost after
the rebate); Grok (via OpenRouter) writes the explanation.

`GET /api/demo/recent` → `{"actions": [{"ts", "kind": "send" | "receive" | "bet" | "move" |
"swap" | "win", "title", "detail", "amount_usd", "asset", "from", "to"}]}`, newest first.

`POST /api/demo/offer`, `POST /api/demo/settle`, `POST /api/demo/reset` → `{"ok": true}`.

Intent: a sentence that buys into a market returns `kind: "market_buy"` with
`market_buy: {"venue", "slug", "title", "side", "amount_usd", "price", "contracts", "fee_usd",
"from_place", "ends_at"}`; `POST /api/plans/{id}/execute` fills it (simulated) and answers
`{"status": "executing", "detail"}`.

Every demo state change publishes `demo.updated` `{"title", "detail"}` on the event bus, so the
dashboard refreshes and the feed shows a line.
