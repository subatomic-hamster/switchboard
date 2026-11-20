# P1 (Money) log

Kept current during the unattended run on branch `p1/money`, which is based on P2's
`p2/voice-surface`. P2's own log is `OVERNIGHT_LOG.md`.

## Live deployment (Arc testnet, chain 5042002)

| What | Address |
|---|---|
| Vault | `0xf04A1A72848cD816A0E22D3aF62fc5d4288d4524` |
| USDC (Arc native ERC-20, 6 decimals) | `0x3600000000000000000000000000000000000000` |
| Owner (burner) | `0x5834D6f0784AaacE5ece312C1e18D04a1CfFc2e8` |
| Agent (burner) | `0x554f14F1Ee51E747fAe8B2095b917b703dDD4198` |
| Approver (burner) | `0x1e5b2b0E5e72601aD56DAC6515503A719ADe8460` |
| Sam's Arc wallet (burner, receive only) | `0x4ccFA9e40f035038edfe5b6A5c5111993B1bEFfA` |
| Ledger (forecast receipts, trade records; agent is a writer) | `0x75375b072Eea338C1AE65941Dfc4E9567aB13569` |
| Escrow (claim links, payment requests) | `0x12b1820011873d8E23A6679Ec26e9D61b4D2523D` |

Agent policy: $5 per transfer, $10 per rolling 24 hours, 600 s approval expiry. Allowlisted
payees: the owner, Sam's Arc wallet, and the agent itself (for cross-chain legs).

QuickNode Stream `switchboard-vault-arc` (id `6c085343-f6e1-4170-b45f-573df028e581`): Arc
Testnet, Logs dataset, JavaScript filter in `server/quicknode/filter.js` (vault logs only, plus a
heartbeat delivery every 60 blocks), webhook destination `<tunnel>/api/hooks/quicknode`, signed
with the Stream's security token (`QUICKNODE_STREAM_SECRET`). Retries every 10 s, terminates
after 10 failed retries (the maximum).

## Done

| Task | Commit |
|---|---|
| Vault contract, 26 Foundry tests, deploy script; deployed and funded on Arc testnet | `5966448` |
| `ChainVault` (VaultPort) and the HMAC-verified Stream webhook wired into P2's server | `426abe2` |
| Live check, 20 Nov: "move 6 dollars from my arc wallet to sam's arc wallet" gave `needs_approval` (OVER_TRANSFER_CAP, request 3 onchain); the Stream delivered `ApprovalRequested`; the dry-run call fired; approval by amount and challenge word paid Sam 6 USDC from the vault | — |
| Ledger + Escrow contracts (commit-reveal claim against front-running), `ChainAnchor`, `ChainEscrow`, `/api/escrow` routes, Stream decoding for both; deployed | `7bdd3ab` |
| Live escrow check: $1 escrow 1 created (`0xdfc8…efbe`), claimed to Sam with the secret (`0xc17d…6a10`, Sam +1 USDC); replayed claim and refund both refused `AlreadySettled`. Escrow 2: refund before expiry refused `NotExpired`, refund after expiry paid back (`0xc059…18fd`) | — |
| Phase 4 safety: dead man's switch (`chain/watchdog.py`: stale until the first heartbeat, stale after 90 s of silence, approvals not settled while stale), anomaly baseline (`chain/anomaly.py`: median/MAD amount, new payee, burst, odd hour, after 8 sends; a flagged spend the vault would allow becomes a phone call with a spoken reason), webhook replay guard, 24 adversarial tests + 3 Foundry tests | `ab21c48` merge |
| Replay guard fix: a delivery that failed mid-handling stays retryable (QuickNode retries with the same nonce; 401 on every retry would terminate the Stream) | `e0a9dd9` |
| Live: server restart publishes `stream.stale` true, first heartbeat publishes false | — |
| **Live dry-run move with an onchain trade record**: `POST /api/desk/move` 2 contracts of the Rockets/Nuggets game (Polymarket US ↔ Gemini, live books, verdict match). Both orders signed with dummy keys and logged "DRY RUN, not sent"; `trade.posted`/`trade.filled` marked `dry_run`; the trade record's hash was committed to the Ledger by the agent (entry 1, tx `0x68b0…5969`) | — |
| **Live dead man's switch**: paused the Stream in QuickNode; `stream.stale` true 71 s later (90 s after the last delivery) and "send 1 dollar … to sam's arc wallet" previewed `block` / `STREAM_STALE`. Resumed: recovered in 9 s and the same sentence previewed `pay` | — |
| Phase 2 cross-chain: `CctpTransfers` (CCTP V2 burn on Arc, Circle Forwarding Service mints on the destination, so no destination gas), Iris attestation watch, `transfer.attested` event, sourced rail fees/ETAs in `fixtures/rails.json`, research in `server/app/chain/RAILS.md`; StableFX written against the API but blocked (permissioned) | `848821c` merge |
| **Live CCTP Arc → Ethereum Sepolia**: "send 1 dollar from my arc wallet to my ethereum wallet". Burn `0xfaf3…e49f` on Arc 06:48:40, attested 06:48:46, forwarded mint `0x668d…cfc2` on Sepolia 06:51:42. Owner's Sepolia USDC 0 → 1.000000 | — |
| **Live CCTP Arc → Solana devnet**: burn `0x5249…6778` (1.214 USDC incl. forward fee and token-account creation), attested and minted 7 s later (`4chE6dAM…nMox6`). "my Solana wallet" holds 1 USDC on devnet | — |
| Wallet book now real burners we control (EVM keys shared Arc/Sepolia, two new Solana devnet keys); tests pinned to the old placeholder book via `WALLETS_PATH` | `e414573`, `24b0e27` |
| Phase 3 markets: Gemini + Polymarket US clients (public data live; HMAC-SHA384 / Ed25519 signing tested against openssl vectors), five-field `match` (refuses Lukka vs CF Benchmarks BTC, offset weather bands, team vs game total), two-book `quote` (Polymarket 0.095, Gemini 0.07 taker), dry-run `move` (partial/unfilled/stranded handled, each move hashed onto the Ledger), `RealMarkets` behind `USE_REAL_MARKETS` | `fbfb51a` merge |
| UI revamp (design canvas https://claude.ai/artifact/Awq4CvtEhsijPMLYgq35zN): three tabs, Freeze in the header, Home = sentence box + three numbers + "Needs you" + activity; Markets and Controls hold the rest, details folded; 44 px targets, aria-live, phone bottom bar | `c7c554f` merge |

## Blocked

| Item | Why | Unblock |
|---|---|---|
| Pushing `p1/money` | The SSH key needs a passphrase and no agent runs in the unattended shell | `git push -u origin p1/money` from your terminal |

## Decisions

1. **Approve pays at once.** `approve` pays in the same transaction (one onchain step for the
   call). P2's flow is approve, then redeem, so `ChainVault` marks that permit prepaid and
   `redeem` skips it.
2. **Approver can freeze, only the owner can unfreeze.** The voice "freeze" uses the approver key.
3. **`PermitIssued` event added** to the four frozen ones: the agent needs its permit id.
4. **Budget counted at permit issue**, not redeem; an unredeemed permit holds budget until it
   leaves the 24-hour window.
5. **Non-EVM payees** (Solana) are paid by the vault to the agent wallet, which funds the
   cross-chain leg. The per-payee allowlist is not enforced onchain for them.
6. **Fresh database when switching to the real vault**: the contract numbers requests from 1, as
   the stub did, and a stub-era row with the same number would swallow a real request.
7. **Stream heartbeat.** The filter sends `{"heartbeat": n}` every 60 blocks (~30 s) so the
   server can tell a quiet vault from a dead Stream (dead man's switch).

8. **Escrow is funded from the agent wallet, not by allowlisting Escrow as a vault payee.** A
   vault payment to Escrow would be a bare transfer with no claim hash, unclaimable and
   unrefundable. The vault funds the agent (already a payee); escrow create and request pay are
   checked against the vault's per-transfer cap in the route.
9. **Ledger accepts sha256 or keccak256 preimages**, because P2's forecast commitments are SHA-256
   over canonical JSON.
10. **Cross-chain money flow**: the vault pays the agent wallet for any payee not on Arc (the
    planner passes the destination chain, since a Sepolia address looks like an Arc one); the
    agent burns `amount + forward fee`, so the recipient gets the full amount and the fee comes
    from the agent's float. Only Arc-origin rails are offered live.
11. **An approval is not settled while the Stream is stale**; a denial always is. Money waits for
    the dead man's switch like every other spend.
12. **Markets refuse on any `different` field and on `unknown` event/strike/direction/time**,
    stricter than the plan: an unverified field could buy the wrong thing.

## Blocked (new)

| Item | Why | Unblock |
|---|---|---|
| USDC → EURC (StableFX) | Permissioned: needs an API key from a Circle rep and a Hub account (unauthenticated POST returns 401) | Request StableFX access; `chain/fx.py` is written against the documented API behind a dry-run flag. The agent already holds 20 EURC on Arc testnet |
| Real prediction-market orders | `TRADING_ENABLED=false` by rule; no venue keys | Out of scope for the unattended run |
| Bank on-ramp | Circle mock wire needs a Circle Mint sandbox account (new account) | Create the sandbox account, then wire it as a rail |

## Needs you

1. `git push -u origin p1/money`.
2. Add to `.env` (a hook stops me writing it):
   ```
   LEDGER_ADDRESS=0x75375b072Eea338C1AE65941Dfc4E9567aB13569
   ESCROW_ADDRESS=0x12b1820011873d8E23A6679Ec26e9D61b4D2523D
   USE_REAL_TRANSFERS=true
   USE_REAL_MARKETS=true
   ```
   and the same names (blank / false) to `.env.example`, plus `CCTP_FORWARD=true`,
   `STREAM_STALE_AFTER_S=90`, `ANOMALY_ENABLED=`, `WALLETS_PATH=`. Until then I pass them as
   process env vars when starting the server.
3. `PUBLIC_BASE_URL` in `.env` points at an old quick tunnel, so claim links use a dead host. Set
   it to the current tunnel (or a named tunnel) before demoing claim links.

## How to run

```
cd contracts && forge build && forge test     # forge build also produces the ABI the server reads
cd server && uv run pytest -q                  # anvil tests run when ~/.foundry/bin is on PATH
cd server && uv run python -m app              # real vault when USE_P1_STUBS=false
cloudflared tunnel --url http://127.0.0.1:8787 # then set the Stream destination to <url>/api/hooks/quicknode
```
