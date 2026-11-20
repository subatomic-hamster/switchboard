# Demo runbook

The five beats of the demo script in `product-plan.md`, with what to say, what should happen,
and what to check beforehand. Everything here runs on testnets with burner wallets.

## Before you go on

| Check | How |
|---|---|
| Contracts built | `cd contracts && forge build` |
| Server on the real vault | `cd server && uv run python -m app` with `LEDGER_ADDRESS`, `ESCROW_ADDRESS`, `USE_REAL_TRANSFERS=true`, `USE_REAL_MARKETS=true` in `.env` |
| Steady tunnel | A named Cloudflare tunnel, or `cloudflared tunnel --protocol http2 --url http://127.0.0.1:8787`. Quick tunnels over QUIC dropped every few minutes on hotel-grade Wi-Fi |
| Stream points at it | QuickNode → Streams → `switchboard-vault-arc` → destination `<tunnel>/api/hooks/quicknode`, active |
| `PUBLIC_BASE_URL` = the tunnel | Claim links are built from it |
| Header shows **Live** | Not "Paused". Paused means no Stream delivery for 90 s, and every spend is refused |
| Budget left | Home shows "Left today". The agent has $10 per rolling 24 h and $5 per send. Each beat below spends at most $2 |
| Agent float | The agent wallet pays cross-chain fees (about $0.23 to Solana) and funds escrow links. Keep $5+ USDC on it |
| Phone | `CALLS_ENABLED=true` and the AgentPhone settings if the call should really ring; otherwise the dashboard stands in for the phone |

## The beats

**1. Problem.** No action. One line: money sits in five places, and an agent that could move it
cannot be trusted with the keys.

**2. Simple move.** Tap the mic or the first example: *"Send 2 dollars from my Arc wallet to
Sam's Solana wallet."*
- Quote reads back: one route, Circle CCTP, about 30 s, within limits.
- Activity: "$2 leaving Arc for Solana" → "Circle confirmed the burn; minting on Solana" →
  "$2 arrived on Solana", each with an explorer link.
- The ring: a particle leaves the vault for Solana; a dashed ring pulses until it lands.

**3. The call.** *"Send 6 dollars from my Arc wallet to Sam's Arc wallet."* (over the $5 cap)
- Quote says it needs your approval. Execute: the vault records a request onchain, the Stream
  delivers it, and the call starts.
- Approve by saying the amount and the word shown on screen. Sam is paid in the same
  transaction.

**4. Markets.** Markets tab, or say *"Move my Rockets position from Polymarket to Gemini."*
- "Same bet on both, 5 of 5 checks match", the two prices, the cost of selling now versus
  posting, and the opinion ("I'd hold" with the models' numbers).
- Move: both orders are signed and logged, never sent ("Practice: no order placed"). The trade
  record is hashed onto the Ledger.
- To show a refusal, pick the San Francisco temperature bet: "Not the same bet", because the
  bands differ (Polymarket US 56 to 57 °F, Gemini 57 to 58 °F). Live listings change daily.

**5. Safety.** Either:
- **Freeze:** the Freeze button in the header. Then repeat beat 2: blocked, `FROZEN`.
  Unfreeze afterwards.
- **Kill the stream:** pause the Stream in QuickNode. Within 90 s the header turns Paused and the
  next send is blocked, `STREAM_STALE`. Resume ("Continue where it left off"); it clears in
  seconds.

**Extra, if there is time:** "No wallet? Send a link" on Home. $1, copy the link, open it on a
phone, paste an Arc address, Claim. The ring lights "New address".

## If something goes wrong

| You see | It means | Do |
|---|---|---|
| Header "Paused" | No Stream delivery for 90 s | Check the tunnel; resume the Stream in QuickNode |
| Quote says it needs approval for a small amount | Daily budget used up (`OVER_DAILY_BUDGET`) | Approve it on the call, or raise the budget in Controls |
| "Sending from your Ethereum wallet isn't live yet" | Only Arc-origin transfers move real money | Send from the Arc wallet |
| Claim page "This link is not valid" | Wrong or cut-off link | Copy the whole link, including the part after `#` |
