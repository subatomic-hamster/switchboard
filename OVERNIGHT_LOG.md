# Overnight log: P2 (Voice and Surface)

Branch `p2/voice-surface`, draft PR
[subatomic-hamster/switchboard#1](https://github.com/subatomic-hamster/switchboard/pull/1).

**State at the end of the run.** Every P2 item in every phase is built and tested against
stubbed P1 routes, or listed under Blocked. 154 server tests and 39 dashboard tests pass; ruff,
mypy (strict) and the TypeScript check are clean; the branch was cloned fresh and the full
setup, checks, build and mock demo were run from that clone. No phone call was placed, no order
was placed, no funds moved, nothing was deployed, and nothing was written to any of your
accounts.

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

Your existing `.env` already works: it has the model keys you added and the two generated
tokens.

## Needs you

In order. Items 1 to 5 are what stands between this branch and a real phone call.

1. **Reminder you asked for: real phone calls.** None were placed. Calls stay in dry run until
   `CALLS_ENABLED=true` and items 2 to 5 are done. The Phase 0 proof is then
   `cd server && uv run python -m scripts.phase0_call --place-real-call`, which rings
   `OWNER_PHONE_NUMBER` once.
2. **ElevenLabs key.** `ELEVENLABS_API_KEY` is still blank in `.env`, so nothing could be
   looked up or created. Your question was whether the other ids can be got from the main key:
   yes. Add the key, then `cd server && uv run python -m scripts.create_agent --list` prints
   your existing agent ids and phone-number ids (read-only).
3. **Import the Twilio number into ElevenLabs** (your "how to do this"):
   1. In the Twilio console, copy the Account SID and Auth Token, and note the number. Use a
      paid account; a trial one only calls verified numbers and plays a trial message.
   2. In ElevenLabs, open Agents, then the Phone Numbers tab, and import a number: choose
      Twilio, give it a label, paste the number, the SID and the token.
   3. Run `uv run python -m scripts.create_agent --list` and copy the `phone_number_id` into
      `ELEVENLABS_PHONE_NUMBER_ID`.
4. **Open a tunnel.** Install `cloudflared`, run `cloudflared tunnel --url http://localhost:8787`
   and put the `https://…` address in `PUBLIC_BASE_URL`. Only the token-protected voice, bot
   and webhook routes are served through it; the dashboard stays local.
5. **Create the agent.** `uv run python -m scripts.create_agent --dry-run` shows what would be
   sent; `--apply --write-env` creates one workspace secret, nine tools and the agent in your
   ElevenLabs account and saves the agent id. Optional first: set
   `ELEVENLABS_OPERATOR_VOICE_ID` and `ELEVENLABS_RISK_OFFICER_VOICE_ID` for two distinct
   voices. Then set `OWNER_PHONE_NUMBER`. For missed calls to deny at once instead of at the
   5-minute expiry, add a post-call webhook in ElevenLabs pointing at
   `<PUBLIC_BASE_URL>/api/voice/hooks/elevenlabs` (events: transcript and call initiation
   failure) and put its secret in `ELEVENLABS_WEBHOOK_SECRET`.
6. **Listen to one call.** I cannot hear audio. Check that the risk officer speaks in the
   second voice and always objects, and that "yes" alone is refused. If the two voices do not
   work on a phone call, blank `ELEVENLABS_RISK_OFFICER_VOICE_ID` and re-run
   `create_agent --apply`; the prompt then has one voice announce each speaker.
7. **Try the passkey and the microphone in your own browser.** Both are covered by tests (the
   passkey with a software authenticator that signs real assertions) but neither could be
   exercised in the automated browser: register a passkey under Safety controls, then send
   something over $1,000; and press "Speak" in Chrome or Edge.
8. **Set up the four Grok Bot routines** from `bots/routines.md` (they cannot be created by
   API), run each once, and save the chat exports under `bots/exports/`.
9. **Record the demo video.** `uv run python -m app.demo_mock` paces a full run for the screen;
   the call audio needs items 1 to 6.
10. **Rotate the key you pasted into the chat.** It is in the transcript. I did not use or
    store it. The "Cursor key" is not used by anything here.
11. **Decide the wording for Kalshi, Limitless and Phantom.** You asked for them to be
    described as supported. They are shown as "Planned", not connected, because the README has
    to carry an honest live-versus-stub table and a judge who tries one would find nothing
    behind it. The label is one edit away: `VENUES` in `server/app/main.py`.
12. **Tell P1** about the interface proposals in Decisions (marked P1), most of all the event
    payloads and `server/app/ports.py`.

## Done

| Phase | Task | Commit |
|---|---|---|
| 0 | Layout, `schemas.py`, event bus and SSE, SQLModel tables, stub routers, generated-types script, Vite app | `88f1b12` |
| 0 | Proof: fetch Gemini and Polymarket US and print one matched pair. Run against the live public APIs; it found Rockets at Nuggets on both, same start time, 45/46 on each | `c6585ac` |
| 0 | Proof: outbound call script. Dry run only; the real call is Blocked | `ae425a2` |
| 1 | Intent parser: sentence to `TransferRequest`. LLM path verified on a live call | `42297a2` |
| 1 | Planner: cheapest and fastest routes, pay / ask / block with a reason | `42297a2` |
| 1 | Agent prompt with two personas, the four voice tools, the outbound call function | `ae425a2` |
| 1 | Dashboard: event feed and sentence input | `b3bb4f5` |
| 2 | Ring view: vault at the centre, particles, flash and tone on a block | `83970c2` |
| 2 | Voice input on the dashboard; route quote read back before execution | `83970c2` |
| 2 | Approval hardening: amount said back, challenge word on screen, no answer means deny | `ae425a2` |
| 2 | Approval hardening: passkey above a second threshold | `068838e` |
| 3 | Markets panel: both venues' prices side by side for a matched pair | `180eef4` |
| 3 | Markets operator voice: match result, move cost, bid depth, hold-or-move opinion with track record, override | `221d8c4`, `cd008ce` |
| 3 | Forecast receipts: hash before the event, reveal after, Brier score. Four model families verified on live calls | `221d8c4` |
| 4 | Anti-rubber-stamping: daily cap on calls, approval-rate counter with a warning at 100% | `ae425a2`, `83970c2` |
| 4 | Rules by voice: say a rule, hear it read back, confirm, policy updates | `278779c`, `008c5db` |
| 4 | Hostile-content text box | `278779c`, `008c5db` |
| 5 | Grok Bot routines: server routes, and `bots/routines.md` with the exact text, trigger, calls and setup of all four | `044979d` |
| 5 | Flight recorder: scrub bar replaying past activity on the ring | `719064f` |
| 5 | README: setup from a clean clone, architecture diagram, decision loop, safety limits, ElevenLabs products, Grok Bot role | `c2c2712` |
| – | Mock event generator `uv run python -m app.demo_mock` | `83970c2` |
| – | Dashboard routes served only to this machine, even behind a tunnel (found in self-review) | `1699a16` |
| – | Bot research notes shown in the markets panel as unverified | `9f48b2f` |

## Blocked

Each was retried at the end of the run; none had changed.

| Item | Why | Exact step that unblocks it |
|---|---|---|
| Real outbound approval call, and the Phase 0 "reaches a real phone and speaks a variable" proof | `ELEVENLABS_API_KEY`, agent id, phone-number id and owner number are all blank, and you chose to place calls later | "Needs you" items 1 to 5 |
| Creating the ElevenLabs agent | No key; no public HTTPS address; and creating it writes to your account, which you did not approve | "Needs you" items 2, 4 and 5 |
| Confirming two voices on one call | Needs a real call and a listener | "Needs you" item 6 |
| Everything behind P1: the vault, real transfers, the Stream webhook, the venue clients, the real match and move | The remote has only `main` and this branch; nothing from P1 | P1 provides objects that satisfy `server/app/ports.py` and routers for its routes, they are wired in `app.main.build_context`, and `USE_P1_STUBS=false`. Until then `USE_P1_STUBS=false` stops the server with a message saying so |
| Forecast receipts and voice rules "onchain" | The plan has no contract function or route for either | P1 implements `AnchorPort.commit` (receipts) and `VaultPort.set_policy` (rules) against the contract. The hashing, verification and rule logic are done |
| Grok Bot routines running, and the chat exports | Cannot be created by API | "Needs you" item 8 |
| Demo video | Needs a person | "Needs you" item 9 |

## Decisions

Choices made where the plan was silent or ambiguous. Items marked **P1** need the teammate's
agreement because they touch the shared interface.

1. **Stack on this machine.** Python 3.12 through uv; Node 22.23.4 (the plan only needs Node
   for the Vite build); pnpm 12 installed globally with `npm install -g pnpm` because it was
   missing. `web/pnpm-workspace.yaml` approves esbuild's build script, which pnpm 12 blocks by
   default.
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
   `forecast.py`, `market_brief.py`, `rules.py`, `hostile.py`, `bots.py`, `local_only.py`,
   `demo_mock.py`) and `server/scripts/`. The only files of P2's under `chain/` or `markets/`
   are the two clearly marked `stub.py` files and empty `__init__.py` files. Nothing under
   `docs/` or `contracts/` was touched.
7. **Extra event types.** Five additive types for P2 features: `call.started`, `call.ended`,
   `policy.updated`, `forecast.committed`, `forecast.revealed`.
8. **Extra routes.** All additive, under `/api`: `stats`, `freeze`, `wallets`, `venues`,
   `summary`, `notes`, `events/history`, `approvals…`, `passkey/…`, `desk/…`, `forecasts…`,
   `rules…`, `hostile`, `bots/…`, `voice/markets/…`, `voice/rules/…`,
   `voice/hooks/elevenlabs`, and the stub-only `chain/stub/stream`.
9. **Stub quote formula.** The stub's `move_cost_pct` is the full taker cost on both sides
   over the sale value (11.3% for the plan's NBA example). The plan's example figure of 6.1%
   leaves out the sell-side fee; P1 owns the real formula.
10. **Kalshi, Limitless, Phantom.** Shown as "Planned", not connected. See "Needs you" 11.
11. **Models.** Intent parser: Anthropic `claude-opus-5-5` at low effort, with an 8-second
    budget, after which the rule-based parser answers; the first call of a run is warmed up at
    start because it is slow. Forecast: four families, one model each: `claude-opus-5-5`
    (Anthropic, direct) and, through OpenRouter, `openai/gpt-6.2-astra`,
    `google/gemini-4-argon` and `x-ai/grok-4.7`. All four answered on live calls. The direct
    OpenAI and Google keys are used only if the OpenRouter key is removed.
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
    never in an event, a call variable, a voice-tool response or a bot webhook.
17. **The dashboard is local-only.** Found in self-review: a tunnel opened for ElevenLabs would
    also have exposed the route that shows the word. Now only `/api/voice/`, `/api/bots/`,
    `/api/hooks/` and `/api/health` are served to requests from outside; everything else needs
    a localhost Host header and no proxy headers.
18. **Passkey.** One passkey per installation, registered on first use; a second registration
    is refused. Each assertion's challenge is bound to one approval request.
19. **Dry-run stand-in for the call.** With calls not live, the dashboard shows a labelled
    "stand in for the call" form that goes through the same checks. With calls live the route
    returns 403.
20. **Stance rule for the opinion.** "Move" only if the combined probability is above what one
    contract costs to re-buy on the destination with fees; otherwise "hold". With no outside
    information the models stay at the market price, so the opinion is usually "hold".
21. **Combining models.** Average in log-odds, then move halfway back to the market price.
22. **Forecast outcome.** `POST /api/forecasts/{id}/reveal` takes the outcome from the caller.
    Reading settlement from the venue belongs to P1's client.
23. **Two voices.** ElevenLabs multi-voice is configured when a second voice id is set
    (`<RiskOfficer>` tags); otherwise one voice announces each speaker.
24. **Voice input and read-back** use the browser's speech recognition and synthesis. No
    ElevenLabs credit is spent by the dashboard.
25. **Rules by voice** are parsed by fixed patterns, not a model, so the read-back is exactly
    what is applied. Loosening needs the word from the screen; tightening needs only "confirm".
    One proposal is open at a time.
26. **Hostile-content box.** It shows two things: the tool-less reader only notices; and the
    worst case, by asking the vault for exactly what the text demands. If that demand is inside
    every limit and to a trusted payee, the vault would allow it; the box reports that as the
    bound on a hijacked agent and does not redeem the permit, so the demonstration never moves
    money by itself.
27. **Bots.** Read-only routes plus two note-taking routes behind `BOT_TOKEN`, which no route
    that approves, sends, freezes or changes a limit accepts. Bot notes are stored and shown as
    unverified and never change the equivalence verdict. The missed-call webhook is sent only
    to an `https://` address.
28. **Ledger.** Written from `transfer.sent`, `transfer.arrived` and `trade.filled` events, so
    it works unchanged when P1's real modules publish them.
29. **Stub restarts.** The stub vault is in memory. Requests left pending across a restart are
    denied at start-up and recorded in the event log.
30. **No frontend linter** was added (the plan asks for frontend tests and a type check). The
    TypeScript configuration is strict.
