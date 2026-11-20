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

## Needs you

1. `git push -u origin p1/money`.
2. Add to `.env` (a hook stops me writing it):
   `LEDGER_ADDRESS=0x75375b072Eea338C1AE65941Dfc4E9567aB13569` and
   `ESCROW_ADDRESS=0x12b1820011873d8E23A6679Ec26e9D61b4D2523D`, and add both names (blank) to
   `.env.example`. Until then I pass them as process env vars when starting the server.
3. `PUBLIC_BASE_URL` in `.env` points at an old quick tunnel, so claim links use a dead host. Set
   it to the current tunnel (or a named tunnel) before demoing claim links.

## How to run

```
cd contracts && forge build && forge test     # forge build also produces the ABI the server reads
cd server && uv run pytest -q                  # anvil tests run when ~/.foundry/bin is on PATH
cd server && uv run python -m app              # real vault when USE_P1_STUBS=false
cloudflared tunnel --url http://127.0.0.1:8787 # then set the Stream destination to <url>/api/hooks/quicknode
```
