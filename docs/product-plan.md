# Switchboard: product plan

Switchboard is a money router driven by speech, with a safety layer called Ringfence: a vault contract that enforces limits and an approval phone call. The build spec is in `implementation-plan.md`.

"Switchboard" is a working name: you call, and a set of operators connect your money.

## One sentence

**Tell it where you want your money to go, in plain speech, and a crew of agents moves it across wallets, chains, banks and prediction markets, inside limits a contract enforces, phoning you when something needs your say.**

## Entry

| | |
|---|---|
| Main track | Agentic Payments and AI |
| Chain award | Best use of Arc |
| Bounties | Gemini Prediction Markets, QuickNode Streams, ElevenLabs, Grok Bot |
| Cash ceiling | $8,500 (Track $2,000, Arc $1,000, Gemini 1st $2,500, QuickNode $500, Official Winner 1st $2,500) |

## What the user experiences

1. **Say it.** "Move two hundred dollars from my Ethereum wallet to Sam's Solana wallet." Or: "Move my Fed position from Polymarket to Gemini."
2. **Hear a quote.** An operator answers with the route, the cost, and the time. "Cheapest route is forty cents and about twenty seconds. Go ahead?"
3. **It happens.** Inside your limits, it just executes. You watch the money hop on screen.
4. **Or the phone rings.** Above a limit, or to a destination it has not seen before, it calls you. Two voices give the case for and against. You decide.
5. **It reports back.** Every move is logged with a reason, and a daily summary is spoken or posted.

## The operators

Each is a voice you can talk to. Grok Bots do the research and monitoring; deterministic code moves the money.

| Operator | Handles | Speaks when |
|---|---|---|
| Chains | Wallet-to-wallet moves across Ethereum, Solana and Arc; dollar-to-euro conversion | Quoting or confirming a transfer |
| Bank | On-ramp from a bank account (sandbox) | Funding or cashing out |
| Markets | Prices and positions across Gemini, Polymarket and Kalshi | Comparing venues, advising on a move |
| Risk officer | Argues against the proposed action | Any call for approval |

## Rails, by how real they are

Be explicit about this in the demo and README. Honesty about what is live is worth more than a longer list.

| Tier | Rail | How |
|---|---|---|
| Live on testnet | Ethereum wallet ↔ Solana wallet ↔ Arc wallet, in USDC | Circle CCTP and Gateway |
| Live on testnet | USDC ↔ EURC | Circle StableFX on Arc |
| Live on testnet | Payment request to a friend; escrow for someone with no wallet | Arc contract plus Circle Wallets |
| Sandbox, labelled | Bank → wallet | A provider's sandbox; real use needs identity checks |
| Read-only, live data | Prices and order books on Gemini, Polymarket, Kalshi | Public market-data APIs |
| Real money, small amounts | Position move between prediction venues | Needs your own verified accounts. See "Open question" |

Cut: bank-to-bank transfers (no blockchain involved), and giving any Grok Bot the ability to move funds.

## The route planner

The core of the product, and what makes it "intelligent" in a way you can show.

- Every rail is a path between two places with a cost and a time.
- The planner finds the cheapest route and the fastest route and quotes both before doing anything. StablePay won a Capital One prize with this approach.
- It checks the quoted price against your budget before asking the vault for permission.
- Every decision resolves to one of three outcomes with a stated reason: pay, ask, or block.

## Moving a prediction-market position

Positions cannot be transferred between venues. A "move" is a sale on one and a purchase on the other.

### Step 1: find the equivalent market

- A Grok Bot searches the second venue for the same event, using X search for context.
- Code then compares five fixed fields: what the event is, the threshold, the direction, the event time, and who decides the outcome.
- If any field differs, the move is refused and the reason is spoken. This will be common. One study found only 13 of 46,360 related market pairs were truly equivalent.

### Step 2: quote the move

"Selling here gets you 61 cents. Buying there costs 66. With fees, moving costs you 9 percent." This is arithmetic from two order books, not a prediction.

### Step 3: the operator's opinion

- **"Will I get a bid?"** is answered from order-book depth and spread. It is a fact.
- **"I'd hold this"** is a forecast. It is given as a second opinion, with the operator's track record beside it, and you can override it.
- The forecast uses several models from different families and starts from the market price. Similarly tuned models share errors, so two copies of one model would agree for the same wrong reason.

### Step 4: execute

This step runs live with small real amounts on the team's own funded accounts.

**The build: hold a balance on both venues.**

1. Post the ask on the first venue and watch for the fill over its WebSocket.
2. The moment it fills, buy on the second venue from the balance already there.
3. Queue a cash rebalance and tell the user what is owed where.

The position moves in seconds and the cash settles later.

**Why this is the path to build first.** The venue docs show that trading is fully API-driven everywhere but moving cash is not:

| Venue | Orders by API | Fill notifications | Withdraw by API | Deposit address by API | Test environment |
|---|---|---|---|---|---|
| Kalshi | Yes | WebSocket `fill` channel | No, history only | No | Yes, with mock funds |
| Polymarket US | Yes | WebSocket private channel | Not found | Not found | None for retail |
| Polymarket international | Yes | WebSocket user channel | Yes, onchain | Yes | By request |
| Gemini | Yes | WebSocket `orders@account` | Crypto only, to pre-approved addresses | Yes | Yes; prediction markets unconfirmed |

- **Kalshi and Polymarket US** document no way to start a withdrawal by API. Polymarket US also holds card and ACH deposits from withdrawal for 3 to 4 business days.
- **Gemini** requires each withdrawal address to be approved first, and a new approval starts a 7-day waiting period. Request the address approval early so API withdrawals become available.
- **Polymarket international** is fully programmable, but its own geoblock endpoint lists the US as close-only and says orders from blocked regions are rejected. The plan does not use it from the US.

So the cash leg is shown as a queued instruction ("$40 owed from Kalshi to Gemini; withdraw in the app") rather than executed. Say this plainly in the demo. Your original sequence (sell, withdraw, deposit, rebuy) stays in the README as the production design once withdrawals can be automated.

**Other notes from the docs:**

- Kalshi orders go to `POST /portfolio/events/orders`; `ask` sells a Yes position. Its demo environment has mock funds, so the order and fill logic can be tested without real money.
- Gemini orders go to `POST /v1/prediction-markets/order`, and the prediction-market terms must be accepted by API first.
- On Gemini, accounts with certain multi-leg No positions cannot sell No directly and must buy Yes instead. Handle that case or refuse it.
- Polymarket's fee schedule changes periodically; read the current one from its docs.

### What is actually listed on both venues

A snapshot from both public APIs. Prices are bid/ask and are examples only; fetch current markets the same way.

**Market data is public on both, with no key:**

- Gemini: `GET https://api.gemini.com/v1/prediction-markets/events?status=active&limit=50&offset=N`. About 1,000 active events: sports 832, crypto 88, weather 63, commodities 6. No politics or Fed markets were active.
- Polymarket US: `GET https://gateway.polymarket.us/v1/markets` (a different host from the trading API), plus `/v1/markets/{slug}/book` and `/bbo`. About 75,600 active markets, the large majority sports.

**Pairs that match:**

| Event | Gemini | Polymarket US | Gemini price | Polymarket price | Check result |
|---|---|---|---|---|---|
| NBA, Rockets at Nuggets | `GEMI-NBA-2611210300-HOU-DEN-M-HOU` | `aec-nba-hou-den-2026-11-20` | 0.44 / 0.46 | 0.45 / 0.46 | Match |
| La Liga, Atletico v Getafe | `GEMI-LALIGA-2611222000-ATM-GET-M-ATM` | `atc-lal-get-atm-2026-11-22-atm` | 0.62 / 0.63 | 0.62 / 0.63 | Match; both settle on 90 minutes plus stoppage |
| NHL, Avalanche at Utah | `GEMI-NHL-2611190300-COL-UTA-M-COL` | `aec-nhl-col-uta-2026-11-18` | 0.29 / 0.32 | 0.32 / 0.33 | Match, but Gemini's overtime rule was not read |
| PGA, RSM Classic winner | `GEMI-GOLF-RSM-WIN-20261122-BRENNAN` | `tec-pga-rsmclas-2026-11-19-w-micbre` | 0.03 / 0.05 | 0.035 / 0.039 | Match, except on player withdrawal |
| Bitcoin above $63,000 at 5pm ET | `GEMI-BTC2611192200-HI63000` | `cpc-btc-above-day-2026-11-19-63000` | 0.61 / 0.64 | 0.62 / 0.63 | **Fails on source**: Lukka index vs CF Benchmarks |
| Ether above $1,800 at 5pm ET | `GEMI-ETH2611192200-HI1800` | `cpceth-eth-above-day-2026-11-19-1800` | 0.45 / 0.50 | 0.47 / 0.54 | **Fails on source** |

Gemini also lists college football (for example `NCAAF-2611211700-TAM-OU-M`). For a live demonstration, find a game listed on both venues on the day.

**Look-alikes that are not the same bet.** These are the best material for the "I can't move that" moment:

- **San Francisco daily high temperature.** Same weather source, but the temperature bands are offset by one degree (Gemini 56-57, 58-59; Polymarket 55-56, 57-58), so no single contract matches.
- **Bitcoin 5-minute up or down.** Different price source and a different reference price.
- **Gold and oil.** Gemini lists strike ladders on one data provider; Polymarket lists only up-or-down markets on another.
- **NFL team total vs game total on Polymarket.** The question text is identical; only the slug and description differ. A naive text match would get this wrong.

**Differences that apply to every sports pair:** the two venues handle postponed games differently, and each lists an "end date" that is not the event time. Compare on the game start time, not the listed expiry.

**Fees, which decide what a move costs:**

- Polymarket US taker fee: 0.095 x contracts x price x (1 - price). At 50 cents that is about 2.4 cents per contract. Makers pay nothing.
- Gemini taker fee uses the same formula; a docs example uses 0.07, about 1.75 cents per contract at 50 cents. Maker fees were waived under a promotion when checked. The current rate table did not load, so confirm it.
- Posting an ask and waiting for it to fill, as you proposed, makes the sell side a maker order and avoids the taker fee. The quote should show both options: sell now and pay the fee, or post and wait.
- Minimum size is 0.01 contracts on both, so test orders can be a few cents.

## The safety layer (Ringfence)

An agent holding the keys to your wallets and exchange accounts is the first thing a judge will question. This is the answer.

### Vault contract on Arc (Solidity, Foundry)

- Holds USDC. The owner sets the policy; the agent's key can never change it.
- **Policy:** per-transfer cap, daily budget, allowlist of destinations, approval expiry.
- **Single-use permits:** each permit is good for one destination, one amount, once, with a short expiry.
- **Pending requests:** anything outside policy becomes a request and emits an event.
- **Freeze and revoke:** a kill switch for everything, and one for a single agent.
- Trades on Gemini settle on Gemini, not onchain. Their cap is enforced before the order call and each trade is recorded onchain.

Circle's nanopayments need a signature from an ordinary key, so the vault cannot pay directly. The agent's wallet holds a small balance that the vault tops up only by redeeming a permit. Worst case, one top-up is misspent, bounded by the per-transfer cap. Say this in the README.

### The approval call

- A QuickNode Stream watches the vault's events on Arc and posts to a webhook.
- The webhook starts an outbound ElevenLabs call through Twilio, passing the amount, destination and reason.
- The operator makes the case. The risk officer objects. Both speak every time.
- You approve by saying the amount back and a one-time word shown on your screen. "Yes" is not enough. Cloned voices defeat voice recognition, so the system never relies on recognising your voice.
- Above a second threshold, a passkey confirmation on the dashboard is also required.
- No answer means denied. A Grok Bot follows up by message.

### Against rubber-stamping

Research on human oversight finds people drift into approving everything, and that clear explanations make it worse.

- A daily cap on calls.
- The dashboard shows your approval rate and warns at 100%.
- Calls trigger on unusual patterns, not only fixed limits.

### Against prompt injection

The planner fixes the route and amounts from your spoken request before reading anything from outside. The model that reads web pages, market descriptions or vendor data has no ability to move money.

## Where each sponsor tool sits

| Tool | Role |
|---|---|
| Arc | Vault contract, USDC as gas, sub-second finality, escrow for non-users |
| Circle | Wallets for the agent, CCTP and Gateway for cross-chain moves, StableFX for currency, Nanopayments for paid data |
| Gemini | Market data, the equivalence search, order placement, the destination for position moves |
| QuickNode Streams | The event bus: vault events to the call, arrivals to the live map |
| ElevenLabs | Every operator's voice, the outbound approval call, spoken rules |
| Grok Bot | Equivalence research with X search, news watch on open positions, missed-call escalation, daily summary, ledger reconciliation on a schedule |

## Signature moments for the demo

1. **One sentence, money hops.** Speak a cross-chain transfer and watch it travel on screen from Ethereum to Solana in seconds.
2. **The ring.** The vault sits inside a ring. Allowed transfers pass through as particles; blocked ones hit the ring with a flash and a tone.
3. **The call.** A transfer over the limit rings the phone on stage. Two voices, the quote on screen, the reason code for the block.
4. **"Don't move it."** Ask to move a position. The operator finds the equivalent, quotes the cost, and advises against it. Or refuses because the two markets settle differently.
5. **Kill the stream.** Stop the QuickNode Stream and the next transfer is refused until it returns.

Also worth building: let a judge type a malicious instruction and watch the worst outcome be a phone call; replay the whole day on a scrub bar.

## Build order

Build in this order. Each step leaves a working demo.

| Step | Build | Done when |
|---|---|---|
| 1 | Vault contract with tests | Permit, redeem, over-limit, approve, deny, expire and freeze all pass |
| 2 | Voice-commanded wallet-to-wallet transfer across chains | One spoken sentence moves testnet USDC between two chains |
| 3 | Stream to phone call to onchain decision | An over-limit transfer rings the phone and the spoken answer settles it |
| 4 | Route planner with quotes | Cheapest and fastest routes are quoted before execution |
| 5 | Cross-venue price comparison and equivalence check | Two venues' prices shown side by side with a match or a refusal |
| 6 | Dashboard with the ring view | A stranger can follow what happened without narration |
| 7 | Position move | A real position is sold on one venue and bought on another |
| 8 | Grok Bot routines | Daily summary and missed-call escalation run on their own |
| 9 | Bank on-ramp in a sandbox | A labelled sandbox deposit funds the vault |

Step 3 is the first milestone: voice in, money moved, limits enforced, and a human in the loop.

## Demo script

1. **Problem.** Your money sits in five places that do not talk to each other, and the agents that could move it cannot be trusted with the keys.
2. **Simple move.** Speak a cross-chain transfer. It hops on screen.
3. **The call.** Ask for a larger transfer to a new address. The phone rings. Hear both sides. Approve by saying the amount.
4. **Markets.** Ask to move a position. Hear the equivalence check, the cost, and the advice.
5. **Safety.** Kill the stream or freeze by voice. The next transfer fails.

Record steps 2 to 4 cleanly for the Devpost video, with call audio.

## Questions judges will ask

- **"What stops it draining me?"** It cannot sign for the vault. It can only ask the contract, and the contract enforces your limits.
- **"Why would your bot know better than the market?"** It does not claim to. Whether you will get a bid is read from the order book. The forecast is a second opinion with a published track record, and you can override it.
- **"Isn't this Circle Gateway, or River Markets?"** Those are the rails. This is the agent that chooses between them, by voice, under limits.
- **"Isn't this C.E.S.T.A Agent?"** That project (2nd in an Arc track, ETHGlobal Cannes 2026) is voice-first with contract-enforced caps. This one reaches you by phone, routes across venues, and lets you hear both sides.
- **"Can a cloned voice approve a transfer?"** No. Calls go out only to your registered number, approval needs a one-time word from your screen, and large amounts need a passkey.
- **"What is real and what is mocked?"** Point to the rails table.

## README checklist

- Agent decision loop and safety limits.
- Contract addresses and how Arc is used.
- Stream network, dataset, filter logic, destination, and why.
- Which ElevenLabs products and how.
- Grok Bot's role in 3 to 4 sentences, plus a chat export.
- Which Gemini endpoints are used.
- Which rails are live, sandboxed, or read-only.
- That the forecast is not financial advice.

Also required: a new public repo, a working demo, and a 2 to 3 minute video.

## Confirmed from sponsor docs

- QuickNode Streams supports Arc testnet with a webhook destination.
- ElevenLabs can place outbound calls through Twilio with per-call variables, and call authenticated tools on your server.
- Circle developer-controlled wallets, Gateway Nanopayments and the Facilitator Service support Arc testnet. Nanopayments need an ordinary key and a one-time deposit.
- Arc testnet: chain ID 5042002, USDC as gas, finality under one second.
- Gemini's prediction-market data is public; placing orders needs a funded account with Prediction Markets access.
- Grok Bot routines run on a schedule or a webhook and have a cloud computer with a browser.

## Not verified

- Whether Gemini's sandbox exposes prediction-market trading. It does not accept deposits other than test Bitcoin.
- What Gemini's prediction markets use as collateral (press coverage says US dollars) and whether deposited USDC can be used directly.
- Whether you already have an approved withdrawal address on Gemini.
- Which ElevenLabs tier allows telephony, and whether the hackathon coupon covers it.
- Whether one ElevenLabs agent can switch voices mid-call. If not, play the risk officer as a pre-generated clip.
- Which bank on-ramp sandbox is quickest to integrate.
- How to export a Grok Bot chat.

## Open question

You have the accounts, so step 7 is live. The two venues are **Gemini and Polymarket US**.

Polymarket US specifics:

- Keys come from polymarket.us/developer and sign requests with Ed25519 (`X-PM-Access-Key`, `X-PM-Timestamp`, `X-PM-Signature`).
- Orders: `POST https://api.polymarket.us/v1/orders`, with `intent` such as `ORDER_INTENT_SELL_LONG` and `type` `ORDER_TYPE_LIMIT`.
- Fills: `wss://api.polymarket.us/v1/ws/private`. Positions: `GET /v1/portfolio/positions`.
- No test environment for retail accounts, so every test order uses real money. Keep sizes to a dollar or two.
- No documented withdrawal or deposit endpoint, and card or ACH deposits cannot be withdrawn for 3 to 4 business days. The cash leg stays a queued instruction.
- Rate limit: 25 requests per second.

Still needed from you:
- Whether a small balance (for example $20) can sit on each venue for the demo.
- API keys created with trading permission only. Do not give the project a key that can withdraw.
