# Overnight log: P2 (Voice and Surface)

Branch `p2/voice-surface`, draft PR
[subatomic-hamster/switchboard#1](https://github.com/subatomic-hamster/switchboard/pull/1).

**State at the end of the run.** Every P2 item in every phase is built and tested against
stubbed P1 routes, or listed under Blocked. 208 server tests and 45 dashboard tests passed
then (242 and 51 after the follow-ups below); ruff, mypy (strict) and the TypeScript check are
clean. No phone call was placed, no order was placed, no funds moved and nothing was deployed.
Nothing was changed in any of your accounts during that run; the one change since is in the
second follow-up below.

**Follow-up on 19 November: the phone provider changed.** Twilio is out. The approval call is
now placed through AgentPhone, behind `CALL_PROVIDER`; the ElevenLabs and Twilio path is kept.
ElevenLabs now does the dashboard's microphone and read-back. What was run against your
accounts in this follow-up, all of it read-only or a single generation:

- AgentPhone: `GET /v1/agents`, `GET /v1/numbers` and `GET /v1/agents/{id}/webhook`, once, to
  find the agent and number ids. They are saved in your local `.env`.
- ElevenLabs: one sentence through text-to-speech and the same audio through speech-to-text
  (it came back as "Send 200 dollars from my Ark wallet to Sam."), and two more short
  text-to-speech requests while checking the dashboard in a browser.

**Second follow-up on 19 November.**

- **Your AgentPhone agent is now in webhook mode.** You said yes; I ran
  `agentphone_setup --set-webhook-mode` and read the agent back. This is the one change made
  to any of your accounts. No webhook is registered and no call was placed.
- **Connected assistants were added**, at your request: ChatGPT, Claude, Grok and others can
  ask Switchboard to send, each with its own key and its own limit. This is not in the plan;
  see Decisions 44 to 52. 242 server tests and 51 dashboard tests pass. It was checked on
  this machine with the official MCP client over real HTTP. No assistant was connected for
  real, because that needs a public address.
- Versine (versine.com) was looked at once, read-only, to answer your question. Nothing was
  taken from it except the idea of a limit per assistant.
- While checking in the browser I made one test connector on your local database, sent one
  simulated 20 dollar request through it, approved it in the dry-run stand-in, and revoked
  the connector. It shows as "1 revoked connector not shown". No funds exist to move.

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

Your existing `.env` already works: it has the model keys you added, the two generated
tokens, the AgentPhone and ElevenLabs keys from your message, the two AgentPhone ids, and
`CALL_PROVIDER=agentphone` with `CALLS_ENABLED=false`, so it runs in dry run and logs the
AgentPhone request it would send.

## Needs you

In order. Items 1 to 8 are what stands between this branch and a real phone call.

1. **Reminder you asked for: real phone calls.** None were placed. Nothing rings until you
   set `CALLS_ENABLED=true` yourself, after items 2 to 7.
2. **Rotate the keys you pasted into the chat**: the AgentPhone key, the ElevenLabs key, and
   the earlier one. They are in the transcript. I copied the AgentPhone and ElevenLabs keys
   into your local `.env` without printing them, because you gave them for that; they are not
   in the repository. After rotating, put the new values in `.env`.
3. **Set `OWNER_PHONE_NUMBER`** in `.env`, in the form `+16045550123`. It is still blank. It
   is the only number a call can go to.
4. **Check AgentPhone billing.** Their documentation says outbound calls need a payment
   method on file and that sign-up credit alone may not unlock calling (the API answers 402),
   and voice minutes are billed as used. You dropped Twilio because it costs money, so check
   this before relying on it. A 402 shows on the dashboard as a failed call and the request
   stays pending until it expires.
5. **Open a tunnel.** Install `cloudflared`, run `cloudflared tunnel --url http://localhost:8787`
   and put the `https://...` address in `PUBLIC_BASE_URL`. AgentPhone posts every turn of the
   call to it. Only the signed and token-protected routes are served through it; the dashboard
   stays local.
   **Done once on 19 November, on your say:** I installed `cloudflared` with winget, opened a
   quick tunnel and saved its address in `PUBLIC_BASE_URL`. Checked from outside: health,
   the assistant routes and the bot routes answer (401 without a key, 200 with the bot
   token); the dashboard, the approvals list with the word, freeze, the event stream and
   connector management all answer 403. A quick tunnel lasts only while it runs and gets a
   new address next time, so this step repeats, and steps 7 and 11 with it.
   **Found while doing it:** the AgentPhone settings are no longer in `.env`
   (`AGENTPHONE_API_KEY`, `AGENTPHONE_AGENT_ID`, `AGENTPHONE_NUMBER_ID`, `CALL_PROVIDER`).
   I did not remove them and git never touched the file. If you rotated the key, add the new
   one and `CALL_PROVIDER=agentphone`, then run `agentphone_setup --write-env` to save the
   two ids again. Until then the webhook cannot be registered and calls stay in dry run.
6. ~~Switch your AgentPhone agent to webhook mode.~~ **Done on 19 November, on your say.** I ran
   `uv run python -m scripts.agentphone_setup --set-webhook-mode` and read the agent back: its
   `voiceMode` is `webhook`. AgentPhone clears the hosted prompt and greeting when it switches.
7. **Register the webhook with AgentPhone.**
   `uv run python -m scripts.agentphone_setup --register-webhook`. It sets the agent's webhook
   to `PUBLIC_BASE_URL/api/voice/hooks/agentphone` and saves the signing secret in `.env` as
   `AGENTPHONE_WEBHOOK_SECRET`. A quick tunnel gets a new address each time it starts, so
   repeat this step whenever the address changes. Then run
   `uv run python -m scripts.agentphone_setup` with no flags: it lists what is still missing.
8. **Place the first call and listen.** Set `CALLS_ENABLED=true`, restart the server, and
   either run `uv run python -m scripts.phase0_call --place-real-call` (one test call that can
   approve nothing) or send "Send 250 from my Arc wallet to Sam's Arc wallet" from the
   dashboard and answer. Things I could not check without a real call:
   - whether the call id in AgentPhone's webhook is the one returned when the call is placed
     (if not, the server matches the call by direction and your number, and logs a warning);
   - whether each turn of an outbound call reaches the webhook now that the agent is in webhook
     mode (item 6);
   - how well the speech-to-text hears the amount and the word on a phone line. If a word is
     misheard, say it again; only a wrong amount-and-word pair costs one of the three tries.
   The risk officer is in the same voice as the operator. That is a limit of AgentPhone, not
   a fault.
9. **Try the passkey and the microphone in your own browser.** The microphone now records and
   sends the audio to ElevenLabs (the button's tooltip says so). Both are covered by tests, but
   neither can be exercised in the automated browser: register a passkey under Safety controls,
   then send something over $1,000; and press "Speak".
10. **Optional: the ElevenLabs agent, for the browser-widget fallback.** Not needed for the
    AgentPhone call. `uv run python -m scripts.create_agent --apply --write-env` creates one
    workspace secret, nine tools and the agent in your ElevenLabs account (needs the tunnel).
    Then `uv run python -m scripts.widget_page <request id> --open` holds the approval
    conversation in the ElevenLabs widget. This path has not been run: the agent does not
    exist yet.
11. **Finish the Grok Bot routines.** Made on 19 November, on your say, in your signed-in
    Chrome: a Bot named `Switchboard` on grok.com (making it accepted xAI's Grok Bot terms),
    with `Switchboard: daily summary` (daily, about 18:00 ET), `Switchboard: reconciliation`
    (daily, about 18:30 ET) and `Switchboard: missed call` (webhook) as routines. The Bot
    saved `Switchboard: equivalence research` as a skill you run by asking, because a routine
    needs a schedule or a trigger. None has run successfully yet. Yours to do:
    - **Paste `BOT_TOKEN` into the secret card** the Bot put in its chat ("Switchboard bot
      token", then "Send securely"). I do not type secrets into web pages. Until then every
      run says the token is missing.
    - **Copy the missed-call routine's webhook address and key** from the Bot's Routines into
      `.env` as `GROK_MISSED_CALL_WEBHOOK_URL` and `GROK_MISSED_CALL_WEBHOOK_KEY`, and restart.
    - **Keep the server and the tunnel running** when the routines fire. Their text carries
      the quick tunnel's address, which changes when the tunnel restarts; then tell the Bot
      the new address for each one.
    - Run each once and save the chats under `bots/exports/`.
12. **Record the demo video.** `uv run python -m app.demo_mock` paces a full run for the screen
    (it needs `CALLS_ENABLED=false`); the call audio needs items 1 to 8.
13. **Decide the wording for Kalshi, Limitless and Phantom.** You asked for them to be
    described as supported. They are shown as "Planned", not connected, because the README has
    to carry an honest live-versus-stub table and a judge who tries one would find nothing
    behind it. The label is one edit away: `VENUES` in `server/app/main.py`.
14. **Tell P1** about the interface proposals in Decisions (marked P1), most of all the event
    payloads and `server/app/ports.py`.
15. **Connect a real assistant.** Nothing outside this computer can reach Switchboard until
    the tunnel is open and `PUBLIC_BASE_URL` is set (item 5). Then, on the dashboard under
    "Connected assistants", make a connector and paste its address into the assistant. Start
    with the limit at 0, so it asks you every time. The README table says what each
    assistant accepts; none of it has been tried from here.
16. **Decide about the secret address.** ChatGPT, Claude and Grok take an address and no key
    in their own settings, so those connectors carry the key in the address. Anyone who has
    the address can act as that assistant, inside its limit. If that is not acceptable, use
    only key-type connectors (Claude Code, the APIs, Gemini CLI) until OAuth sign-in is built.

## Done

| Phase | Task | Commit |
|---|---|---|
| 0 | Layout, `schemas.py`, event bus and SSE, SQLModel tables, stub routers, generated-types script, Vite app | `720ff44` |
| 0 | Proof: fetch Gemini and Polymarket US and print one matched pair. Run against the live public APIs; it found Rockets at Nuggets on both, same start time, 45/46 on each | `56cc078` |
| 0 | Proof: outbound call script. Dry run only; the real call is Blocked | `5eb55c0` |
| 1 | Intent parser: sentence to `TransferRequest`. LLM path verified on a live call | `3a9f7a7` |
| 1 | Planner: cheapest and fastest routes, pay / ask / block with a reason | `3a9f7a7` |
| 1 | Agent prompt with two personas, the four voice tools, the outbound call function | `5eb55c0` |
| 1 | Dashboard: event feed and sentence input | `249937f` |
| 2 | Ring view: vault at the centre, particles, flash and tone on a block | `7dd36d1` |
| 2 | Voice input on the dashboard; route quote read back before execution | `7dd36d1` |
| 2 | Approval hardening: amount said back, challenge word on screen, no answer means deny | `5eb55c0` |
| 2 | Approval hardening: passkey above a second threshold | `3105dc5` |
| 3 | Markets panel: both venues' prices side by side for a matched pair | `fe8eb74` |
| 3 | Markets operator voice: match result, move cost, bid depth, hold-or-move opinion with track record, override | `1e130a0`, `74b2be7` |
| 3 | Forecast receipts: hash before the event, reveal after, Brier score. Four model families verified on live calls | `1e130a0` |
| 4 | Anti-rubber-stamping: daily cap on calls, approval-rate counter with a warning at 100% | `5eb55c0`, `7dd36d1` |
| 4 | Rules by voice: say a rule, hear it read back, confirm, policy updates | `a3ab3ab`, `82dcf53` |
| 4 | Hostile-content text box | `a3ab3ab`, `82dcf53` |
| 5 | Grok Bot routines: server routes, and `bots/routines.md` with the exact text, trigger, calls and setup of all four | `86d30ff` |
| 5 | Flight recorder: scrub bar replaying past activity on the ring | `00014a3` |
| 5 | README: setup from a clean clone, architecture diagram, decision loop, safety limits, ElevenLabs products, Grok Bot role | `25d9b06` |
| – | Mock event generator `uv run python -m app.demo_mock` | `7dd36d1` |
| – | Dashboard routes served only to this machine, even behind a tunnel (found in self-review) | `874fab6` |
| – | Bot research notes shown in the markets panel as unverified | `71f3cad` |
| 1 | Call placement behind `CALL_PROVIDER` (`dry_run`, `agentphone`, `elevenlabs_twilio`); AgentPhone call in webhook mode; `POST /api/voice/hooks/agentphone` with signature and timestamp checks; the approval conversation as a state machine in our code; `scripts/agentphone_setup.py` | `75fc578` |
| 2 | Dashboard microphone and read-back through ElevenLabs speech-to-text and text-to-speech, browser speech as the fallback; ElevenLabs browser-widget fallback for the approval (`scripts/widget_page.py`) | `b966ab8` |
| – | Fix: after a restart on the same database the stub vault reused request numbers, so a new over-limit request raised no approval (found in the browser) | `b966ab8` |
| – | Connected assistants: a key and a limit per assistant, held requests, MCP endpoint (official SDK) and JSON routes, dashboard panel | `8a7a03c` |

## Blocked

Each was retried at the end of the first run; none had changed. The call rows were rewritten
on 19 November for the provider change.

| Item | Why | Exact step that unblocks it |
|---|---|---|
| Real outbound approval call, and the Phase 0 "reaches a real phone and speaks a variable" proof | Calls are still on your say. Also missing: the owner number, a public HTTPS address, and the registered webhook with its secret. The agent is in webhook mode | "Needs you" items 1 to 8 |
| Registering the AgentPhone webhook | It changes your AgentPhone account and needs a public address | "Needs you" items 5 and 7 |
| Creating the ElevenLabs agent (now only for the browser-widget fallback and the `elevenlabs_twilio` path) | No public HTTPS address; and creating it writes to your account, which you have not approved | "Needs you" item 10 |
| Two distinct voices on the phone call | AgentPhone gives one voice per call. The risk officer is announced by name in the same voice | Known limit. Two voices exist only on the ElevenLabs agent |
| Rules by voice and the markets operator on the phone call | They are tools of the ElevenLabs agent; the AgentPhone call is a fixed approval script | A rule is spoken into the dashboard microphone and confirmed on the dashboard |
| Everything behind P1: the vault, real transfers, the Stream webhook, the venue clients, the real match and move | The remote has only `main` and this branch; nothing from P1 | P1 provides objects that satisfy `server/app/ports.py` and routers for its routes, they are wired in `app.main.build_context`, and `USE_P1_STUBS=false`. Until then `USE_P1_STUBS=false` stops the server with a message saying so |
| Forecast receipts and voice rules "onchain" | The plan has no contract function or route for either | P1 implements `AnchorPort.commit` (receipts) and `VaultPort.set_policy` (rules) against the contract. The hashing, verification and rule logic are done |
| Connecting a real assistant (ChatGPT, Claude, Grok, ...) | It has to reach this server from the internet, and the hookup is done in your account with each one | "Needs you" items 5 and 15 |
| Confirming that money arrived, for an assistant | Nothing here watches for incoming payments | P1's chain module publishes arrivals; then `switchboard_receive` can report them |
| The Gemini app, and any assistant that only accepts OAuth sign-in | Switchboard has no OAuth sign-in | Build an authorization server for connectors (not started) |
| Grok Bot routines running, and the chat exports | Cannot be created by API | "Needs you" item 11 |
| Demo video | Needs a person | "Needs you" item 12 |

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
10. **Kalshi, Limitless, Phantom.** Shown as "Planned", not connected. See "Needs you" 13.
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
24. **Voice input and read-back.** Superseded by decision 36: they now go through ElevenLabs
    when a key is set. The browser's speech recognition and synthesis remain the fallback.
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
31. **Phone provider: AgentPhone instead of Twilio. This departs from the plan.** The plan
    names ElevenLabs Agents calling through Twilio (`POST /v1/convai/twilio/outbound-call`).
    On your instruction of 19 November the approval call is placed with
    `POST https://api.agentphone.ai/v1/calls` instead, because Twilio is a paid line. The
    plan's endpoint is still in the code and its tests, behind
    `CALL_PROVIDER=elevenlabs_twilio`, and `scripts/create_agent.py` is unchanged. The frozen
    voice-tool routes are unchanged; `POST /api/voice/hooks/agentphone` is an addition.
32. **`CALL_PROVIDER` defaults to `dry_run`.** A call is placed only when `CALLS_ENABLED=true`,
    a provider is named, and everything that provider needs is set. One consequence: the
    ElevenLabs settings alone no longer make calls live, as they did before; that path now
    needs `CALL_PROVIDER=elevenlabs_twilio` as well. A blank value means dry run; a misspelt
    one stops the server at start-up instead of silently placing no calls.
33. **The AgentPhone conversation is a state machine in our code, with no language model.**
    AgentPhone runs in webhook mode (no `systemPrompt`): it posts each thing the owner says to
    this server, which replies with the text to speak. The order is fixed: the greeting states
    the request; the first reply gives the case for it, the risk officer's one objection, and
    the ask; then the amount and the word are collected. Whatever is said first, even the
    right amount and word, the objection is spoken before anything can be approved. The
    replies are NDJSON, so "Checking." is spoken while the vault is asked. The script lives in
    `server/app/call_script.py`; the state is one row per call in a new table.
34. **Same rules, same code.** The call ends in the existing `approve`, `deny` and `freeze`
    methods: the amount must match, the word must match, three wrong tries deny, a passkey is
    still needed above the threshold, and no answer or a call that ends without an approval
    denies. Extra guards for this path: more than one vocabulary word in one turn counts as a
    wrong try (it is a guess, not the word); an amount with two readings ("two fifty") is asked
    again and costs no try; twelve turns without a decision deny; "no" on its own denies.
    Nothing recognises the speaker.
35. **Known limitation: one voice.** AgentPhone gives one voice per call, so the risk officer
    is announced by name in the operator's voice. Also on this path: rules by voice and the
    markets operator are not on the call (they are ElevenLabs agent tools), so a rule is
    confirmed on the dashboard whenever the live call is AgentPhone.
36. **Where ElevenLabs is used now.** No telephony: the dashboard's microphone
    (speech-to-text) and the operator's voice reading the quote, rules, briefs and summary
    (text-to-speech), through two local-only routes that keep the key on the server. The
    agent with two personas remains for the browser-widget fallback. The README says in so
    many words that the phone call does not use ElevenLabs.
37. **The widget is not embedded in the dashboard.** The ElevenLabs widget is third-party
    script, and the dashboard shows the one-time word, so embedding it would put the word
    within the script's reach. `scripts/widget_page.py` writes a standalone page instead,
    opened from disk, which cannot read the dashboard or the local API.
38. **Whose call it is.** The webhook acts only on a call this server placed. An inbound call
    to the number, or any call id it does not know, is told nothing can be approved and hung
    up on. One exception, because AgentPhone's documents do not promise that the webhook's
    call id equals the id returned at placement: an unknown id is accepted if the call is
    outbound, to `OWNER_PHONE_NUMBER`, and exactly one placed call is still waiting for its
    first turn. Events for another agent on the account, test events and non-voice events are
    acknowledged and ignored.
39. **The AgentPhone key goes to one host.** `https://api.agentphone.ai` is a constant in
    `voice.py` and in the setup script, not a setting. Redirects are not followed.
40. **What the greeting says.** Amount, payee and reason only. It is fixed when the call is
    placed, before the owner's sentence is linked to the request, and it is what a voicemail
    would record. The case for the request is read from the record on the first reply.
41. **Keys given in chat.** The prompt says not to take keys in chat. You pasted two with the
    instruction to use them, so they were copied into the git-ignored `.env` by a script that
    reads the transcript and prints only names and lengths. They were never printed, logged or
    committed, and the commit check now refuses those patterns. See "Needs you" 2.
42. **Stub numbering survives a restart.** The stubs count requests, permits, transfers and
    moves in memory. They now start above the highest event id the database has recorded, so a
    number from an earlier run is never reused. Before this, a new request after a restart was
    taken for a duplicate delivery and dropped without a call or an approval panel.
43. **"Ark" is read as Arc** by the intent parser, because that is how speech-to-text writes
    the word when the sentence is spoken.
44. **Connected assistants. This is not in the plan.** You asked for connectors so ChatGPT,
    Claude, Grok and others can send and receive through Switchboard. Built as
    `server/app/connectors.py`, `mcp_http.py` and `holds.py`, all additive. The frozen routes
    and events are unchanged.
45. **An assistant gets less than the agent, never more.** Its request goes through the same
    planner and vault. It can pay only people in the wallet book (an address is refused, not
    queued for a call), and it has five operations: status, quote, send, its own transfers,
    receiving addresses. Approving, limits, payees, freeze and the word have no route.
46. **A limit per assistant, held by the server (P1).** Each connector has "without asking,
    up to" and a daily figure, 0 by default, which means ask every time. A spend the vault
    would allow but that is over the assistant's limit is held in `HoldingVault`, a wrapper
    around `VaultPort`: it publishes the same `approval.requested`, the same call follows,
    and the vault is asked only after a yes. If the vault would refuse by then, nothing is
    sent and no second request is raised. Held request ids start with `h`. The contract
    knows one agent key, so this limit is not enforced onchain. Proposal to P1: a key per
    assistant in the vault, each with its own cap.
47. **New reason code `OVER_AGENT_LIMIT`**, server-side like `CALL_CAP_REACHED`. Additive.
48. **An assistant cannot spend your attention freely.** Three approval requests in 24 hours
    and one waiting at a time, per assistant; past that it is refused without a call. The
    same amount to the same payee inside two minutes is refused unless the assistant marks
    it deliberate, because assistants retry tool calls that time out.
49. **What the call says about it.** The operator says the request came from the named
    assistant and not from a sentence of yours; the risk officer says which of its limits it
    passed. Both are built from the connector's name (which you typed, reduced to plain
    characters) and from numbers. The assistant cannot add a note to a request: anything it
    wrote would be read to you as the case for paying.
50. **MCP through the official SDK, not hand-written.** The protocol changed after my
    training data (revision 2026-07-28 is stateless), so I installed `mcp` 2.3 and read its
    API from the package. The server authenticates before the SDK sees a request, passes the
    connector's identity inward in a header it sets itself (a copy sent by the client is
    dropped), and answers only POST. The SDK's own Host check is off because it accepts only
    localhost and every request already carries a token.
51. **Two kinds of key.** A key sent as `Authorization: Bearer`, or a key that is part of
    the address, for assistants whose settings take only an address (ChatGPT, Claude, Grok,
    per their documentation). Each key works one way only. The address form is weaker: who
    has the address is the assistant. It is the owner's choice per connector, the dashboard
    says so, and limits still bound it. OAuth sign-in is the proper fix and is not built.
    The MCP spec forbids access tokens in a query string; this is a path, and it is not an
    OAuth token, but the same caution applies. Only the key's SHA-256 is stored.
52. **Receiving is addresses only.** `switchboard_receive` returns your wallets. Nothing
    watches for incoming payments, and the answer says so.
53. **History was rewritten on 19 November, on your instruction.** You asked for the Claude
    co-author line to be removed from GitHub. That line was in the message of every commit, so
    all 25 commits on this branch and the one on `main` were replaced by copies with the same
    files, author and dates and no such line, and both branches were force-pushed. This is the
    one exception to "do not force-push, rewrite history, or push to main". Every commit id
    changed; the ids in this log were updated to the new ones. Commits from here on carry no
    co-author line, and the PR description no longer has an attribution footer.
54. **Grok Bot routines, as they really work.** Checked on 19 November against xAI's
    documentation and your signed-in grok.com (looked at, nothing changed). A routine is made
    by asking a Bot in chat; there is no API or CLI. Its webhook is called with the routine's
    own key as a bearer token, which the server did not send; it does now, from the new
    setting `GROK_MISSED_CALL_WEBHOOK_KEY`, to that address only and only over https.
    `bots/routines.md` was rewritten to match, and has the bot token saved on the Bot as a
    secret instead of pasted into the routine text.
55. **The Grok Bot was set up by chat, in Chrome.** There is no API or CLI, so I created the
    `Switchboard` Bot on grok.com and sent it the routine texts from `bots/routines.md` as
    messages, with the server's address filled in and the token referred to only by the name
    of its secret. I typed no token anywhere; the Bot asked for it with a secret card, which
    is left for you. One difference from the plan's four routines: research on demand is a
    skill, not a routine.

## P1 integration

The vault contract is wired into the server. With `USE_P1_STUBS=true` (the default) nothing
changes.

**Real with `USE_P1_STUBS=false`:** `app/chain/vault.py` (`ChainVault`, web3.py, satisfies
`VaultPort`, still wrapped by `HoldingVault`) and `app/chain/stream.py` (the QuickNode Stream
webhook at `/api/hooks/quicknode`: HMAC check, gzip cap, vault logs published as
`approval.requested`, `approval.resolved` and `frozen`). `ChainVault` publishes no vault events
itself; the webhook does, so nothing appears twice. The one event it does publish is
`policy.updated`, because the contract has no event for a policy change. **Still stubs, logged
at start-up:** transfers, markets, anchor.

**Switch:** set `USE_P1_STUBS=false`, `VAULT_ADDRESS`, `ARC_RPC_URL`, `OWNER_PRIVATE_KEY`,
`AGENT_PRIVATE_KEY`, `APPROVER_PRIVATE_KEY` and `QUICKNODE_STREAM_SECRET` (also
`ARC_CHAIN_ID`, default 5042002, and `USDC_ADDRESS`). The server refuses to start if the vault
address, the RPC URL or a key is blank. The ABI is read from `contracts/out`, so run
`cd contracts && forge build` once. Use a fresh `DATABASE_PATH` when switching: the contract's
request numbers start at 1, like the stub's, and an old approval or plan row with the same
number would be matched to the new request. Hold ids start with `h`, so they never meet the
contract's.

**Limits:**
- A payee that is not an EVM address (Solana) cannot be a vault payee, so the vault pays the
  agent's own address, which funds the cross-chain leg. The per-payee allowlist is not enforced
  onchain for these until the Phase 2 transfer module exists. The agent address must be
  allowed as a payee onchain for such a spend to be permitted without a call.
- The contract cannot tell who asked, so `via` on `freeze` and `deny` is dropped, and events
  from the Stream carry `via="chain"`.
- The allowlist the dashboard sees is the wallet book, with each EVM entry checked on the
  contract; entries that are not EVM addresses are kept in memory only.
- A spend asked while frozen opens a request onchain (reason 4). Nothing rings and nothing is
  published; it stays pending until someone expires it.
- Stream deliveries are de-duplicated by transaction and log index in memory. `ctx.last_stream_delivery`
  records the last one, for the Phase 4 dead man's switch (not built).
- Tests: `tests/test_stream.py` (no chain) and `tests/test_chain_vault.py` (anvil, marked
  `anvil`, skipped when `anvil` is not on PATH; put `~/.foundry/bin` there).
