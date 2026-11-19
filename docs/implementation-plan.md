# Switchboard: 2-person implementation plan

## What is being built

**Tell it where you want your money to go, in plain speech, and a crew of agents moves it across wallets, chains, banks and prediction markets, inside limits a contract enforces, phoning you when something needs your say.**

- A user speaks or types a sentence such as "Move two hundred dollars from my Ethereum wallet to Sam's Solana wallet."
- An operator voice quotes the route, cost and time.
- Inside the user's limits, it executes. Outside them, a vault contract blocks the spend and the user's phone rings.
- On the call, an operator states the request and a risk officer argues against it. The user approves or denies by voice.
- For prediction markets, it can compare the same event on Gemini and Polymarket US, check the two contracts are truly equivalent, quote the cost of moving a position, and execute the move.

Build each phase to a finished standard before starting the next. Tests, error handling and documentation are part of each phase, not a later pass.

The product context, safety design and demo script are in `product-plan.md`.

## Roles

| Person | Owns | Needs |
|---|---|---|
| **P1, Money** | Vault contract, wallets, cross-chain transfer, QuickNode Stream, Gemini and Polymarket US clients, equivalence check, position move, bank on-ramp | Foundry, Python 3.12, the exchange API keys |
| **P2, Voice and Surface** | Server skeleton and shared schemas, intent parsing and route planner, ElevenLabs agent and phone calls, dashboard, Grok Bots, README, demo video | Python 3.12, Node (for the Vite build only), a phone, Twilio and ElevenLabs accounts |

**Working rules:**

- One public repo, `switchboard`.
- Each person works on their own branch (`p1/money`, `p2/voice-surface`) and merges to `main` at every checkpoint.
- The interface in "Frozen interface" is agreed before feature work starts. Change it only by telling the other person.
- Secrets live in `.env`, never committed. Commit a `.env.example`.
- **Real-money guard:** exchange keys are created with trading permission only, never withdrawal. The code carries a hard `MAX_ORDER_USD=2` and a `TRADING_ENABLED` switch that defaults to off.

## Accounts

| Account | Who | Note |
|---|---|---|
| Circle developer account, API key, entity secret | P1 | For wallets on `ARC-TESTNET` |
| Arc testnet USDC from `faucet.circle.com` | P1 | Fund the owner and agent wallets |
| QuickNode | P1 | Create an Arc testnet endpoint |
| Gemini API key, Trader role only | P1 | Accept the prediction-market terms by API before the first order |
| Polymarket US key from polymarket.us/developer | P1 | No test environment; every test order is real money |
| ElevenLabs | P2 | The plan must allow Agents with telephony |
| Twilio number | P2 | A trial account can only call verified numbers and plays a trial message. Use a paid account so any phone can be called |
| Grok Bot (X Premium) | P2 | Routines by webhook and by schedule |
| Tunnel (`cloudflared` or `ngrok`) for local development | P2 | Webhooks need a public HTTPS address |

## Stack

**Backend: Python and FastAPI. Frontend: Vite and React.**

- **Contracts:** Solidity with Foundry, deployed to Arc testnet (chain ID 5042002, RPC `https://rpc.testnet.arc.io`, explorer `https://explorer.testnet.arc.io`). USDC ERC-20 at `0x3600000000000000000000000000000000000000`, 6 decimals. The native gas balance uses 18 decimals; do not mix them.
- **Server:** Python 3.12, FastAPI on uvicorn. One long-running process. Exchange WebSocket connections run as asyncio background tasks started in the app's lifespan handler.
- **Schemas:** Pydantic v2 models in `server/app/schemas.py` are the frozen interface. TypeScript types for the frontend are generated from FastAPI's OpenAPI document with `openapi-typescript`, never written by hand.
- **Chain access:** web3.py. Circle developer-controlled wallets through Circle's Python SDK or its REST API, with an EOA-type wallet for the agent key.
- **Storage:** SQLite through SQLModel.
- **HTTP and streaming:** httpx for outbound requests, `websockets` for exchange feeds, `sse-starlette` for the `/events` stream.
- **Signing:** `cryptography` for Ed25519 (Polymarket US); the standard library's `hmac` and `hashlib` for HMAC-SHA384 (Gemini).
- **Web:** Vite, React, TypeScript, managed with pnpm. Server-sent events from `/events` drive the live view. In development Vite proxies `/api` and `/events` to the FastAPI server; in production FastAPI serves the built files.
- **Voice:** ElevenLabs Agents, outbound calls through Twilio (`POST /v1/convai/twilio/outbound-call`), webhook tools pointing at the server.
- **Intent parsing:** one LLM call with structured output validated by a Pydantic model. It never sees external content and has no tools.
- **Forecast opinion:** at least three models from different families, starting from the market price.
- **Grok Bot:** routines triggered by webhook and by schedule.
- **Tooling:** uv for Python dependencies and virtual environments, pytest with pytest-asyncio and respx, ruff for lint and format, mypy for types. Vitest for the frontend.

## Repo layout

```
switchboard/
  contracts/              src/Vault.sol  test/Vault.t.sol  script/Deploy.s.sol   # P1
  server/
    pyproject.toml
    app/
      main.py             app factory, routes, lifespan tasks                   # P2
      schemas.py          Pydantic models: the frozen interface                 # both
      events.py           in-process event bus and SSE endpoint                 # P2
      intent.py           sentence -> TransferRequest                           # P2
      planner.py          routes, costs, pay / ask / block                      # P2
      voice.py            outbound call, voice tool handlers                    # P2
      db.py               SQLModel tables and session                           # P2
      chain/              vault.py  transfer.py  stream.py  onramp.py           # P1
      markets/            gemini.py  polymarket.py  match.py  move.py           # P1
    tests/                                                                      # both
    fixtures/             recorded API responses for tests                      # both
  web/
    src/api-types.ts      generated from /openapi.json                          # generated
    src/                  dashboard                                             # P2
  bots/                   routines.md  exports/                                 # P2
  docs/                   plans and prompts
  README.md  .env.example
```

## Frozen interface

**Contract events and reason codes (P1 emits, P2 consumes):**

```solidity
event Spent(uint256 permitId, address agent, address payee, uint256 amount);
event ApprovalRequested(uint256 requestId, address agent, address payee, uint256 amount, uint8 reason);
event Resolved(uint256 requestId, bool approved);
event Frozen(bool frozen);
// reason: 1 OVER_TRANSFER_CAP, 2 OVER_DAILY_BUDGET, 3 PAYEE_NOT_ALLOWED, 4 FROZEN
```

**Server routes.** Each request and response body is a Pydantic model in `schemas.py`.

```jsonc
// P2 owns
POST /api/intent            {"text":"move 200 from my eth wallet to sam's sol wallet"}
                            -> {"plan_id":"p_1","request":{...},"routes":[{"id":"cheapest","cost_usd":0.40,"eta_s":20,"steps":[...]}]}
POST /api/plans/{id}/execute {"route_id":"cheapest"}
                            -> {"status":"executing|needs_approval|blocked","reason":"OVER_TRANSFER_CAP","request_id":"7"}
GET  /events                // SSE: transfer.sent, transfer.arrived, approval.requested, approval.resolved,
                            //      blocked, trade.posted, trade.filled, stream.stale, frozen

// Voice tools, called by the ElevenLabs agent (Authorization: Bearer VOICE_TOKEN)
GET  /api/voice/request/{id} -> {"amount_usd":250,"payee_label":"new address ending 4f2c","reason":"OVER_TRANSFER_CAP","spent_today_usd":40,"case_for":"...","case_against":"..."}
POST /api/voice/approve     {"request_id":"7","amount_spoken":250,"challenge":"harbor"} -> {"ok":true}
POST /api/voice/deny        {"request_id":"7"}
POST /api/voice/freeze      {}

// P1 owns
POST /api/hooks/quicknode   // Stream webhook, HMAC-verified; decodes vault events and publishes to the event bus
POST /api/chain/transfer    {"from_chain":"eth-sepolia","to_chain":"arc-testnet","to":"0x..","amount_usd":200} -> {"transfer_id":"t_1"}
GET  /api/markets/match     ?venue=polymarket&slug=<polymarket slug>
                            -> {"verdict":"match|refuse","checks":{"event":"same","strike":"n/a","direction":"same","time":"same","source":"same"},"gemini_symbol":"GEMI-...","notes":["postponement rules differ"]}
GET  /api/markets/quote     ?from=polymarket&slug=...&qty=4
                            -> {"sell":{"bid":0.45,"post_at":0.46,"taker_fee":0.09},"buy":{"ask":0.46,"taker_fee":0.07},"move_cost_pct":6.1,"bid_depth":120}
POST /api/markets/move      {"slug":"...","qty":4,"mode":"post|take"} -> {"move_id":"m_1"}
```

P1's routes live in FastAPI routers under `chain/` and `markets/`. P2 builds against stub routers that return fixture data, and P1 against a stub event bus, so neither waits on the other. Money amounts are `Decimal`, never `float`.

## Phases

Each phase ends at a checkpoint where both branches merge and the checkpoint behaviour is demonstrated end to end.

### Phase 0: foundations (together)

- Open every account in the table above.
- P2 creates the layout, `schemas.py`, stub routers, the generated-types script, and the Vite app.
- Agree the frozen interface.
- Prove each external dependency with a small standalone script before building on it:
  - P1: send testnet USDC on Arc with web3.py from a Circle wallet.
  - P1: a QuickNode Stream on Arc testnet delivering to a webhook.
  - P2: an ElevenLabs outbound call that reaches a real phone and speaks a variable.
  - P2: fetch Gemini events and Polymarket US markets and print one matched pair.

### Phase 1: the core loop

| P1 | P2 |
|---|---|
| `Vault.sol`: policy, single-use permits, pending requests, approve, deny, expire, freeze, revoke. Foundry tests for each. Deploy to Arc testnet. | Intent parser: sentence to `TransferRequest`. Planner: pay, ask, or block with a reason. |
| `chain/vault.py`: request a spend, redeem a permit, approve as the approver key. | ElevenLabs agent prompt with two personas and the four voice tools. Outbound call function. |
| Stream filter on the vault address, HMAC-verified webhook handler, publish to the event bus. | Dashboard: event feed and an input for the sentence. |

**Checkpoint A:** a sentence requesting an over-limit transfer makes the contract emit `ApprovalRequested`, the Stream fires, the phone rings, the spoken approval settles it onchain, and the feed shows it.

### Phase 2: real transfers and the picture

| P1 | P2 |
|---|---|
| Cross-chain USDC transfer with Circle CCTP between Arc testnet, an Ethereum testnet, and Solana devnet. | The ring view: vault in the centre, transfers as particles, blocked ones hit the ring with a flash and a tone. |
| USDC to EURC conversion through Circle StableFX on Arc. | Voice input on the dashboard. Route quote read back before execution. |
| Payment requests, and escrow for a recipient with no wallet, with refund after expiry. | Approval hardening: say the amount back, one-time challenge word on screen, no answer means deny, passkey above a second threshold. |

**Checkpoint B:** a spoken sentence moves testnet USDC between any two of the three chains and it shows on the ring. A payment to a new recipient sits in escrow until claimed.

### Phase 3: markets

| P1 | P2 |
|---|---|
| `markets/gemini.py` and `markets/polymarket.py`: events, order books, positions, balances. | Markets panel: both venues' prices side by side for a matched pair. |
| `markets/match.py`: the five-field comparison in code, with fixtures for matches and look-alikes. | Markets operator voice: reads the match result, move cost, and bid depth; gives a hold-or-move opinion with its track record; accepts an override. |
| `markets/move.py`: post a limit sell on Polymarket US, watch the private WebSocket for the fill, then buy on Gemini. Guarded by `MAX_ORDER_USD` and `TRADING_ENABLED`. Record each trade onchain. Queue the cash-rebalance instruction. | Forecast receipts: hash each opinion onchain before the event, reveal after, and compute a Brier score. |

**Checkpoint C:** `/api/markets/match` returns "match" for a real cross-listed game and "refuse" for a look-alike. With a small real balance, a position is sold on Polymarket US and bought on Gemini, visible on the dashboard and spoken by the operator.

### Phase 4: safety depth

| P1 | P2 |
|---|---|
| Dead man's switch: mark the stream stale after a set interval without a delivery and refuse spends until it recovers. | Anti-rubber-stamping: daily cap on calls, approval-rate counter with a warning at 100%. |
| Anomaly baseline: flag spending that departs from the agent's own history, not only fixed caps. | Rules by voice: speak a rule, hear it read back, confirm, and the policy updates onchain. |
| Adversarial test suite: replayed permits, wrong payee, agent calling `setPolicy`, injected instructions in external content. | A text box that lets anyone feed the agent hostile content, to show the worst outcome is a phone call. |

**Checkpoint D:** every adversarial test passes, stopping the Stream freezes spending, and a policy can be changed by voice.

### Phase 5: bots, bank, and polish

| P1 | P2 |
|---|---|
| Bank on-ramp in a provider's sandbox, clearly labelled as sandbox in the UI. | Grok Bot routines: missed-call escalation by webhook, daily spoken summary on a schedule, equivalence research using X search, ledger reconciliation on a schedule. Save the chat exports. |
| Route planner data: real fee and time figures for every rail. | Flight recorder: a scrub bar that replays past activity on the ring view. |
| README sections: contract addresses, how Arc is used, Stream configuration, Gemini endpoints, what is live versus sandboxed. | README sections: decision loop and safety limits, ElevenLabs products, Grok Bot role, architecture diagram. Record the demo video. |

**Checkpoint E:** a new user can clone the repo, follow the README, and run the full demo path.

## Dependencies between pieces

Build in phase order. Where a choice is needed, this is what depends on what:

- Everything depends on the vault, the Stream, and the approval call (Phase 1).
- The route planner needs at least two working rails to choose between (Phase 2).
- The position move needs the venue clients and the match check (Phase 3).
- The forecast opinion needs the receipts to have any track record.
- Grok Bot routines need the server's webhooks and ledger.

## Known constraints and how to handle them

| Constraint | Handling |
|---|---|
| Circle's nanopayments need a signature from an ordinary key, so the vault cannot pay directly | The agent's wallet holds a small balance that the vault tops up only by redeeming a permit. Document this |
| Circle's nanopayments client library (`@circle-fin/x402-batching`) is published for JavaScript | Call the underlying HTTP flow from Python, or isolate it in one small Node script the server invokes. Check for a Python client first |
| Polymarket US and Kalshi document no withdrawal endpoint | Hold a balance on both venues; show the cash leg as a queued instruction |
| Gemini API withdrawals need a pre-approved address, and approval has a waiting period | Request the address approval at the start so it is available later |
| Gemini trades settle on Gemini, not onchain | Enforce the trade cap before the order call and record each trade onchain |
| Polymarket US has no test environment | Develop against recorded fixtures and a dry-run mode; real orders stay under `MAX_ORDER_USD` |
| Each venue's listed "end date" is not the event time | Compare on `gameStartTime` / `assetPriceTerms.windowEnd` (Polymarket US) and `startTime` (Gemini) |
| Polymarket US market data is on a different host from trading | Data: `https://gateway.polymarket.us/v1/markets`. Trading: `https://api.polymarket.us/v1` |
| Exchange prices and quantities arrive as strings | Parse into `Decimal`. Never use binary floats for money |
| One voice agent may not switch voices mid-call | Use speaker tags if supported; otherwise pre-generate the risk officer's line and play it |
| Voice recognition can be defeated by cloned voices | Never authenticate by voice. Use the registered number, the challenge word, and a passkey |

## Fallbacks if a dependency does not work

| Problem | Fallback |
|---|---|
| The ElevenLabs plan does not allow phone calls | Use the ElevenLabs browser widget for the approval conversation |
| A QuickNode Stream will not deliver from Arc testnet | Poll the contract's logs with web3.py behind the same handler while the Stream is fixed. A real configured Stream is required in the finished product |
| Cross-chain transfer to Arc fails | Transfer between two other testnets and keep the vault on Arc |
| A Gemini order is rejected | Confirm the terms were accepted by API and the key is account-scoped with the Trader role |

## Reference data for the markets work

Matched pairs seen on both venues, usable as test fixtures. Live games change, so fetch current ones the same way.

| Kind | Gemini | Polymarket US | Expected result |
|---|---|---|---|
| NBA game winner | `GEMI-NBA-2611210300-HOU-DEN-M-HOU` | `aec-nba-hou-den-2026-11-20` | Match |
| La Liga three-way result | `GEMI-LALIGA-2611222000-ATM-GET-M-ATM` | `atc-lal-get-atm-2026-11-22-atm` | Match |
| Bitcoin above a strike at 5pm ET | `GEMI-BTC2611192200-HI63000` | `cpc-btc-above-day-2026-11-19-63000` | Refuse: different price index |
| City daily high temperature | `WXHIGH-SFO-...` | `tc-temp-sfohigh-...` | Refuse: temperature bands offset by one degree |
| NFL team total vs game total | n/a | `tsc-nfl-...-tt-...-45pt5` vs `...-total-45pt5` | Refuse: identical question text, different market |

Fees:

- Polymarket US taker fee: 0.095 x contracts x price x (1 - price). Makers pay nothing.
- Gemini taker fee uses the same formula with its own rate. Read the current rate from Gemini's fee page.
- Posting a limit order and waiting is a maker order and avoids the taker fee. Quote both options.
- Minimum order size is 0.01 contracts on both venues.

## Demo assets

- A small position on Polymarket US in a game that is also listed on Gemini, and a small balance on Gemini.
- A look-alike pair for the refusal.
- Wallets on each supported testnet, funded, labelled "mine" and "Sam's".
- A scripted over-limit transfer to a new address.

## Starting prompts

Paste into Cursor or Claude Code with the repo open. For a long unattended run, use the prompts in `docs/prompts/` instead.

**P1**

1. "Write `contracts/src/Vault.sol` for Solidity 0.8 with Foundry tests. It holds an ERC-20 (USDC, 6 decimals). Owner sets a policy per agent: per-transfer cap, rolling 24-hour budget, payee allowlist, approval expiry. `requestSpend(payee, amount, purposeHash)` issues a single-use permit if within policy, otherwise stores a pending request and emits `ApprovalRequested` with a reason code. `redeem(permitId)` transfers exactly that amount to that payee once. `approve`/`deny` are owner-or-approver only; `expire` is callable by anyone after the deadline and resolves as denied. `freeze`, `revokeAgent`, `setPolicy` are owner only. Emit the events in the frozen interface. Tests must cover replayed permits, wrong payee, the agent calling `setPolicy`, expiry, and freeze."
2. "Write `server/app/markets/match.py`. Given a Polymarket US market (from `https://gateway.polymarket.us/v1/markets`) and the Gemini active events list, find the candidate Gemini contract and compare five fields in code: event, strike, direction, event time, settlement source. Use `gameStartTime` and `assetPriceTerms.windowEnd` on Polymarket and `startTime`/`sourceDetails` on Gemini, not the listed end dates. Return per-field same, different, or unknown, and a verdict that refuses unless all known fields are the same and source is not different. Use Pydantic models and `Decimal`. Add pytest tests using the fixtures in `server/fixtures/`, covering every row of the reference table."
3. "Write `server/app/markets/move.py` as async code. Post a limit sell on Polymarket US (`POST https://api.polymarket.us/v1/orders`, Ed25519-signed headers using `cryptography`), subscribe to `wss://api.polymarket.us/v1/ws/private` with `websockets` for the fill, then place a limit buy on Gemini (`POST /v1/prediction-markets/order`, HMAC-SHA384 payload). Refuse if `TRADING_ENABLED` is not true or notional exceeds `MAX_ORDER_USD`. Publish `trade.posted` and `trade.filled` events. Include a dry-run mode that logs the requests it would send, and handle partial fills, an unfilled order, and a failed second leg. Test with respx."

**P2**

1. "Scaffold `server/` as a FastAPI app managed with uv: `app/main.py` with an app factory and lifespan handler, `app/schemas.py` with Pydantic v2 models for every body in the frozen interface, `app/events.py` with an in-process event bus and an SSE `/events` endpoint using sse-starlette, `app/db.py` with SQLModel on SQLite, and stub routers for `/api/chain` and `/api/markets` that return fixture data. Add pytest, ruff and mypy configuration, `.env.example`, and a script that writes `web/src/api-types.ts` from `/openapi.json` with openapi-typescript."
2. "Write `server/app/voice.py`. `start_approval_call(request_id)` calls ElevenLabs `POST /v1/convai/twilio/outbound-call` with httpx, passing dynamic variables for amount, payee label, reason, and spend today. Implement the four voice tool routes with bearer-token auth. `approve` succeeds only if `amount_spoken` equals the pending amount and `challenge` equals the one-time word stored for that request. Include a dry-run mode that places no call. Also draft the agent system prompt: an operator who states the request, a risk officer who always gives one objection, then ask the owner to say the amount and the word on their screen. Test with respx."
3. "Build the dashboard in `web/` with Vite, React and TypeScript, using the generated `api-types.ts`. Main view: an SVG ring with the vault at the centre and destinations around it. Subscribe to `/events` with EventSource. A `transfer.sent` event animates a particle out through the ring; a `blocked` or `approval.requested` event makes the particle stop at the ring, flash it, and play a short tone. Side panel: event feed with reason codes, budget remaining, approval rate, and a markets panel showing both venues' prices for one matched pair. Configure the Vite dev server to proxy `/api` and `/events` to FastAPI."

## Definition of done

- Public GitHub repo with setup instructions that work from a clean clone.
- Contract tests, server tests and frontend tests pass. Ruff and mypy are clean.
- README with architecture, decision loop, safety limits, contract addresses, Stream configuration (network, dataset, filter, destination), ElevenLabs products used, Grok Bot role and chat export, Gemini endpoints, and a table of what is live versus sandboxed. It states that the forecast is not financial advice.
- A 2 to 3 minute demo video with call audio showing: a spoken transfer, the approval call, a market match and a refusal, and a position move.
