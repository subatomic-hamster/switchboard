// Hand-written shapes for demo mode (docs/demo-mode.md). The generated api-types.ts does not know them.
import type { IntentResponse } from "./types";

export type DemoVenue = "Polymarket" | "Polymarket US" | "Gemini";

export interface Adjacent {
  venue: string;
  title: string;
  symbol: string;
  /** Null when the side held could not be told apart in the Gemini event. */
  price: number | null;
}

/** The linked Polymarket account (pretend-linked in the demo). */
export interface LinkedAccount {
  venue: string;
  name: string;
  wallet: string;
  profile_url: string;
  linked: boolean;
}

export interface DemoPosition {
  id: string;
  venue: DemoVenue;
  slug: string;
  title: string;
  side: string;
  contracts: number;
  avg_price: number;
  price: number;
  value_usd: number;
  pnl_usd: number;
  ends_at: string | null;
  status: "open" | "won" | "lost" | "moved";
  url: string;
  adjacent?: Adjacent | null;
}

export interface DemoOffer {
  venue: string;
  headline: string;
  rebate_pct: number;
  expires_at: string;
}

export interface Portfolio {
  account: LinkedAccount | null;
  positions: DemoPosition[];
  offer: DemoOffer | null;
  balances: { place: string; usd: number }[];
}

export interface Suggestion {
  label: string;
  text: string;
  why: string;
}

export interface InsightCheck {
  field: string;
  same: boolean;
  detail: string;
}

export interface Insight {
  verdict: "go" | "against" | "neutral";
  headline: string;
  body: string;
  model: string;
  source: "grok" | "fallback";
  checks: InsightCheck[];
  alternative: string | null;
}

export type RecentKind = "send" | "receive" | "bet" | "move" | "swap" | "win";

export interface RecentAction {
  ts: string;
  kind: RecentKind;
  title: string;
  detail: string;
  amount_usd: number | null;
  asset: string | null;
  from: string | null;
  to: string | null;
}

export interface MarketBuy {
  venue: string;
  slug: string;
  title: string;
  side: string;
  amount_usd: number;
  price: number;
  contracts: number;
  fee_usd: number;
  from_place: string;
  ends_at: string | null;
}

/** An intent answer, which may also be a market buy. */
export type Plan = Omit<IntentResponse, "kind"> & {
  kind: IntentResponse["kind"] | "market_buy";
  market_buy?: MarketBuy | null;
};

export interface DemoUpdatedEvent {
  id: number;
  ts: string;
  type: "demo.updated";
  data: { title: string; detail: string };
}
