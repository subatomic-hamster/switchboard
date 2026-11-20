# Switchboard

Tell it where your money should go, in plain speech, and an agent moves it across wallets, chains and prediction markets. A contract enforces the limits, and your phone rings when something needs your say.

![Home](docs/screenshots/01-home.png)

Switchboard is our entry in the University Blockchain Conference 2026 Hackathon. You type, speak or call in a sentence like "Send 150 dollars from my Ethereum wallet to Sam's Solana wallet". The server parses it, prices the routes, and asks a vault contract on Arc to pay. The agent can ask, but only the vault can spend. Anything over your limits becomes an ElevenLabs phone call where you say the amount and a one-time word from your screen.

The same sentence box runs prediction markets. "Move my Rockets position to Gemini" first checks in code that the two venues list the same bet. It then prices both legs with fees and asks four AI models for a second opinion. The forecast is committed on-chain before the game.

## On the dashboard

React and Vite, served by the server. Every screen updates live from the server's event stream.

| | |
| --- | --- |
| ![Bet Down on BTC 5m](docs/screenshots/02-bet-down-5m-quote.png) | ![Send quote](docs/screenshots/03-send-routes-quote.png) |
| **One sentence, one bet.** "Place a bet on DOWN on the current BTC up or down 5m market" resolves to the five-minute window in progress, priced from the live Polymarket US book. | **Cross-chain send.** Ethereum to Sam's Solana wallet, with the cheapest and fastest routes, fee and arrival time. Whether it pays, asks or blocks is known before you confirm. |
| ![Markets](docs/screenshots/07-markets-match-and-forecast.png) | ![Activity](docs/screenshots/05-activity.png) |
| **Moving a position.** Five checks that it is the same bet on both venues, two ways to sell with fees, a bid-depth check, and four model families reasoning from the game's injury report. Each forecast has a receipt hash committed before the event. | **Activity.** Every event in order. The ring shows where money went around the vault on Arc, and the flight recorder replays the day. |
| ![Controls](docs/screenshots/08-controls-and-injection-test.png) | ![Research](docs/screenshots/06-research-rockets-go.png) |
| **Controls.** Limits, health (vault, event stream, passkey) and a prompt-injection test. Hostile page text goes to a reader with no tools, and the panel shows the worst case if the agent itself were hijacked. | **Research.** The verdict comes from code: same bet, and the cost after a venue rebate. Grok writes the explanation. |

Also on the dashboard:

- A microphone. ElevenLabs speech-to-text fills the sentence box, and text-to-speech reads quotes back.
- Rules by voice: "lower my daily limit to 300" is parsed by fixed patterns and read back before it applies.
- A freeze button that stops all spending at once.
- "No wallet? Send a link": escrow on Arc with a claim link.
- Connected assistants (ChatGPT, Claude, Grok, Gemini) over MCP. Each gets its own key and limit, and none can approve or change limits.

## On the phone

Voice runs on ElevenLabs.

- **Approval calls.** When the vault holds a request, your phone rings. The operator states the request, a risk officer gives one objection, and you approve by saying the exact amount and the one-time word shown only on your screen. "Yes" is not an approval. Three wrong tries, hanging up, or the expiry all deny. Saying "freeze" stops everything.
- **Calling in** (demo mode). Call the number and ask: "Send 150 dollars to Sam's Solana wallet", "Put 30 dollars into the Bitcoin hourly market", "Add 20 dollars to my Rockets position", "Move my Rockets position to Gemini". The agent reads back the quote or the verdict and runs it only after a yes, then tells you what you now hold.

## Architecture

![System architecture](docs/diagrams/system-architecture.png)

- **Server.** Python 3.12, FastAPI, one process. `intent.py` turns a sentence into a request with one Claude call, cross-checked by a rule-based parser. `planner.py` prices routes and decides pay, ask or block.
- **Vault on Arc testnet.** `Vault.sol` holds the money and enforces a per-transfer cap, a rolling 24-hour budget, a payee allowlist and a freeze. The agent key can only request a spend and redeem a single-use permit.
- **Cross-chain.** Circle CCTP V2, burning on Arc, with Circle's Forwarding Service minting on Ethereum Sepolia or Solana devnet.
- **QuickNode Stream.** Vault events arrive as HMAC-signed webhooks with a heartbeat. If it goes quiet for 90 seconds, spending stops.
- **Markets.** Live public order books from Polymarket US and Gemini. The match, fees and verdict are code; Grok (via OpenRouter) explains them, and Claude, GPT, Gemini and Grok forecast.
- **Ledger and Escrow.** `Ledger.sol` stores forecast receipts and trade records as commit-reveal hashes. `Escrow.sol` holds claim links and payment requests.

Every money path goes through four ports in `server/app/ports.py`. Each has an in-memory stub (the default) and a real module chosen by settings. More diagrams (request lifecycle, the approval call, the market move, calling in, CCTP) are in [docs/architecture.md](docs/architecture.md).

## Security

![Security layers](docs/diagrams/security-layers.png)

- **The contract is the authority.** The server predicts the vault's answer so it can quote it, but the vault decides. No server code can loosen a limit.
- **Models explain, code decides.** The parser sees only your sentence and cannot invent an amount or an address. Verdicts, amounts and approvals are deterministic.
- **Outside text cannot spend.** Web pages, market descriptions and bot notes only reach code with no access to money, and are shown as unverified.
- **Extra reasons to ask, never to pay.** An anomaly baseline (odd amount, new payee, burst, odd hour) and per-assistant limits can turn a "pay" into an "ask", never the reverse.
- **A small public surface.** The dashboard answers only on localhost. The few public routes (webhooks, bot and assistant APIs, claim links) each check their own credential, and cross-site requests are refused.
- **No real orders.** `TRADING_ENABLED` is false and the order size has a hard $2 ceiling in code. Market moves are built, signed with a dummy key and logged, never sent.

## On Arc testnet

| Contract | Address |
| --- | --- |
| Vault | [`0xf04A1A72848cD816A0E22D3aF62fc5d4288d4524`](https://testnet.arcscan.app/address/0xf04A1A72848cD816A0E22D3aF62fc5d4288d4524) |
| Ledger | [`0x75375b072Eea338C1AE65941Dfc4E9567aB13569`](https://testnet.arcscan.app/address/0x75375b072Eea338C1AE65941Dfc4E9567aB13569) |
| Escrow | [`0x12b1820011873d8E23A6679Ec26e9D61b4D2523D`](https://testnet.arcscan.app/address/0x12b1820011873d8E23A6679Ec26e9D61b4D2523D) |

Checked live on testnet:

- Transfers from Arc to Arc, to Ethereum Sepolia and to Solana devnet, through the vault and CCTP V2.
- An escrow claim link, a claim, and a refund after expiry.
- A forecast committed to the Ledger before tip-off.
- The Stream's dead man's switch blocking spends within 90 seconds of being paused.

Testnet USDC has no value, and no real money is involved anywhere. Hashes and details are in [OVERNIGHT_LOG_P1.md](OVERNIGHT_LOG_P1.md) and [server/app/chain/RAILS.md](server/app/chain/RAILS.md).

## Repo layout

```
server/app/            FastAPI server: intent, planner, voice, markets desk, demo mode
server/app/chain/      vault, Stream, watchdog, anomaly, CCTP transfers, escrow, ledger anchor
server/app/markets/    Polymarket US and Gemini clients, five-field match, quotes, moves
server/tests/          pytest suite, including adversarial and live-marked tests
web/                   dashboard (React + Vite + TypeScript)
contracts/             Vault, Ledger and Escrow (Foundry)
bots/                  Grok Bot routines
docs/                  architecture, demo mode, demo runbook, plans, screenshots
```

## Local development

Needs [uv](https://docs.astral.sh/uv/), Node 20 or later and pnpm.

```sh
cp .env.example .env                       # every integration defaults to dry run or fixtures
cd server && uv sync && uv run python -m app          # server on http://127.0.0.1:8787
cd web && pnpm install && pnpm dev                    # dashboard on http://localhost:5173
cd server && uv run python -m app.demo_mock --fast    # replays a full day of events
```

With `.env` copied unchanged, nothing needs a key: the vault, transfers and venues are in-memory stubs. Every setting is in `server/app/config.py`. For the real vault on Arc, build the contracts (`cd contracts && forge build`), set `USE_P1_STUBS=false` with the vault address, RPC URL and the three role keys, and point the QuickNode Stream at `<tunnel>/api/hooks/quicknode`.

## Demo mode

```sh
cd server && DEMO_MODE=true uv run python -m app
```

Demo mode is a scripted stage with simulated money and live market data. It uses its own database, raised limits, and no orders. The script, from a $30 Bitcoin bet to moving the Rockets position and sending $150 to Sam, is in [docs/demo-mode.md](docs/demo-mode.md).

## Tests

```sh
cd server && uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy
cd web && pnpm test && pnpm typecheck
cd contracts && forge test
```

- **Server.** 480+ pytest tests. Some need Foundry's `anvil` on `PATH`; `-m live` hits the real public venue APIs.
- **Web.** Vitest suites.
- **Contracts.** Foundry tests for all three contracts.

## Documentation

- [docs/architecture.md](docs/architecture.md): all diagrams, with what is real and what is simulated
- [docs/demo-mode.md](docs/demo-mode.md): the demo stage, its script, calling in, and its API
- [docs/demo-runbook.md](docs/demo-runbook.md): the demo beat by beat, with pre-flight checks
- [docs/product-plan.md](docs/product-plan.md), [docs/implementation-plan.md](docs/implementation-plan.md): what we set out to build and how
- [server/app/chain/RAILS.md](server/app/chain/RAILS.md): CCTP, Gateway and StableFX research, with sources
- [bots/routines.md](bots/routines.md): Grok Bot routines
- [OVERNIGHT_LOG.md](OVERNIGHT_LOG.md), [OVERNIGHT_LOG_P1.md](OVERNIGHT_LOG_P1.md): build logs for each half, with live checks and hashes
