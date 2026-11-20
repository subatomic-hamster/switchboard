import type { Chain, Reason, SwitchboardEvent } from "./types";

export type Tone = "good" | "warning" | "critical" | "neutral";

export const CHAIN_NAMES: Record<Chain, string> = {
  "eth-sepolia": "Ethereum",
  "solana-devnet": "Solana",
  "arc-testnet": "Arc",
};

const VENUE_NAME: Record<string, string> = { polymarket: "Polymarket US", gemini: "Gemini" };

export const REASON_TEXT: Record<Reason, string> = {
  OVER_TRANSFER_CAP: "over the per-transfer limit",
  OVER_DAILY_BUDGET: "over the daily budget",
  PAYEE_NOT_ALLOWED: "a destination not paid before",
  FROZEN: "the vault is frozen",
  STREAM_STALE: "the event stream is stale",
  CALL_CAP_REACHED: "the daily limit on approval calls was reached",
  OVER_AGENT_LIMIT: "more than that assistant may send without asking",
  UNUSUAL_SPEND: "unlike this agent's usual spending",
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
      const route = [d.route_id && `${d.route_id} route`, d.eta_s && `about ${duration(d.eta_s)}`]
        .filter(Boolean)
        .join(", ");
      return {
        tone: "good",
        label: "Sent",
        title: `${money(d.amount_usd)} leaving ${CHAIN_NAMES[d.from_chain]} for ${CHAIN_NAMES[d.to_chain]}`,
        detail: [d.payee_label && `to ${d.payee_label}`, route].filter(Boolean).join(", "),
      };
    }
    case "transfer.attested":
      return {
        tone: "neutral",
        label: "Attested",
        title: `Circle confirmed the burn; minting on ${CHAIN_NAMES[event.data.to_chain]}`,
        detail:
          event.data.detail ||
          (event.data.forwarding ? "Circle will submit the mint." : "Waiting for the mint."),
      };
    case "transfer.arrived":
      return {
        tone: "good",
        label: "Arrived",
        title: `${money(event.data.amount_usd)} arrived on ${CHAIN_NAMES[event.data.to_chain]}`,
        detail: "",
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
        title: `${d.side === "sell" ? "Sell" : "Buy"} ${d.qty} at ${Math.round(d.price * 100)}¢ on ${
          VENUE_NAME[d.venue] ?? d.venue
        }`,
        detail: d.dry_run ? "Practice: no order placed" : d.slug,
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
            detail: event.data.seconds_since_delivery
              ? `Spending was paused for ${duration(event.data.seconds_since_delivery)}.`
              : "Spending can resume.",
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
        title: "Opinion sealed before the event",
        detail: event.data.slug,
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

const EVM_HASH = /^0x[0-9a-fA-F]{64}$/;
const SOLANA_SIG = /^[1-9A-HJ-NP-Za-km-z]{64,90}$/;

/** Explorer link for a real transaction hash on `chain`; null for stub or malformed hashes. */
export function explorerLink(
  chain: Chain,
  hash: string | null | undefined,
): { name: string; url: string } | null {
  if (!hash) return null;
  if (chain === "solana-devnet") {
    return SOLANA_SIG.test(hash)
      ? { name: "Solana Explorer", url: `https://explorer.solana.com/tx/${hash}?cluster=devnet` }
      : null;
  }
  if (!EVM_HASH.test(hash)) return null;
  return chain === "arc-testnet"
    ? { name: "Arcscan", url: `https://testnet.arcscan.app/tx/${hash}` }
    : { name: "Etherscan", url: `https://sepolia.etherscan.io/tx/${hash}` };
}

/** The explorer link for an event: sent is on the source chain, attested is the Arc burn, arrived is the mint. */
export function eventExplorerLink(event: SwitchboardEvent) {
  switch (event.type) {
    case "transfer.sent":
      return explorerLink(event.data.from_chain, event.data.tx_hash);
    case "transfer.attested":
      return explorerLink("arc-testnet", event.data.tx_hash);
    case "transfer.arrived":
      return explorerLink(event.data.to_chain, event.data.tx_hash);
    default:
      return null;
  }
}

/**
 * One row per stream outage. A stale/recovered pair within a minute is the watchdog on a server
 * restart, so both go. A longer outage keeps only the recovery row, whose
 * seconds_since_delivery is rewritten to the outage length. An open outage keeps its stale row.
 */
export function collapseBlips(events: SwitchboardEvent[], windowMs = 60_000): SwitchboardEvent[] {
  const drop = new Set<number>();
  const paused = new Map<number, number>();
  let open: SwitchboardEvent | null = null;
  for (const e of events) {
    if (e.type !== "stream.stale") continue;
    if (e.data.stale) {
      // A second "went quiet" with no recovery between (a server restart) says nothing new.
      if (open) drop.add(e.id);
      else open = e;
    } else {
      if (open) {
        const gap = Date.parse(e.ts) - Date.parse(open.ts);
        drop.add(open.id);
        if (gap <= windowMs) drop.add(e.id);
        else paused.set(e.id, Math.round(gap / 1000));
      }
      open = null;
    }
  }
  if (!drop.size) return events;
  return events
    .filter((e) => !drop.has(e.id))
    .map((e) =>
      e.type === "stream.stale" && paused.has(e.id)
        ? { ...e, data: { ...e.data, seconds_since_delivery: paused.get(e.id) ?? 0 } }
        : e,
    );
}
