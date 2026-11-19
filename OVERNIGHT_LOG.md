# Overnight log: P2 (Voice and Surface)

Branch `p2/voice-surface`, draft PR
[subatomic-hamster/switchboard#1](https://github.com/subatomic-hamster/switchboard/pull/1).
Kept current as the work goes.

## How to run

```
# once
cp .env.example .env            # then fill in what you have; blank values mean dry-run
cd server && uv sync
cd ../web && pnpm install       # needs pnpm: npm install -g pnpm

# server (port 8787)
cd server && uv run python -m app

# dashboard (port 5173, proxies /api and /events to the server)
cd web && pnpm dev              # then open http://localhost:5173

# mock demo: replays a full day through the real routes; no chain, venue or phone
cd server && uv run python -m app.demo_mock          # add --fast to skip the pauses

# checks
cd server && uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy
cd web && pnpm test && pnpm typecheck

# regenerate web/src/api-types.ts after changing schemas or routes
cd server && uv run python -m scripts.gen_api_types
```

## Needs you

In order. Items 1 to 4 are what stands between this branch and a real phone call.

1. **Reminder you asked for: real phone calls.** None were placed. Calls stay in dry run until
   `CALLS_ENABLED=true` and items 2 to 4 are done. When you are ready, the Phase 0 proof is
   `cd server && uv run python -m scripts.phase0_call --place-real-call` (rings
   `OWNER_PHONE_NUMBER` once).
2. **ElevenLabs key.** `ELEVENLABS_API_KEY` is blank in `.env`, so nothing could be looked up
   or created. Add it, then run `uv run python -m scripts.create_agent --list` to see existing
   agent and phone-number ids.
3. **Import the Twilio number into ElevenLabs** (your question 4):
   1. In the Twilio console, copy the Account SID and Auth Token, and note the number
      (use a paid account; a trial one only calls verified numbers).
   2. In ElevenLabs, open Agents, then the Phone Numbers tab, then import a number: choose
      Twilio, give it a label, paste the number, the SID and the token.
   3. Run `uv run python -m scripts.create_agent --list` and copy the `phone_number_id` into
      `ELEVENLABS_PHONE_NUMBER_ID`.
4. **Create the agent.** Needs a public HTTPS address for the server: install `cloudflared`,
   run `cloudflared tunnel --url http://localhost:8787`, put the address in `PUBLIC_BASE_URL`,
   then `uv run python -m scripts.create_agent --apply --write-env`. This writes to your
   ElevenLabs account (one secret, seven tools, one agent), which is why I did not run it.
   Optional: set `ELEVENLABS_OPERATOR_VOICE_ID` and `ELEVENLABS_RISK_OFFICER_VOICE_ID` first
   for two distinct voices. Then set `OWNER_PHONE_NUMBER`.
5. **Rotate the key you pasted into the chat.** It is in the transcript. I did not use or
   store it.

_(More items are added below as later phases finish.)_

## Done

| Phase | Task | Commit |
|---|---|---|
| 0 | Layout, `schemas.py`, event bus and SSE, SQLModel tables, stub routers, generated types script, Vite app | `88f1b12` |
| 0 | Proof script: fetch Gemini and Polymarket US and print one matched pair (run against live data) | `c6585ac` |
| 0 | Proof script for the outbound call (dry-run; a real call needs you) | `ae425a2` |
| 1 | Intent parser: sentence to `TransferRequest` | `42297a2` |
| 1 | Planner: routes, pay / ask / block with a reason | `42297a2` |
| 1 | Agent prompt with two personas, the four voice tools, outbound call function | `ae425a2` |
| 1 | Dashboard: event feed and sentence input | `b3bb4f5` |
| 2 | Ring view: particles, flash and tone | `83970c2` |
| 2 | Voice input; quote read back before execution | `83970c2` |
| 2 | Approval hardening: amount said back, challenge word, no answer means deny | `ae425a2` |
| 2 | Approval hardening: passkey above a second threshold | `068838e` |
| – | Mock event generator `app.demo_mock` | `83970c2` |
| 3 | Forecast receipts: hash before, reveal after, Brier score | `221d8c4` |
| 3 | Markets operator: match result, move cost, bid depth, opinion with track record, override | `221d8c4`, `cd008ce` |
| 3 | Markets panel: both venues' prices side by side | `180eef4` |

## Blocked

| Item | Why | Exact step that unblocks it |
|---|---|---|
| Real outbound approval call, and the Phase 0 "reaches a real phone" proof | No ElevenLabs key, agent, imported number or owner number; and you chose to place calls later | "Needs you" items 1 to 4 |
| Creating the ElevenLabs agent | No key; needs a public HTTPS address; and creating it writes to your account | "Needs you" item 4 |
| Hearing whether two voices work on one call | Needs a real call | After item 4, place one call and listen. If the risk officer is not in a second voice, leave `ELEVENLABS_RISK_OFFICER_VOICE_ID` blank and re-run `create_agent --apply`: the prompt then has one voice announce each speaker |
| Everything behind P1: the vault, real transfers, the Stream webhook, venue clients, the real match and move | Nothing from P1 is on the remote | P1 provides objects that satisfy `server/app/ports.py` and routers for its routes; wire them in `app.main.build_context` and set `USE_P1_STUBS=false` |
| Forecast receipts and voice rules "onchain" | The plan has no contract function or route for either | P1 implements `AnchorPort.commit` (receipts) and `VaultPort.set_policy` (rules) against the contract |

## Decisions

Choices made where the plan was silent or ambiguous. Items marked **P1** need the teammate's
agreement because they touch the shared interface.

1. **Stack on this machine.** Python 3.12 through uv; Node 22.23.4 (the plan only needs Node
   for the Vite build); pnpm 12 installed globally with `npm install -g pnpm` because it was
   missing.
2. **Money on the wire.** `Decimal` everywhere in Python. JSON carries numbers, as in the
   plan's examples (`"amount_usd":250`); strings are accepted on input too. Stored in SQLite as
   integer micro-dollars.
3. **P1 – event payloads.** The plan freezes the nine event names but not their payloads. P2's
   proposal is in `server/app/schemas.py` (`TransferSentData`, `ApprovalRequestedData`, and so
   on). Each SSE message is `event: <type>`, `id: <n>`, `data: {"id","type","ts","data"}`.
4. **P1 – ports.** P2 code reaches P1 only through the protocols in `server/app/ports.py`
   (`VaultPort`, `TransferPort`, `MarketsPort`, `AnchorPort`). The stubs in `app/chain/stub.py`
   and `app/markets/stub.py` satisfy them today. The frozen HTTP routes are unchanged.
   `MarketsPort.listing()` is an addition: the side-by-side panel needs both venues' bid and
   ask, and the frozen `quote` carries only the sell bid and the buy ask.
5. **P1 – who starts the call.** The plan says "the webhook starts the call". P1's hook only
   publishes `approval.requested`; `voice.py` subscribes on the bus and starts the call, so
   neither side imports the other.
6. **New modules.** P2 modules beyond the plan's list live directly under `server/app/`
   (`config.py`, `context.py`, `ports.py`, `wallets.py`, `agent_config.py`, `passkey.py`,
   `forecast.py`, `market_brief.py`, `demo_mock.py`) and `server/scripts/`. Nothing of P2's is
   under `chain/` or `markets/` except the two clearly marked `stub.py` files.
7. **Extra event types.** Five additive types for P2 features: `call.started`, `call.ended`,
   `policy.updated`, `forecast.committed`, `forecast.revealed`.
8. **Extra routes.** All additive, under `/api`: `stats`, `freeze`, `wallets`, `venues`,
   `events/history`, `approvals…`, `passkey/…`, `desk/…`, `forecasts…`,
   `voice/markets/…`, `voice/hooks/elevenlabs`, and the stub-only `chain/stub/stream`.
9. **Stub quote formula.** The stub's `move_cost_pct` is the full taker cost on both sides
   over the sale value (11.3% for the plan's NBA example). The plan's example figure of 6.1%
   leaves out the sell-side fee; P1 owns the real formula.
10. **Kalshi, Limitless, Phantom.** Listed by `/api/venues` and in the markets panel as
    "Planned", not connected. They are not described as supported anywhere, because the README
    must carry an honest live-versus-sandboxed table. To change the wording, edit `VENUES` in
    `server/app/main.py`.
11. **Models.** Intent parser: Anthropic `claude-opus-5-5` at low effort, with an 8-second
    budget, after which the rule-based parser answers. Forecast: four families, one model
    each: `claude-opus-5-5` (Anthropic, direct), and through OpenRouter
    `openai/gpt-6.2-astra`, `google/gemini-4-argon`, `x-ai/grok-4.7`. All four answered on a
    live call. The direct OpenAI and Google keys are only used if the OpenRouter key is
    removed. The Cursor key is not a model-provider key and is not used or sent anywhere.
12. **`.env` beats the shell.** Settings read `.env` before the process environment, and the
    Anthropic base URL is pinned. A key or `ANTHROPIC_BASE_URL` exported by another tool was
    silently replacing the project's own and returning 401.
13. **Ask versus block.** Over the transfer cap, over the daily budget and a payee not on the
    allowlist are "ask" (the phone rings). A frozen vault, a stale Stream and the daily call
    cap are "block" (nothing rings).
14. **Policy numbers.** $200 per transfer, $500 a day, passkey above $1,000, 5 calls a day,
    5-minute approval expiry. All in `.env`.
15. **Approval rules beyond the plan.** Three wrong attempts deny the request. A call that
    ends with no decision denies it. Freezing by voice needs no challenge word, because
    stopping all spending is always safe; unfreezing is only on the dashboard.
16. **Where the challenge word goes.** Only to the dashboard (`GET /api/approvals`). It is
    never in an event, a call variable or a voice-tool response.
17. **Passkey.** One passkey per installation, registered on first use; a second registration
    is refused. Each assertion's challenge is bound to one approval request.
18. **Dry-run stand-in for the call.** With calls not live, the dashboard shows a labelled
    "stand in for the call" form that goes through the same checks. With calls live the route
    returns 403.
19. **Stance rule for the opinion.** "Move" only if the combined probability is above what one
    contract costs to re-buy on the destination with fees; otherwise "hold". With no outside
    information the models stay at the market price, so the opinion is usually "hold".
20. **Combining models.** Average in log-odds, then move halfway back to the market price.
21. **Forecast outcome.** `POST /api/forecasts/{id}/reveal` takes the outcome from the caller.
    Reading settlement from the venue is P1's client.
22. **Two voices.** ElevenLabs multi-voice is configured when a second voice id is set
    (`<RiskOfficer>` tags); otherwise one voice announces each speaker.
23. **Voice input and read-back** use the browser's speech recognition and synthesis. No
    ElevenLabs credit is spent by the dashboard.
