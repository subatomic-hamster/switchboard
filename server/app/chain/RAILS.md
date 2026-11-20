# Rails: what is real, what was checked

Researched 2026-11-20 from Circle and Arc documentation, plus read-only calls (fee API GETs, Arc
RPC `eth_call` and `eth_getCode`). No transaction was sent. "Verified" means read from a primary
source or observed in a read-only call. "Inferred" means concluded, not observed.

## Findings

| # | Finding | Status | Source |
|---|---|---|---|
| 1 | CCTP V2 supports Arc as source and destination. Arc is domain **26**. Ethereum (Sepolia) is domain **0**, Solana (devnet) is **5**. | Verified | https://developers.circle.com/cctp/concepts/supported-chains-and-domains |
| 2 | Arc Testnet TokenMessengerV2 `0x8FE6B999Dc680CcFDD5Bf7EB0974218be2542DAA`, MessageTransmitterV2 `0xE737e5cEBEEBa77EFE34D4aa090756590b1CE275`. Ethereum Sepolia uses the same two addresses. | Verified | https://developers.circle.com/cctp/references/contract-addresses and https://docs.arc.io/arc/references/contract-addresses |
| 3 | Solana devnet programs: MessageTransmitterV2 `CCTPV2Sm4AdWt5296sk4P66VBZ7bEhcARwFaaS9YPbeC`, TokenMessengerMinterV2 `CCTPV2vPZJS2u2BBsUoscuikbYjnpFmbFsvVuJdgUMQe`. (The page's two IDL links name them the other way round from its table; the server does not call these programs, so it makes no difference here.) | Verified | https://developers.circle.com/cctp/references/solana-programs |
| 4 | USDC: Arc `0x3600000000000000000000000000000000000000` (ERC-20 view, 6 decimals; native gas is the same balance at 18). Sepolia `0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238`. Solana devnet mint `4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU`. | Verified | https://developers.circle.com/stablecoins/usdc-contract-addresses , https://docs.arc.io/arc/references/contract-addresses |
| 5 | Arc testnet: chain id 5042002, RPC `https://rpc.testnet.arc.io` (a read-only call confirmed the id and that the USDC contract returns 6 decimals). | Verified | https://docs.arc.io/arc/references/rpc-endpoints |
| 6 | Iris sandbox: `GET https://iris-api-sandbox.circle.com/v2/messages/{sourceDomain}?transactionHash=...` (404 until Circle has the message), and `GET /v2/burn/USDC/fees/{src}/{dst}[?forward=true][&includeRecipientSetup=true]`. No key. Message `status` is `complete` or `pending_confirmations`; with forwarding, `forwardState` (`PENDING`, `COMPLETE`), `forwardTxHash`, `destinationMintTxHash`. | Verified (fee API called live; messages API shape from Circle's OpenAPI, 404 body observed) | https://developers.circle.com/api-reference/cctp/all/get-messages-v2 |
| 7 | **Arc has no Fast Transfer as a source**: "standard attestation is already fast". Standard from Arc attests after 1 block (about 0.5 s). Use `minFinalityThreshold = 2000` from Arc. Fast (`1000`) exists from Ethereum (about 20 s) and Solana (0.4 to 0.6 s). | Verified | https://developers.circle.com/cctp/concepts/finality-and-block-confirmations |
| 8 | Standard transfers are free at the protocol level. Fast fee is 1 bp from Ethereum or Solana (fee API). From Arc the standard fee is `minimumFee: 0` (fee API, 26 to 0 and 26 to 5). `maxFee` is a cap; if the real fee exceeds it the burn reverts. | Verified | https://developers.circle.com/cctp/concepts/fees ; live GET 2026-11-20 |
| 9 | **Forwarding Service exists and works on testnet** (the how-to uses the sandbox Iris): put `cctp-forward` hook data in `depositForBurnWithHook`; Circle signs the attestation and broadcasts the mint, so the recipient and the sender pay no destination gas. Arc, Ethereum and Solana are all listed as forwarding destinations. Fee is paid in USDC out of the burn: forwardFee.med quoted live at 0.050 to 0.060 USDC to Ethereum, 0.214 to Solana (0.106 if the token account exists). | Verified from docs and fee API; **not yet run end to end** (needs a live burn) | https://developers.circle.com/cctp/concepts/forwarding-service , https://developers.circle.com/cctp/howtos/transfer-usdc-with-forwarding-service |
| 10 | Solana `mintRecipient` must be the recipient's USDC **associated token account**, not the wallet. Hook data v0 with flag 1 + owner asks Circle to create the account. `app/chain/transfer.py` derives it without a Solana library; checked against `@solana/spl-token` for three owners. | Verified | https://developers.circle.com/cctp/concepts/forwarding-service ; `tests/test_chain_transfer.py` |
| 11 | The server's ABI for `depositForBurn` and `depositForBurnWithHook` matches the deployed contract: a read-only `eth_call` to the Arc testnet TokenMessenger reaches the USDC `transferFrom` and reverts only on "transfer amount exceeds allowance" for both signatures. | Verified (read-only) | `eth_call` on https://rpc.testnet.arc.io |
| 12 | **Gateway** supports Arc Testnet (`arcTestnet`, domain 26) and Sepolia, with forwarding. Model: deposit USDC into GatewayWallet (Arc testnet `0x0077777d7EBA4688BDeF3E311b846F25870A19B9`), sign an EIP-712 burn intent, POST to the Gateway API, mint via GatewayMinter. No transfer fee when Arc is source or destination (0.5 bp elsewhere), plus a flat burn fee and, with forwarding, a forwarding fee. Solana has no testnet name in the table. Needs a deposit and a Gateway API client. | Verified from docs; not built | https://developers.circle.com/gateway/references/supported-blockchains , https://developers.circle.com/gateway/references/fees , https://developers.circle.com/crosschain-fees |
| 13 | **Gateway does not fit better** for this build: an extra deposit step and a second API client for the same result CCTP gives with one burn, and Solana testnet is not in its table. Worth revisiting for a standing unified balance. | Inferred | as above |
| 14 | **StableFX is permissioned.** "Designed for financial institutions"; API key comes from "your Circle representative" (Hub account; sandbox `hub-sandbox.circle.com`). `POST https://api-sandbox.circle.com/v1/exchange/stablefx/quotes` without a key returns 401 (observed). Trades need a maker via the API, a Permit2 allowance, and EIP-712 signatures. FxEscrow on Arc testnet: `0xd68256f4D69C6BbEcB873D8588AE0Dc6B8E22E10`; there is no key-free onchain path. | Verified | https://developers.circle.com/stablefx , https://developers.circle.com/stablefx/howtos/get-api-key , https://developers.circle.com/stablefx/quickstarts/fx-trade-taker |
| 15 | EURC on Arc testnet: `0x89B50855Aa3bE2F677cD6303Cec089B5F319D72a` (6 decimals). Testnet EURC from https://faucet.circle.com (choose Arc Testnet, EURC). | Verified | https://docs.arc.io/arc/references/contract-addresses |
| 16 | **A USDC to EURC swap with no new account exists:** Arc App Kit `swap()` works on Arc testnet ("Swap is available on mainnet only, with the exception of Arc testnet"), USDC to EURC accepted on Arc, optional Circle API key, 2 bp provider fee from a third-party liquidity service, thin testnet liquidity (a swap can pass estimation and revert; raise slippage above 300 bp). It is a JavaScript SDK (`@circle-fin/app-kit`). Whether its provider is StableFX is not stated. | Verified from docs; not run | https://docs.arc.io/app-kit/swap , https://docs.arc.io/app-kit/concepts/swap-fees , https://docs.arc.io/app-kit/tutorials/swap/estimate-swap-rate |
| 17 | Whether a forward whose recipient Solana token account already exists is harmed by also requesting account creation (flag 1). | **Unknown** | not stated in the docs. `transfer.py` always requests creation for Solana; fee 0.214 instead of 0.106. |
| 18 | Time from attestation to the forwarded mint. | **Unknown** (no live run) | The planner's 30 s is an estimate. |

## Money flow (decision)

```
Arc:   vault --redeem--> agent EOA --approve + depositForBurn(WithHook)--> TokenMessengerV2
Dest:  Circle's Forwarding Service --receiveMessage--> recipient (Sepolia address, Solana token account)
```

- The vault lives on Arc and pays only on Arc. A payee address on another chain cannot be a vault
  payee, and an Ethereum Sepolia address looks exactly like an Arc address (the vault cannot tell
  them apart), so `VaultPort.request_spend` gained an optional `chain` argument. For any chain
  other than Arc, `ChainVault` asks the vault to pay **the agent's own address**; otherwise
  USDC would land on Arc at an address the user meant on Sepolia. `HoldingVault` keeps the chain
  on a held request, so approving it later pays the same way. Arc payees are paid directly as before.
- Arc to Arc: the vault already paid the payee (`redeem`, or the `approve` transaction), so
  `CctpTransfers.start` only publishes `transfer.sent` and `transfer.arrived` (no hash: the port
  gives the module no redeem hash). Nothing is paid twice.
- Amounts: the vault pays the agent exactly `amount_usd`. The agent burns `amount_usd + maxFee`
  where `maxFee = forwardFee.med + protocol fee`, quoted from the fee API just before the burn
  (Circle: fees move, never hardcode). The fee comes out of the agent's own float, so the
  recipient is owed the full amount and the vault's budget counts only `amount_usd`. The agent
  wallet therefore needs a small float of Arc USDC (also its gas). Per Circle's how-to the fees
  are deducted at the mint; if the real fee is below `maxFee` the recipient gets slightly more
  than `amount_usd` (inferred, not observed).
- A burn that fails after the vault paid the agent (RPC down, revert) raises out of
  `Planner.execute`; the USDC stays in the agent wallet. Nothing is burned twice.
- Only Arc-origin rails are live. The vault and the agent are on Arc, so a route from Sepolia or
  Solana cannot be run. With `USE_REAL_TRANSFERS=true` the planner offers only `"live": true`
  rails (`load_rails(live_only=True)`), and EURC is not offered (StableFX is blocked). Without it
  the planner and the stubs behave as before.
- `POST /api/chain/transfer` (the frozen direct route) returns 403 when transfers are real: it
  skips the vault, so it would burn the agent wallet's USDC with no limit or approval.
- Onchain allowlist: the contract checks the payee it is given, now the agent address, so the
  owner must run `setPayeeAllowed(agent, agent, true)` once. Per-payee limits for cross-chain
  payees are therefore enforced by the server's mirror and the planner preview, not by the
  contract. Documented in the `ponytail` comment in `ChainVault._request_spend`.
- Restarts: a transfer in flight when the server restarts is not resumed (the watch is an
  in-memory task). The burn is onchain and Circle forwards regardless; only the
  `transfer.arrived` event would be missed. Upgrade path: on startup, re-poll ledger rows whose
  status is `sent`.

## What runs live today with only Arc USDC

| Route | Runs? | Why |
|---|---|---|
| Arc to Arc | Yes | Vault pays the payee. No agent gas needed beyond the vault calls |
| Arc to Ethereum Sepolia | **Yes, expected** | Burn on Arc (agent pays gas in Arc USDC). Circle forwards the mint, no Sepolia ETH. Not yet run end to end |
| Arc to Solana devnet | **Yes, expected** | Same, with a real Solana owner address (the wallet book's `Sam111...` is not a valid address and is refused). Circle creates the token account. Not yet run end to end |
| Sepolia or Solana to Arc | No | Needs gas and USDC on the source chain; faucets need a human |
| `CCTP_FORWARD=false` mint on Sepolia | No | Needs Sepolia ETH on the agent key. Code path exists and is unit-tested with a mocked mint |
| Self-mint on Solana | Not built | Needs a Solana client and SOL; forwarding covers it. The transfer stops at `transfer.attested` |
| USDC to EURC | No | StableFX is permissioned (unblock below) |

## StableFX: blocked, exact unblock

1. Ask a Circle representative (sales@circle.com) for StableFX access; sign in at
   https://hub-sandbox.circle.com, Developers, API Keys, create a key with StableFX Read and write.
2. Set `STABLEFX_API_KEY`. `chain/fx.py` then quotes from the real endpoint (`dry_run=False`)
   instead of the public ECB mid-rate (api.frankfurter.dev).
3. Grant the agent wallet a USDC allowance to Permit2 (`0x000000000022D473030F116dDEE9F6B43aC78BA3`).
4. Implement `StableFx.execute`: sign the quote's EIP-712 typed data, `POST /v1/exchange/stablefx/trades`,
   then the funding presign and signature steps (taker quickstart above). Not written: it cannot
   be tested without a key.

Alternative with no account: Arc App Kit Swap (finding 16), driven by a small Node script.

## Events

`transfer.sent` now carries `tx_hash` (the Arc burn, absent for Arc to Arc). New, additive:
`transfer.attested` (`TransferAttestedData`: burn hash, `forwarding`, `detail`). Published once
when Circle has signed the burn, again with a reason if forwarding fails or the wait runs out
(30 minutes, `CCTP_TIMEOUT_S`), or if nothing here can mint. `transfer.arrived` carries the real
mint hash (`destinationMintTxHash`).

## Settings

`USE_REAL_TRANSFERS` (default false; needs `USE_P1_STUBS=false`), `CCTP_FORWARD` (default true),
`IRIS_URL` (default the sandbox), `CCTP_POLL_INTERVAL_S` (3), `CCTP_TIMEOUT_S` (1800),
`ETH_SEPOLIA_RPC_URL` and `ETH_SEPOLIA_CHAIN_ID` (only for `CCTP_FORWARD=false`),
`STABLEFX_API_KEY` (blank = dry-run quotes). `.env.example` was not edited (a hook blocks it).

## Rail figures

`server/fixtures/rails.json`: each rail has `cost_usd`, `eta_s`, `live`, `basis`
(`quoted`/`documented`/`estimate`) and `source`. Fee figures are Circle's fee API on 2026-11-20 and
will drift; the transfer module re-quotes before every burn. Estimated, not measured: the
forward-and-mint time (30 s), Sepolia and Solana native transfer cost, and the 5 bp StableFX fee.
