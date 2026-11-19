import type { ApprovalView, IntentResponse, StatsResponse, SwitchboardEvent } from "./types";

let nextId = 1;

/** Build an event with a fresh id. Typed, so a wrong payload fails the type check. */
export function event<E extends SwitchboardEvent>(
  type: E["type"],
  data: E["data"],
  ts = "2026-11-19T12:00:00+00:00",
): E {
  return { id: nextId++, type, ts, data } as E;
}

export const transferPlan: IntentResponse = {
  plan_id: "p_1",
  kind: "transfer",
  request: {
    amount_usd: 200,
    asset: "USDC",
    to_asset: "USDC",
    source: {
      label: "my Ethereum wallet",
      chain: "eth-sepolia",
      address: "0x1111111111111111111111111111111111111111",
      known: true,
    },
    destination: {
      label: "Sam's Solana wallet",
      chain: "solana-devnet",
      address: "Sam11111111111111111111111111111111111111111",
      known: true,
    },
  },
  market_move: null,
  routes: [
    {
      id: "cheapest",
      cost_usd: 0.3,
      eta_s: 915,
      steps: [
        {
          rail: "cctp-standard",
          from_chain: "eth-sepolia",
          to_chain: "arc-testnet",
          cost_usd: 0.25,
          eta_s: 900,
          description: "Circle CCTP standard transfer, Ethereum to Arc",
        },
        {
          rail: "cctp-standard",
          from_chain: "arc-testnet",
          to_chain: "solana-devnet",
          cost_usd: 0.05,
          eta_s: 15,
          description: "Circle CCTP standard transfer, Arc to Solana",
        },
      ],
    },
    {
      id: "fastest",
      cost_usd: 0.6,
      eta_s: 20,
      steps: [
        {
          rail: "cctp-fast",
          from_chain: "eth-sepolia",
          to_chain: "solana-devnet",
          cost_usd: 0.6,
          eta_s: 20,
          description: "Circle CCTP fast transfer, Ethereum to Solana",
        },
      ],
    },
  ],
  spoken_quote:
    "Sending 200 dollars to Sam's Solana wallet. Cheapest route is 30 cents and about 15 minutes. Fastest is 60 cents and about 20 seconds. It is inside your limits. Go ahead?",
  preview: { outcome: "pay", reason: null, detail: "Within your limits." },
  parser: "rules",
};

export const stats: StatsResponse = {
  policy: {
    transfer_cap_usd: 200,
    daily_budget_usd: 500,
    allowlist: ["0x1", "0x2"],
    approval_expiry_s: 300,
  },
  spent_today_usd: 200,
  budget_remaining_usd: 300,
  approvals: { total: 0, approved: 0, denied: 0, rate_pct: null, warning: "" },
  calls_today: 0,
  max_calls_per_day: 5,
  frozen: false,
  stream_stale: false,
  trading_enabled: false,
  max_order_usd: 2,
  calls_live: false,
  using_p1_stubs: true,
  passkey_threshold_usd: 1000,
  passkey_registered: false,
};

export const pendingApproval: ApprovalView = {
  request_id: "7",
  plan_id: "p_2",
  amount_usd: 250,
  payee_label: "new address ending 4f2c",
  reason: "OVER_TRANSFER_CAP",
  status: "pending",
  challenge: "harbor",
  expires_at: "2099-01-01T00:00:00+00:00",
  requires_passkey: false,
  voice_ok: false,
  passkey_ok: false,
  case_for: 'You asked for this yourself: "send 250 to the new address".',
  case_against: "It is 50 dollars over your 200 dollars transfer limit.",
  call_status: "started",
  source: "user",
};
