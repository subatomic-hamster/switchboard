# Switchboard

Tell it where you want your money to go, in plain speech, and a crew of agents moves it across
wallets, chains, banks and prediction markets, inside limits a contract enforces, phoning you
when something needs your say.

Built for the University Blockchain Conference 2026 Hackathon.

## What works today

This branch is the voice and surface half (P2). The money half (P1: the vault contract, real
transfers, the QuickNode Stream, the venue clients) is developed separately and is stubbed
here, so everything below runs with no chain, no venue account and no phone.

| Piece | State on this branch |
|---|---|
| Sentence to transfer request | Real. One LLM call (Anthropic), cross-checked by a rule-based parser; rules alone without a key |
| Route planner, pay / ask / block | Real logic. Fee and time figures are estimates in `server/fixtures/rails.json` until P1 measures them |
| Vault policy, permits, approvals, freeze | **Stub** (`server/app/chain/stub.py`), same rules as the contract, in memory |
| Transfers | **Stub**: pretends to send, then to arrive. No funds move |
| Approval call | Real code, **dry run** by default: the request that would be sent is logged and the dashboard stands in for the phone. The phone call goes through **AgentPhone** (`CALL_PROVIDER=agentphone`); an ElevenLabs agent through Twilio is the alternative. **No real call has been placed from this branch yet**, so the call itself is untested end to end |
| Dashboard voice (microphone, quote read back) | Real: ElevenLabs speech-to-text and text-to-speech when `ELEVENLABS_API_KEY` is set, checked with one live round trip; the browser's own speech otherwise |
| Approval rules (amount, word, passkey, expiry, call cap) | Real |
| Prices and equivalence check | **Recorded data** (`server/fixtures/markets.json`), a snapshot of pairs on Gemini and Polymarket US. `scripts/phase0_markets.py` reads the live public APIs |
| Position move | **Dry run only.** No order is ever placed from this branch. `TRADING_ENABLED` is false and the stub ignores it anyway |
| Forecast opinion | Real: four model families on live calls, when the keys are set |
| Forecast receipts | Real hashing, reveal and Brier scoring. The onchain commitment is a **stub** |
| Grok Bot routines | Server routes are real. The routines are set up by hand: [bots/routines.md](bots/routines.md) |
| Kalshi, Limitless, Phantom | **Planned.** Listed in the markets panel as not connected. Nothing is built for them |

## Quick start

Needs [uv](https://docs.astral.sh/uv/) (it installs Python 3.12), Node 20 or later, and pnpm
(`npm install -g pnpm`).

```
git clone https://github.com/subatomic-hamster/switchboard.git
cd switchboard
cp .env.example .env

cd server
uv sync
uv run python -m app            # the server, on http://127.0.0.1:8787
```

In a second terminal:

```
cd switchboard/web
pnpm install
pnpm dev                        # the dashboard, on http://localhost:5173
```

In a third, with the dashboard open:

```
cd switchboard/server
uv run python -m app.demo_mock  # replays a full day; add --fast to skip the pauses
```

With `.env` copied unchanged, every integration is in dry-run or fixture mode and nothing
needs a key. To build the dashboard into the server instead of running Vite:
`cd web && pnpm build`, then open http://127.0.0.1:8787.

### The demo path by hand

1. Type or say "Move two hundred dollars from my Ethereum wallet to Sam's Solana wallet". The
   operator quotes the cheapest and the fastest route. Send it: a particle passes through the
   ring.
2. "Send 250 from my Arc wallet to Sam's Arc wallet". It is over the limit: the particle stops
   at the ring, and "Needs your say" shows the request, both sides of the argument and a
   one-time word. Approve by giving the amount and the word, or deny.
3. "Move my Rockets position from Polymarket to Gemini". The markets panel shows both venues'
   prices, the match result and the cost. Pick the Bitcoin market to see a refusal.
4. Safety controls: kill the stream or freeze, then try a transfer. Change a rule by voice.
   Paste hostile text into "Try to trick it".
5. Flight recorder: replay the day on the ring.

### Checks

```
cd server && uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy
cd web && pnpm test && pnpm typecheck
```

`web/src/api-types.ts` is generated from the server's OpenAPI document and never edited by
hand. After changing `server/app/schemas.py` or a route:
`cd server && uv run python -m scripts.gen_api_types`.

## Architecture

```mermaid
flowchart LR
  owner([Owner]) -- speaks or types --> dash[Dashboard<br/>Vite + React]
  dash -- "/api" --> server
  server -- "/events (SSE)" --> dash

  subgraph server[Server: FastAPI, one process]
    intent[intent.py<br/>sentence to request]
    planner[planner.py<br/>routes, pay / ask / block]
    bus((event bus))
    voice[voice.py<br/>approval rules, call]
    desk[market_brief.py<br/>forecast.py]
    intent --> planner
    planner --> bus
    bus --> voice
  end

  planner -- request spend, redeem --> vault[(Vault contract<br/>on Arc)]
  vault -- events --> stream[QuickNode Stream]
  stream -- webhook --> bus
  voice -- approve / deny as approver --> vault
  voice -- outbound call --> phone[AgentPhone<br/>webhook mode]
  phone -- rings --> owner
  phone -- each thing the owner says, signed --> voice
  dash -- microphone audio, lines to read --> eleven[ElevenLabs<br/>speech-to-text, text-to-speech]
  desk -- match, quote, move --> venues[Gemini<br/>Polymarket US]
  bots[Grok Bot routines] -- read, leave notes --> server
  server -- missed-call webhook --> bots
```

On this branch the vault, the Stream and the venues are the stubs described above. P2's code
reaches P1's only through the four protocols in `server/app/ports.py`, so the real modules drop
in without changes on this side.

| Path | What it is |
|---|---|
| `server/app/schemas.py` | Pydantic models for every body and event: the shared interface |
| `server/app/events.py` | In-process event bus, `/events` (SSE) with replay, stored history |
| `server/app/intent.py`, `planner.py` | Sentence to request; routes, quote, decision, execution |
| `server/app/voice.py`, `call_script.py`, `passkey.py` | The approval call and its rules, the scripted conversation for the AgentPhone call, the passkey check |
| `server/app/agent_config.py`, `speech.py` | The ElevenLabs agent's prompt and tools (Twilio path and browser widget); ElevenLabs speech for the dashboard |
| `server/app/market_brief.py`, `forecast.py` | The markets operator, the multi-model forecast and its receipts |
| `server/app/rules.py`, `hostile.py`, `bots.py` | Rules by voice, the hostile-content box, Grok Bot routes |
| `server/app/chain/stub.py`, `markets/stub.py` | **Stubs** for P1's modules |
| `server/scripts/` | `gen_api_types`, `agentphone_setup`, `create_agent`, `widget_page`, and the two Phase 0 proofs |
| `web/src/` | The dashboard |
| `bots/` | Routine definitions and chat exports |
| `docs/` | The product plan, the implementation plan, and the unattended-run prompts |

## The decision loop

1. **The owner's sentence is parsed** into an amount, a source and a destination. One LLM
   call with structured output, no tools, and it sees only that sentence. Its answer is checked
   against a rule-based parse of the same sentence: the amount must match, and a destination
   address must appear in the sentence character for character. The model cannot invent
   either.
2. **The planner fixes the route** before anything from outside is read. It finds the cheapest
   and the fastest route over the available rails and quotes both, with cost and time.
3. **The planner asks the vault.** It never signs for the vault; it requests a spend. Every
   request resolves to one of three outcomes, each with a reason code:

   | Outcome | When | What happens |
   |---|---|---|
   | **pay** | Within the per-transfer limit and the daily budget, to a payee on the allowlist | The vault issues a single-use permit; the transfer starts |
   | **ask** | `OVER_TRANSFER_CAP`, `OVER_DAILY_BUDGET` or `PAYEE_NOT_ALLOWED` | The vault holds a pending request; the owner's phone rings |
   | **block** | `FROZEN`, `STREAM_STALE`, `CALL_CAP_REACHED` | Nothing is requested and nothing rings |

4. **The owner decides on the call**, and the answer settles the request onchain.
5. **Everything is published once** on the event bus: to the dashboard, to the stored log
   (which the flight recorder replays), and to the daily summary.

### Safety limits

| Limit | Default | Where it is enforced |
|---|---|---|
| Per-transfer limit | $200 | Vault contract |
| Rolling 24-hour budget | $500 | Vault contract |
| Payee allowlist | The wallet book | Vault contract |
| Freeze (kill switch) | Off | Vault contract; by voice, or on the dashboard |
| Stale-Stream pause | Spending stops while the Stream is quiet | Server, before the vault is asked |
| Approval expiry | 5 minutes, then denied | Vault contract and server |
| Approval calls per day | 5, then requests are denied without a call | Server |
| Passkey threshold | Above $1,000, a passkey as well as the call | Server |
| Order size | `MAX_ORDER_USD` = $2, and a hard ceiling of $2 in code | Before any order call |
| Trading switch | `TRADING_ENABLED` = false | Before any order call |

Defaults are in `.env.example`. The owner can change the first three by voice (below); the
agent's key can never change them.

Circle's nanopayments need a signature from an ordinary key, so the vault cannot pay those
directly. The agent's wallet holds a small balance that the vault tops up only by redeeming a
permit. The worst case is one top-up misspent, bounded by the per-transfer limit.

### The approval call

When the vault holds a request, the server stores it with a one-time word, shows the word on
the owner's dashboard, and calls the owner's registered number. The operator states the
request and the case for it, and the risk officer gives one objection. To approve, the
owner must:

- say the **amount** back, exactly;
- say the **word on their screen**. The word is never sent to the voice agent, in a call
  variable, a tool response or an event, so someone who only hears the call cannot learn it;
- above the passkey threshold, also confirm with a **passkey** on the dashboard. The passkey
  challenge is bound to that one request.

"Yes" is not an approval. Three wrong attempts deny the request. No answer, a call that ends
without a decision, or the expiry passing, denies it. Saying "freeze" stops all spending and
needs no word, because stopping is always safe.

Nothing authenticates by voice. A cloned voice gains nothing: calls go out only to the
registered number, and approval needs the word from the owner's screen.

**Which path the phone call uses.** `CALL_PROVIDER` picks who places it:

| `CALL_PROVIDER` | What happens |
|---|---|
| `dry_run` (default) | No call. The request that would be sent is logged, and the dashboard stands in for the phone through the same checks |
| `agentphone` | **The phone call.** `POST https://api.agentphone.ai/v1/calls` in webhook mode. AgentPhone turns the owner's speech into text and posts each turn to `POST /api/voice/hooks/agentphone`; this server decides what is said back. The conversation is a fixed script in `server/app/call_script.py`: no language model is on the call, so nothing said on it can talk the line into anything |
| `elevenlabs_twilio` | An ElevenLabs agent called through Twilio (`POST /v1/convai/twilio/outbound-call`), using the voice tools on this server. Kept and tested with mocked HTTP; not what the demo uses, because Twilio is a paid line |

All three end in the same `approve`, `deny` and `freeze` code, so the rules above do not
depend on the provider. Whatever `CALL_PROVIDER` says, no call is placed unless
`CALLS_ENABLED=true` and everything the provider needs is set; the server logs what is
missing at start-up.

The AgentPhone webhook accepts a delivery only with a valid `X-Webhook-Signature` (HMAC-SHA256
of the timestamp and the raw body) no more than five minutes old, and only for a call this
server placed. Someone ringing the number, even from the owner's caller id, is told nothing
can be approved and the line hangs up. A call that has not reached a decision after twelve
turns is denied.

Known limits of the AgentPhone call:

- **One voice.** AgentPhone gives one voice per call, so the risk officer is announced by
  name ("Now the risk officer") in the same voice as the operator. The two-voice version
  exists only on the ElevenLabs agent.
- **Approvals only.** Rules by voice and the markets operator are tools of the ElevenLabs
  agent. On an AgentPhone call they are not available; a rule is confirmed on the dashboard.
- **Speech-to-text decides what was heard.** The amount is read from digits or words ("two
  fifty" is asked again, because it has two readings). The word is matched against the
  public list of possible words, so "harbour" is read as "harbor"; whether it is the right
  word is still checked only against the one stored for that request.
- **If the owner does not pick up**, the opening line (amount, payee, reason) is what a
  voicemail would record. The word is never spoken by the server.

The dashboard stays on the machine running the server. AgentPhone, ElevenLabs and the bots
reach the server through a public tunnel, so only four prefixes are served to requests from
outside, and each checks its own credential: `/api/voice/` (bearer token for the voice tools,
HMAC for the two call webhooks), `/api/bots/` (bearer token), `/api/hooks/` (HMAC) and
`/api/health`. Everything else, including the route that shows the word, is refused
unless the request was addressed to localhost and did not come through a proxy
(`server/app/local_only.py`).

Against rubber-stamping: the dashboard shows the approval rate and warns when every request
has been approved, and calls are capped per day.

### Rules by voice

"Raise my transfer limit to 300." The sentence is parsed by fixed patterns, not a model, so
what is read back is exactly what will be applied. Loosening a rule needs the word from the
owner's screen; tightening one needs only "confirm". The change goes through the vault's
owner-only `setPolicy`. The sentence can be spoken into the dashboard's microphone; the
confirmation is given on the dashboard, or on a call when the ElevenLabs agent hosts it.

### Prompt injection

Outside content (web pages, market descriptions, bot research, model rationales) only reaches
code that has no ability to move money, and its output is stored and shown as unverified text.
The "Try to trick it" box demonstrates the worst case: it asks the vault for exactly what the
hostile text demands, as a hijacked agent would. The result is a phone call the owner can
refuse, a denial at the call cap, or, if the demand is inside every limit and to a payee
already trusted, one capped transfer. That last case is the stated bound on a hijacked agent;
the demonstration reports it and does not execute it.

## ElevenLabs

ElevenLabs is used wherever speech does not need a phone line, and is kept as an alternative
for the part that does.

| Product | How it is used | State |
|---|---|---|
| **Speech-to-text** | The dashboard's microphone: the recording goes to `POST /v1/speech-to-text` and the text comes back into the sentence box for the owner to check | Live when `ELEVENLABS_API_KEY` is set |
| **Text-to-speech** | The operator's voice on the dashboard: the quote read back, a rule read back, the markets brief, the day's summary (`POST /v1/text-to-speech/{voice}`) | Live when the key is set |
| **Agents** (conversational AI) | The approval conversation with two personas, prompt in `server/app/agent_config.py`. Runs in the **browser widget** as the fallback when no phone line is available, and on the phone when `CALL_PROVIDER=elevenlabs_twilio` | Built; the agent is created by `scripts/create_agent.py --apply` and has not been created yet |
| **Server (webhook) tools** | Nine tools pointing at this server, each sending a bearer token held as a workspace secret: `get_request`, `approve`, `deny`, `freeze`; `list_markets`, `market_brief`, `market_override`; `propose_rule`, `confirm_rule` | With the agent |
| **Multi-voice** | On the agent, the risk officer speaks in a second voice (`<RiskOfficer>` tags) when `ELEVENLABS_RISK_OFFICER_VOICE_ID` is set | With the agent |
| **Outbound calling through Twilio, post-call webhooks** | `POST /v1/convai/twilio/outbound-call` and the HMAC-verified `call_initiation_failure` / `post_call_transcription` webhooks | Only with `CALL_PROVIDER=elevenlabs_twilio` |

**The phone call does not use ElevenLabs.** It is placed through AgentPhone, in AgentPhone's
voice, because Twilio is a paid line. The plan's endpoint
(`POST /v1/convai/twilio/outbound-call`) is still in the code behind
`CALL_PROVIDER=elevenlabs_twilio`.

Both dashboard speech routes (`/api/speech/say`, `/api/speech/hear`) hold the key on the
server and are served to this machine only. With no key, or if ElevenLabs refuses a request,
the dashboard falls back to the browser's own speech recognition and synthesis; the
microphone button's tooltip says which one is in use.

**Browser widget fallback.** With the agent created and a tunnel open:

```
cd server
uv run python -m scripts.create_agent --apply --write-env   # once; writes to your ElevenLabs account
uv run python -m scripts.widget_page 7 --open               # request 7, as shown on the dashboard
```

The second command writes a standalone page under `data/` that runs the agent in the
ElevenLabs widget with that request's variables, and opens it. The agent states the request,
argues it, and calls the same `approve` / `deny` / `freeze` tools. The page is kept apart from
the dashboard on purpose: the widget is third-party script, and the dashboard shows the
one-time word.

## AgentPhone

The approval call. One agent and one number on the account are used as they are; nothing here
creates an agent or buys a number.

```
cd server
uv run python -m scripts.agentphone_setup --write-env         # read-only: finds the agent and number ids
uv run python -m scripts.agentphone_setup --set-webhook-mode  # changes the agent: voiceMode = webhook
uv run python -m scripts.agentphone_setup --register-webhook  # changes the agent: points its webhook here
```

`--register-webhook` needs `PUBLIC_BASE_URL` set to the server's public HTTPS address (for
example `cloudflared tunnel --url http://localhost:8787`). It registers
`PUBLIC_BASE_URL/api/voice/hooks/agentphone` as the agent's webhook and saves the signing
secret AgentPhone returns in `.env` without printing it. Run the first command again at any
time to see what is still missing. Then set `OWNER_PHONE_NUMBER`, `CALL_PROVIDER=agentphone`
and, when the phone should really ring, `CALLS_ENABLED=true`, and restart the server.

What the call sounds like:

1. *"This is the Switchboard operator, an automated call about your own account. Your agent
   wants to send 250 dollars to Sam's Arc wallet. It needs your approval because it is over
   your per-transfer limit."*
2. After anything the owner says: the operator gives the case for it (the owner's own
   sentence), then *"Now the risk officer"* gives one objection, then the operator asks for the
   amount and the word on the dashboard.
3. *"Two hundred and fifty dollars, ocean."* The server checks both and answers
   *"Approved"*, or says which one did not match and how many tries are left.

"Deny" and "freeze" work at any point. The API key is sent to `api.agentphone.ai` and to no
other host; the address is a constant in the code, not a setting.

## Grok Bot

Grok Bots do the research and the watching; they never move money. Four routines run on their
own: one messages the owner when an approval call goes unanswered (triggered by a webhook from
this server), one posts the day's summary on a schedule, one researches whether two markets
settle the same way using X search, and one reconciles the ledger against the vault each day.
A bot's token opens read-only routes and two note-taking routes; it is rejected by every route
that approves, sends, freezes or changes a limit. What a bot writes is stored as unverified
outside content: a research note is shown next to the equivalence check and never changes its
verdict.

The exact text of each routine, its trigger and its setup are in
[bots/routines.md](bots/routines.md). Chat exports go in `bots/exports/`.

## Markets

Positions cannot be transferred between venues. A "move" is a sale on one and a purchase on
the other.

- **Same bet or not.** P1's check compares five fields in code: the event, the threshold, the
  direction, the event time, and who decides the outcome. If any differs, the move is refused
  and the reason is spoken ("different price index").
- **The cost of moving** is arithmetic from two order books: sell now and pay the taker fee,
  or post an ask and wait, which makes the sale a maker order.
- **"Will I get a bid?"** is read from bid depth and spread. It is a fact.
- **"I'd hold this"** is a forecast. One model from each of at least three families
  (Anthropic, OpenAI, Google, xAI) estimates the probability, starting from the market price;
  the answers are averaged in log-odds and pulled halfway back to the market. With fewer than
  three families answering there is no opinion. The owner can override it.
- **Receipts.** Each opinion is hashed with a salt and the hash is committed before the event.
  Afterwards it is revealed and scored with the Brier score, next to the market price's own
  score on the same events. That is the track record shown beside every opinion.

**The forecast is a second opinion from language models with a published track record. It is
not financial advice.**

The cash leg of a move is not automated: neither venue documents a withdrawal endpoint that
fits, so a balance is held on both and the rebalance is shown as a queued instruction. The
original sequence (sell, withdraw, deposit, re-buy) remains the production design once
withdrawals can be automated.

## Configuration

Everything is in `.env` (copy `.env.example`). `.env` takes priority over variables already in
the shell. A blank value always means dry run or fixtures, never a failure to start.

| Variable | For |
|---|---|
| `ANTHROPIC_API_KEY`, `INTENT_MODEL` | The intent parser. Blank: rules only |
| `OPENROUTER_API_KEY` (or `OPENAI_API_KEY`, `GOOGLE_AI_API_KEY`, `XAI_API_KEY`), `FORECAST_MODELS` | The forecast. Fewer than three families: no opinion |
| `CALL_PROVIDER`, `CALLS_ENABLED`, `OWNER_PHONE_NUMBER` | Who places the approval call (`dry_run`, `agentphone`, `elevenlabs_twilio`), whether a call may be placed at all, and the only number it may go to |
| `AGENTPHONE_API_KEY`, `AGENTPHONE_AGENT_ID`, `AGENTPHONE_NUMBER_ID`, `AGENTPHONE_WEBHOOK_SECRET` | The AgentPhone call. All four are needed; `scripts.agentphone_setup` fills in the last three |
| `ELEVENLABS_API_KEY` | Dashboard speech-to-text and text-to-speech. Blank: the browser's speech |
| `ELEVENLABS_AGENT_ID`, `ELEVENLABS_PHONE_NUMBER_ID`, `ELEVENLABS_WEBHOOK_SECRET`, `ELEVENLABS_*_VOICE_ID` | The ElevenLabs agent: the browser widget fallback, and the `elevenlabs_twilio` call |
| `VOICE_TOKEN`, `BOT_TOKEN` | Bearer tokens for the voice tools and for bot routes. Generate long random values |
| `PUBLIC_BASE_URL` | The server's public HTTPS address (a tunnel), for webhooks and voice tools |
| `GROK_MISSED_CALL_WEBHOOK_URL` | Where to report a missed call |
| `POLICY_*`, `PASSKEY_THRESHOLD_USD`, `APPROVAL_EXPIRY_S`, `MAX_CALLS_PER_DAY` | The limits |
| `TRADING_ENABLED`, `MAX_ORDER_USD` | The real-money guards. Leave as they are |
| `USE_P1_STUBS` | `true` until P1's modules are wired in |

## P1 sections

These belong to the money half and are filled in on its branch.

- **Contract addresses and how Arc is used**: to come with the vault deployment.
- **QuickNode Stream configuration** (network, dataset, filter, destination): to come.
- **Gemini endpoints used**: to come with the venue clients. The Phase 0 proof on this branch
  reads `GET https://api.gemini.com/v1/prediction-markets/events` and
  `GET https://gateway.polymarket.us/v1/markets` (public, no key).
- **Bank on-ramp** (sandbox): to come.

## Documents

| File | What it is |
|---|---|
| [docs/product-plan.md](docs/product-plan.md) | What the product does, the safety design, sponsor tools, demo script |
| [docs/implementation-plan.md](docs/implementation-plan.md) | Roles for two people, stack, repo layout, shared interface, phases |
| [docs/prompts/](docs/prompts/) | Prompts for unattended Claude Code runs on each half |
| [bots/routines.md](bots/routines.md) | Grok Bot routines |
| [OVERNIGHT_LOG.md](OVERNIGHT_LOG.md) | What was built on this branch, what is blocked, decisions, and what needs a person |
