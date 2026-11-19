# Overnight log: P2 (Voice and Surface)

Branch `p2/voice-surface`. Kept current as the work goes. Newest state is at the top of each
section.

## How to run

```
# once
cp .env.example .env            # then fill in what you have; blank values mean dry-run
cd server && uv sync
cd ../web && pnpm install

# server (port 8787)
cd server && uv run python -m app

# dashboard (port 5173, proxies /api and /events to the server)
cd web && pnpm dev

# checks
cd server && uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy
cd web && pnpm test && pnpm typecheck

# regenerate web/src/api-types.ts after changing schemas or routes
cd server && uv run python -m scripts.gen_api_types
```

## Needs you

_(filled in as items come up)_

## Done

| Task | Commit |
|---|---|

## Blocked

| Item | Why | Exact step that unblocks it |
|---|---|---|

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
5. **P1 – who starts the call.** The plan says "the webhook starts the call". P1's hook only
   publishes `approval.requested`; `voice.py` subscribes on the bus and starts the call, so
   neither side imports the other.
6. **New modules.** P2 modules beyond the plan's list live directly under `server/app/`
   (`config.py`, `context.py`, `ports.py`, `wallets.py`, and later ones) and `server/scripts/`.
   Nothing of P2's is under `chain/` or `markets/` except the two clearly marked `stub.py`
   files.
7. **Extra event types.** Five additive types for P2 features: `call.started`, `call.ended`,
   `policy.updated`, `forecast.committed`, `forecast.revealed`.
8. **Stub quote formula.** The stub's `move_cost_pct` is the full taker cost on both sides
   over the sale value (11.3% for the plan's NBA example). The plan's example figure of 6.1%
   leaves out the sell-side fee; P1 owns the real formula.
9. **Kalshi, Limitless, Phantom.** Listed by `/api/venues` and in the dashboard as `planned`,
   "not connected yet". They are not described as supported anywhere, because the README must
   carry an honest live-versus-sandboxed table. To change the wording, edit `VENUES` in
   `server/app/main.py`.
10. **Models.** Intent parser: Anthropic `claude-opus-5-5` at low effort. Forecast: four
    families (Anthropic, OpenAI, Google, xAI); details in the forecast entry below when built.
    The Cursor key is not a model-provider key and is not used or sent anywhere.
