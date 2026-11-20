# Demo mode

A scripted stage for the pitch. Money moves are simulated by the stub vault and stub transfers;
market data (Polymarket US, Gemini, a real linked Polymarket account's public positions) is live. No order
is placed and no testnet funds move. The real-rails build is unchanged when `DEMO_MODE` is off.

## Start

```
cd server && DEMO_MODE=true uv run python -m app
```

Demo mode forces, over anything in `.env`: stub vault and transfers, live market data, no orders,
its own database (`./data/demo.sqlite`), and raised limits ($500 a send, $2,000 a day, 50 calls).
`DEMO_TRADER_WALLET` (in `.env`) pins the linked account; blank picks one from the leaderboard.

Open http://127.0.0.1:8787/ (or the Vite dev server). Controls → Demo has Reset, Show the Gemini
offer, and Settle Rockets as a win.

## The script

1. Home shows live suggestions, your positions with live odds (a linked Polymarket account's
   real positions plus the scripted ones; linked ones show a similar Gemini market), and recent
   money actions.
2. "Put 30 dollars from my Ethereum wallet into the Bitcoin hourly market on Polymarket"
   → plan card (live price, contracts, fee) → Confirm → position appears with live odds. It is
   this hour's up-or-down market, so there is no waiting for a window to open; avoid the last
   few minutes of the hour, when the price is all but decided. (The first seconds of an hour,
   before the new market is listed, say so.)
   Also: "Place a bet on DOWN on the current BTC up or down 5m market" (suggestion "Bet Down on
   BTC 5m") buys $30 of Down in the five-minute window in progress, from the Ethereum wallet. Down
   costs one dollar less the best Up bid. It also works on a call.
3. Controls → Demo → Show the Gemini offer (3% rebate for 10 minutes). A banner and new
   suggestions appear on Home.
4. "Move my Bitcoin hourly position to Gemini" → Research says **against**: Gemini has no
   hourly Bitcoin market (only 5- and 15-minute windows settled on Lukka's index, and daily
   prices), so there is nothing that is the same bet to move into, rebate or not. Markets shows
   the refusal. (This is the real match, not a script.)
5. "Move my Rockets position to Gemini" → same bet on both (5 of 5 checks), the rebate makes the
   move cheaper → Move → the position now sits on Gemini.
6. Controls → Demo → Settle Rockets as a win → winnings land in the Ethereum wallet.
7. "Send 150 dollars from my Ethereum wallet to Sam's Solana wallet to cover my debt" → the
   cheapest and fastest routes with fees and times → Send → the transfer runs through its legs.

## Calling in

With `CALLS_ENABLED=true` and the AgentPhone webhook registered, call the AgentPhone number and
talk to the agent (`app/phone_line.py`). Say what you want; it reads back the quote or the
verdict, and runs it only after a yes ("no" cancels). With no request ("no" to "Anything
else?", "nothing", "I'm good", "bye", or silence after the greeting) it says "Thank you,
goodbye" and hangs up:

- "Send 150 dollars from my Ethereum wallet to Sam's Solana wallet"
- "Put 30 dollars from my Ethereum wallet into the Bitcoin hourly market" (then what you hold,
  the market's odds and the payout)
- "Add 20 dollars to my Rockets position"
- "Move my Rockets position to Gemini" (with the offer on: go, then a yes moves it) or "Move my
  Bitcoin hourly position to Gemini" (against: not offered)

Demo mode only, and any caller is treated as the owner: caller ID can be faked, so off demo
mode an inbound call is refused. A send over the cap still needs the usual approval call.

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
