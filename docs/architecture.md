# Switchboard architecture

Switchboard is an AI money agent. The owner types or speaks a sentence such as "Send 150 dollars from my Ethereum wallet to Sam's Solana wallet", "Move my Rockets position to Gemini" or "Put 30 dollars into the Bitcoin hourly market". The server parses it, plans routes and quotes, checks policy, and only an on-chain vault can spend. The agent can ask. It cannot pay.

Three ideas carry the design:

- **The contract is the authority.** The server predicts the vault's answer so it can quote it. The vault gives the real answer.
- **A human yes is built from things a stranger on the phone cannot supply.** An over-limit spend needs an ElevenLabs voice call where the owner says the amount and a one-time word shown only on their screen, or a passkey.
- **Models explain, code decides.** Language models parse phrasing and word explanations. Verdicts, amounts and approvals come from deterministic code.

Demo mode simulates the money and keeps the market data live. The owner can also phone in and ask the agent to do things.

## 1. System architecture

Rendered: [PNG](diagrams/system-architecture.png) · [SVG](diagrams/system-architecture.svg)

```mermaid
flowchart LR
  subgraph you["You"]
    ui["Dashboard<br/>type or speak"]
    eleven["ElevenLabs<br/>calls and voice"]
    mcp["Other assistants<br/>via MCP"]
  end

  subgraph server["Switchboard server"]
    plan["Intent + planner<br/>Claude parses, code decides"]
    gates["Safety gates<br/>watchdog, anomaly"]
    desk["Markets desk<br/>match, quote, 4-model forecast"]
  end

  subgraph money["Money"]
    vault[("Vault on Arc<br/>limits, approvals")]
    cctp["Circle CCTP<br/>Ethereum, Solana"]
    qn["QuickNode Stream"]
  end

  subgraph mk["Markets"]
    venues["Polymarket US<br/>Gemini"]
  end

  ui --> plan
  eleven --> plan
  mcp --> plan
  ui --> desk
  plan --> gates --> vault --> cctp
  vault -. events .-> qn -.-> gates
  desk --> venues

  classDef chain fill:#ecfdf5,stroke:#10b981,color:#064e3b
  class vault chain
```

The dashboard talks to one FastAPI process. Everything that touches money reaches the vault, the transfer layer, the anchor and the venues through four ports in `server/app/ports.py`. Each port has an in-memory stub (the default) and a real module chosen by settings. QuickNode tells the server what the vault did instead of the server polling, and a silent Stream stops spending. ElevenLabs covers every voice path: the approval call, the call-in line, and the dashboard's speech-to-text and text-to-speech.

## 2. Request lifecycle

```mermaid
sequenceDiagram
  autonumber
  actor Owner
  participant UI as Dashboard
  participant P as Planner
  participant I as Intent parser
  participant V as Vault contract
  participant T as Transfer layer
  participant B as Event bus

  Owner->>UI: "Send 250 from my Arc wallet to Sam's Arc wallet"
  UI->>P: POST /api/intent
  P->>I: parse the sentence
  Note over I: One Claude call, no tools. Amount and address are<br/>cross-checked against a rule-based parse
  I-->>P: amount, source, destination
  P->>P: cheapest and fastest routes, with cost and time
  P->>P: preview: frozen? stream stale? cap? budget? allowlist?
  P-->>UI: plan card and spoken quote with the predicted pay / ask / block
  Owner->>UI: Confirm a route
  UI->>P: POST /api/plans/id/execute
  alt frozen or stream stale
    P->>B: blocked
  else vault asked
    P->>V: requestSpend(payee, amount)
    alt inside policy (pay)
      V-->>P: single-use permit
      P->>V: redeem(permit)
      V-->>T: USDC paid on Arc
      P->>T: start transfer legs
      T->>B: transfer.sent, transfer.attested, transfer.arrived
    else over policy (ask)
      V-->>P: pending request with a deadline
      P->>B: needs approval, see section 4
    end
  end
  B-->>UI: SSE events, stored log feeds the flight recorder
```

The planner fixes the route and the amount from the owner's own sentence before any outside content is read. `preview` predicts the vault's answer so the quote can say what will happen. The vault decides. Every request resolves to **pay** (permit issued), **ask** (`OVER_TRANSFER_CAP`, `OVER_DAILY_BUDGET`, `PAYEE_NOT_ALLOWED`) or **block** (`FROZEN`, `STREAM_STALE`, `CALL_CAP_REACHED`). Everything that happens is published once on the bus, which feeds the dashboard over SSE and the stored log.

## 3. Security layers

Rendered: [PNG](diagrams/security-layers.png) · [SVG](diagrams/security-layers.svg)

```mermaid
flowchart TB
  req(["A request to spend"])
  web(["Web pages, bot notes"])
  reader["Tool-less reader<br/>can flag, cannot spend"]

  g1["1. Parser<br/>only your sentence, checked by rules"]
  g2["2. Server gates<br/>stream alive, anomaly, assistant limits"]
  g3["3. Vault on Arc<br/>cap, daily budget, allowlist, freeze"]
  you["4. You approve<br/>ElevenLabs call: amount + one-time word<br/>passkey above $1,000"]
  ok(["Single-use permit, 10 min"])
  no(["Denied"])

  req --> g1 --> g2 --> g3
  g3 -- pay --> ok
  g3 -- ask --> you
  g3 -- block --> no
  you -- yes --> ok
  you -- "no or timeout" --> no
  web -.-> reader

  classDef good fill:#ecfdf5,stroke:#10b981,color:#064e3b
  classDef bad fill:#fef2f2,stroke:#ef4444,color:#7f1d1d
  class ok good
  class no,reader bad
```

Every layer can only make a spend harder, and none can loosen the vault. Holds and the anomaly baseline can turn a "pay" into an "ask" but never the reverse. The watchdog refuses new spends and also refuses to settle an approval on-chain while the Stream is stale (denials always go through). The agent key cannot change limits, add payees, approve, or unfreeze: those functions do not accept it. The "Try to trick it" box shows the stated bound on a hijacked agent: a phone call the owner can refuse, or at worst one capped transfer to an allowlisted payee.

Defaults differ by build. The stub vault starts at 200 dollars per transfer and 500 per day. The vault deployed on Arc testnet runs 5 and 10. Demo mode raises them to 500 and 2,000 because the money is simulated.

## 4. Voice approval

```mermaid
sequenceDiagram
  autonumber
  participant V as Vault
  participant S as Server
  participant D as Dashboard
  participant E as ElevenLabs voice call
  actor Owner

  V-->>S: ApprovalRequested via the Stream
  S->>S: store request, pick a one-time word, set expiry
  S->>D: show request, both sides of the argument, and the word
  alt call cap reached
    S->>V: deny without calling
  else call placed
    S->>E: outbound call to the registered number
    E->>Owner: states the request and why it needs approval
    Owner->>E: any reply
    E->>S: signed turn (HMAC, under 5 minutes old)
    S-->>Owner: the case for it, then one objection from the risk officer
    Owner->>E: "Two hundred fifty dollars, ocean"
    E->>S: signed turn with the transcript
    S->>S: amount equals pending amount? word equals stored word?
    alt both match and under the passkey threshold
      S->>V: approve (same transaction pays)
      S-->>Owner: Approved
    else both match and above the threshold
      S-->>Owner: confirm with your passkey on the dashboard
    else mismatch
      S-->>Owner: says which one failed and tries left
    end
  end
```

```mermaid
stateDiagram-v2
  [*] --> Pending: ApprovalRequested
  Pending --> Denied: call cap reached
  Pending --> Calling: call placed or dry run
  Calling --> Pending: wrong amount or word, tries left
  Calling --> Denied: three wrong tries
  Calling --> Denied: owner says deny
  Calling --> Frozen: owner says freeze
  Calling --> Denied: call ends without a decision
  Calling --> Denied: more than 12 turns
  Calling --> AwaitingPasskey: amount and word match, above threshold
  AwaitingPasskey --> Approved: passkey assertion verified
  Calling --> Approved: amount and word match
  Pending --> Expired: expiry passes
  Calling --> Expired: expiry passes
  AwaitingPasskey --> Expired: expiry passes
  Frozen --> Denied: request also denied
  Approved --> [*]
  Denied --> [*]
  Expired --> [*]
```

"Yes" is not an approval. The owner must say the exact amount and the word from their screen, and the word is never put in a call variable, a tool response or an event, so someone who only hears the call cannot learn it. The call is scripted: no language model is on it, so nothing said on the line can talk it into anything. Silence, a dropped call, the expiry (5 minutes in demo mode) and an approval attempted while the Stream is stale all end in "not approved". Saying "freeze" needs no word, because stopping is always safe. The dashboard shows the approval rate and warns when every request has been approved.

## 5. Market move pipeline

```mermaid
flowchart TB
  s(["Move my Rockets position to Gemini"])
  i["Intent: kind market_move<br/>market resolved to a slug"]
  m{"Five-field match<br/>event, strike, direction,<br/>event time, settlement source"}
  r["Refuse: name the field that differs<br/>or is unverified"]
  q["Quote both legs from live order books<br/>sell on Polymarket US, taker fee 0.095<br/>buy on Gemini, taker fee 0.07<br/>Gemini rebate in demo mode"]
  vd["Verdict from code<br/>go, against or neutral"]
  g["Grok via OpenRouter words the explanation<br/>fixed text if it fails"]
  own{"Owner confirms the move"}
  eng["MoveEngine guards<br/>min size, order cap, TRADING_ENABLED"]
  dry["Dry run (default)<br/>both legs built and signed with a dummy key<br/>nothing sent"]
  live["Live: sell on Polymarket US,<br/>wait for the fill, buy on Gemini"]
  rec["Hash of the trade record<br/>committed to Ledger.sol"]

  s --> i --> m
  m -- "any field different, or event,<br/>strike, direction or time unknown" --> r
  m -- "match" --> q --> vd --> g --> own
  r --> g
  own -- "yes" --> eng
  eng -- "trading disabled" --> dry --> rec
  eng -- "enabled, keys set, within cap" --> live --> rec
```

A position moves between Polymarket US and Gemini only when the five-field check says it is the same bet. Event, strike, direction and event time must each be known and equal, and a different settlement source blocks the move. An unknown settlement source alone does not block but is noted. The cost is arithmetic over the two order books: fees are summed per book level taken, and a maker route (rest an ask) avoids the sale fee but may not fill. The verdict is made in code. Grok only writes the explanation, and a failed or odd answer is replaced by fixed text. Orders are dry-run unless trading is switched on, and a hard 2 dollar ceiling in code limits any live order. Cash does not move between the venues, so a rebalance instruction is queued instead.

## 6. Calling in (demo mode)

```mermaid
sequenceDiagram
  autonumber
  actor Owner as Caller (the owner)
  participant E as ElevenLabs voice call
  participant S as Server webhook
  participant L as Phone line
  participant P as Planner
  participant D as Demo desk
  participant V as Vault (stub)

  Owner->>E: rings the agent's number
  E->>S: signed turn (HMAC, timestamp checked)
  S->>L: demo mode only, inbound call accepted
  L-->>Owner: "What would you like to do?"
  Owner->>E: "Move my Rockets position to Gemini"
  E->>S: signed turn with the transcript
  S->>L: turn
  L->>P: create_plan(sentence, source phone)
  alt send or buy
    P-->>L: quote and predicted decision
  else move
    L->>D: insight for the held position
    D-->>L: verdict from code, Grok's wording
  end
  L-->>Owner: reads back the quote or verdict and asks "Shall I go ahead?"
  Owner->>E: "Yes"
  E->>S: signed turn
  L-->>Owner: "On it."
  alt send
    L->>P: execute (cheapest route)
    P->>V: request spend, vault limits still apply
  else buy or move
    L->>D: fill the buy, or re-check the match and move
  end
  L-->>Owner: spoken result, such as what is now held and the market's odds
  L-->>Owner: "Anything else?" then "Thank you, goodbye" and hang up
```

The call-in line reuses the same planner as the dashboard's sentence box. A yes is required before anything runs, and "no" cancels. A send over the cap still goes through the usual approval call afterward. Each call-in is accepted only in demo mode, where any caller is treated as the owner because caller ID can be faked and the money is simulated. Outside demo mode an inbound call is refused. The webhook accepts a delivery only with a valid HMAC signature that is under five minutes old.

## 7. Cross-chain transfer (CCTP V2)

```mermaid
sequenceDiagram
  autonumber
  participant P as Planner
  participant V as Vault on Arc
  participant A as Agent wallet on Arc
  participant TM as CCTP TokenMessengerV2
  participant C as Circle Iris and Forwarding Service
  participant D as Destination chain
  participant B as Event bus

  P->>V: requestSpend (chain recorded, pay the agent wallet)
  V-->>P: permit, then redeem
  V->>A: exactly the amount in USDC
  P->>C: fee quote just before the burn
  A->>TM: approve, then depositForBurnWithHook (finality threshold 2000)
  TM-->>B: transfer.sent with the burn hash
  loop poll until attested
    P->>C: GET message by burn hash
  end
  C-->>B: transfer.attested
  C->>D: Forwarding Service submits the mint, no destination gas
  D-->>B: transfer.arrived with the mint hash
```

For an Arc destination the vault pays the payee directly and the transfer layer only publishes `transfer.sent` and `transfer.arrived`. For Ethereum Sepolia or Solana devnet the vault pays the agent wallet exactly the amount, because an Ethereum address looks like an Arc address and the vault cannot tell them apart. The agent then burns the amount plus a quoted forwarding fee, and Circle attests and submits the mint, so neither side needs destination gas. A Solana recipient is addressed by its USDC associated token account, which Circle creates if needed. Both cross-chain routes were run live on testnet from Arc. Routes that start on Sepolia or Solana are not live, and USDC to EURC is blocked behind a permissioned Circle API.

## What is real and what is simulated

| Piece | State |
|---|---|
| Vault, Ledger, Escrow | Deployed on Arc testnet, testnet USDC only |
| Cross-chain transfer | Live on testnet from Arc, behind `USE_REAL_TRANSFERS` |
| Market data | Live from Polymarket US and Gemini |
| Orders | Dry run only: `TRADING_ENABLED` is false and no venue keys are set |
| Demo mode | Stub vault and transfers, live market data, no orders placed |

## Screenshots

Taken from the running app in demo mode (live Polymarket US and Gemini prices, simulated money).

**Home.** One sentence box (typed or spoken). Below it are suggestions built from your own
history and live markets. Positions combine a linked Polymarket account with Polymarket US and
Gemini holdings, at live odds, and show a similar Gemini market where one exists.

![Home](screenshots/01-home.png)

**A market bet from one sentence.** "Place a bet on DOWN on the current BTC up or down 5m market"
resolves to the five-minute window in progress. Down is priced at one dollar less the best Up
bid, and the quote shows the venue fee and a live countdown.

![Bet Down on BTC 5m](screenshots/02-bet-down-5m-quote.png)

**A cross-chain send.** Ethereum to Sam's Solana wallet, with the cheapest and fastest routes
priced. The policy preview (inside the limits, so no call) is decided before anything runs.

![Send quote](screenshots/03-send-routes-quote.png)

**Activity.** Every event in order, from bets and sends to arrivals and position moves. The ring
draws where the money went around the vault on Arc.

![Activity](screenshots/05-activity.png)

**Research verdict.** The verdict comes from code: five checks that it is the same bet, and the
cost after the rebate. Grok writes the explanation.

![Research](screenshots/06-research-rockets-go.png)

**Moving a position.** Polymarket US to Gemini, with 5 of 5 checks matched, two ways to sell
priced with fees, and whether you'd get a bid. Four model families give a second opinion, which
is combined with the market price. Its receipt hash is committed before the event.

![Markets](screenshots/07-markets-match-and-forecast.png)

**Controls and the prompt-injection test.** Limits come from the vault policy. Health covers the
vault, the event stream with its dead man's switch, and the passkey. The fake attack feeds
hostile page text to a tool-less reader, which flags each manipulation tactic. It also shows the
worst case if the agent itself were hijacked: the vault holds the spend and your phone rings.

![Controls](screenshots/08-controls-and-injection-test.png)
