import type { Chain, Reason, SwitchboardEvent } from "./types";

export type Tone = "good" | "warning" | "critical" | "neutral";

export const CHAIN_NAMES: Record<Chain, string> = {
  "eth-sepolia": "Ethereum",
  "solana-devnet": "Solana",
  "arc-testnet": "Arc",
};

export const REASON_TEXT: Record<Reason, string> = {
  OVER_TRANSFER_CAP: "over the per-transfer limit",
  OVER_DAILY_BUDGET: "over the daily budget",
  PAYEE_NOT_ALLOWED: "a destination not paid before",
  FROZEN: "the vault is frozen",
  STREAM_STALE: "the event stream is stale",
  CALL_CAP_REACHED: "the daily limit on approval calls was reached",
  OVER_AGENT_LIMIT: "more than that assistant may send without asking",
  TRADING_DISABLED: "trading is switched off",
  OVER_ORDER_CAP: "over the order size limit",
  NOT_EQUIVALENT: "the two markets are not the same bet",
  UNSUPPORTED: "not supported",
};

const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const usdWhole = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

export function money(amount: number): string {
  return Number.isInteger(amount) ? usdWhole.format(amount) : usd.format(amount);
}

export function duration(seconds: number): string {
  if (seconds < 90) return `${Math.max(1, Math.round(seconds))} s`;
  return `${Math.round(seconds / 60)} min`;
}

export function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export interface EventLine {
  tone: Tone;
  /** A short word shown with the status mark, so state is never carried by color alone. */
  label: string;
  title: string;
  detail: string;
  /** The reason code, when the event has one. */
  code?: Reason;
}

/** One feed line for an event. All wording for the feed lives here. */
export function describe(event: SwitchboardEvent): EventLine {
  switch (event.type) {
    case "transfer.sent": {
      const d = event.data;
      return {
        tone: "good",
        label: "Sent",
        title: `${money(d.amount_usd)} to ${d.payee_label || CHAIN_NAMES[d.to_chain]}`,
        detail: `${CHAIN_NAMES[d.from_chain]} to ${CHAIN_NAMES[d.to_chain]}${
          d.route_id ? `, ${d.route_id} route` : ""
        }${d.eta_s ? `, about ${duration(d.eta_s)}` : ""}`,
      };
    }
    case "transfer.arrived":
      return {
        tone: "good",
        label: "Arrived",
        title: `${money(event.data.amount_usd)} on ${CHAIN_NAMES[event.data.to_chain]}`,
        detail: event.data.transfer_id,
      };
    case "approval.requested": {
      const d = event.data;
      return {
        tone: "warning",
        label: "Needs approval",
        title: `${money(d.amount_usd)} is waiting for your say`,
        detail: `Request ${d.request_id}: ${REASON_TEXT[d.reason]}`,
        code: d.reason,
      };
    }
    case "approval.resolved":
      return event.data.approved
        ? {
            tone: "good",
            label: "Approved",
            title: `Request ${event.data.request_id} approved`,
            detail: event.data.via ? `by ${event.data.via.replaceAll("_", " ")}` : "",
          }
        : {
            tone: "critical",
            label: "Denied",
            title: `Request ${event.data.request_id} denied`,
            detail: event.data.via ? `by ${event.data.via.replaceAll("_", " ")}` : "",
          };
    case "blocked": {
      const d = event.data;
      return {
        tone: "critical",
        label: "Blocked",
        title:
          d.amount_usd != null
            ? `${money(d.amount_usd)}${d.payee_label ? ` to ${d.payee_label}` : ""} was stopped`
            : "A spend was stopped",
        detail: d.detail || REASON_TEXT[d.reason],
        code: d.reason,
      };
    }
    case "trade.posted":
    case "trade.filled": {
      const d = event.data;
      return {
        tone: "neutral",
        label: event.type === "trade.posted" ? "Order posted" : "Order filled",
        title: `${d.side === "sell" ? "Sell" : "Buy"} ${d.qty} at ${d.price.toFixed(2)} on ${d.venue}`,
        detail: `${d.slug}${d.dry_run ? " (dry run, no order placed)" : ""}`,
      };
    }
    case "stream.stale":
      return event.data.stale
        ? {
            tone: "critical",
            label: "Stream stale",
            title: "The event stream went quiet",
            detail: "Spending is paused until it recovers.",
            code: "STREAM_STALE",
          }
        : {
            tone: "good",
            label: "Stream live",
            title: "The event stream recovered",
            detail: "Spending can resume.",
          };
    case "frozen":
      return event.data.frozen
        ? {
            tone: "critical",
            label: "Frozen",
            title: "The vault is frozen",
            detail: event.data.via ? `by ${event.data.via}` : "",
            code: "FROZEN",
          }
        : {
            tone: "good",
            label: "Unfrozen",
            title: "The vault is live again",
            detail: event.data.via ? `by ${event.data.via}` : "",
          };
    case "call.started":
      return {
        tone: "warning",
        label: event.data.dry_run ? "Call (dry run)" : "Calling you",
        title: `Approval call for request ${event.data.request_id}`,
        detail: event.data.detail,
      };
    case "call.ended":
      return {
        tone: event.data.status === "ended" ? "neutral" : "critical",
        label: "Call ended",
        title: `Call for request ${event.data.request_id}: ${event.data.status.replaceAll("_", " ")}`,
        detail: event.data.detail,
      };
    case "policy.updated": {
      const d = event.data;
      return {
        tone: "neutral",
        label: "Policy changed",
        title: `Limit ${money(d.transfer_cap_usd)} per transfer, ${money(d.daily_budget_usd)} a day`,
        detail: d.rule_text ? `"${d.rule_text}"${d.via ? `, by ${d.via}` : ""}` : d.via,
      };
    }
    case "forecast.committed":
      return {
        tone: "neutral",
        label: "Forecast sealed",
        title: `Opinion on ${event.data.slug} hashed before the event`,
        detail: `${event.data.commitment.slice(0, 18)}…`,
      };
    case "forecast.revealed": {
      const d = event.data;
      return {
        tone: "neutral",
        label: "Forecast revealed",
        title: `${d.slug}: said ${d.probability != null ? `${Math.round(d.probability * 100)}%` : "?"}, outcome ${
          d.outcome == null ? "unknown" : d.outcome ? "yes" : "no"
        }`,
        detail: d.brier != null ? `Brier score ${d.brier.toFixed(3)}` : "",
      };
    }
    case "escrow.updated": {
      const d = event.data;
      const label = { deposited: "Held for claim", claimed: "Claimed", refunded: "Refunded" }[
        d.status
      ];
      return {
        tone: d.status === "claimed" ? "good" : "neutral",
        label,
        title: `${money(d.amount_usd)} in escrow ${d.escrow_id}`,
        detail: d.to ? `to ${d.to}` : "",
      };
    }
    case "payment.updated": {
      const d = event.data;
      return {
        tone: d.status === "paid" ? "good" : "neutral",
        label: d.status === "paid" ? "Request paid" : "Payment requested",
        title: `${money(d.amount_usd)} request ${d.request_id}`,
        detail: d.memo,
      };
    }
  }
}
