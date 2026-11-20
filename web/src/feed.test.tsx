import { render, screen, within } from "@testing-library/react";

import { EventFeed } from "./components/EventFeed";
import { mergeEvents } from "./events";
import { describe as describeEvent, duration, money } from "./format";
import { event } from "./test-fixtures";
import type { EventOf } from "./types";

describe("mergeEvents", () => {
  it("orders by id, drops duplicates and keeps the same array when nothing is new", () => {
    const a = event<EventOf<"frozen">>("frozen", { frozen: true, via: "voice" });
    const b = event<EventOf<"frozen">>("frozen", { frozen: false, via: "dashboard" });
    const merged = mergeEvents([b], [a, b]);
    expect(merged.map((item) => item.id)).toEqual([a.id, b.id]);
    expect(mergeEvents(merged, [a])).toBe(merged);
    expect(mergeEvents(merged, [])).toBe(merged);
  });
});

describe("format", () => {
  it("writes money and durations the way the feed shows them", () => {
    expect(money(200)).toBe("$200");
    expect(money(0.4)).toBe("$0.40");
    expect(money(1250.5)).toBe("$1,250.50");
    expect(duration(20)).toBe("20 s");
    expect(duration(960)).toBe("16 min");
  });

  it("carries the reason code on blocked and approval events", () => {
    const blocked = describeEvent(
      event<EventOf<"blocked">>("blocked", {
        reason: "FROZEN",
        plan_id: "p_1",
        amount_usd: 200,
        payee_label: "Sam's Solana wallet",
        detail: "The vault is frozen.",
      }),
    );
    expect(blocked.tone).toBe("critical");
    expect(blocked.code).toBe("FROZEN");
    expect(blocked.title).toBe("$200 to Sam's Solana wallet was stopped");

    const requested = describeEvent(
      event<EventOf<"approval.requested">>("approval.requested", {
        request_id: "7",
        agent: "0xa",
        payee: "0xb",
        amount_usd: 250,
        reason: "OVER_TRANSFER_CAP",
      }),
    );
    expect(requested.tone).toBe("warning");
    expect(requested.code).toBe("OVER_TRANSFER_CAP");
    expect(requested.detail).toBe("Request 7: over the per-transfer limit");
  });

  it("marks dry-run trades as dry runs", () => {
    const line = describeEvent(
      event<EventOf<"trade.filled">>("trade.filled", {
        move_id: "m_1",
        venue: "polymarket",
        slug: "aec-nba-hou-den-2026-11-20",
        side: "sell",
        qty: 4,
        price: 0.46,
        dry_run: true,
      }),
    );
    expect(line.title).toBe("Sell 4 at 0.46 on polymarket");
    expect(line.detail).toContain("dry run, no order placed");
  });
});

describe("EventFeed", () => {
  it("explains itself when empty", () => {
    render(<EventFeed events={[]} />);
    expect(screen.getByText(/Nothing yet/)).toBeInTheDocument();
  });

  it("shows newest first, with a status label and the reason code", () => {
    const sent = event<EventOf<"transfer.sent">>("transfer.sent", {
      transfer_id: "t_1",
      plan_id: "p_1",
      from_chain: "eth-sepolia",
      to_chain: "solana-devnet",
      amount_usd: 200,
      payee_label: "Sam's Solana wallet",
      route_id: "fastest",
      eta_s: 20,
    });
    const blocked = event<EventOf<"blocked">>("blocked", {
      reason: "STREAM_STALE",
      plan_id: "p_2",
      amount_usd: 50,
      payee_label: "Sam's Arc wallet",
      detail: "The event stream has gone quiet.",
    });
    render(<EventFeed events={[sent, blocked]} />);
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(within(items[0]!).getByText("Blocked")).toBeInTheDocument();
    expect(within(items[0]!).getByText("STREAM_STALE")).toBeInTheDocument();
    expect(within(items[1]!).getByText("Sent")).toBeInTheDocument();
    expect(within(items[1]!).getByText("$200 to Sam's Solana wallet")).toBeInTheDocument();
    expect(
      within(items[1]!).getByText("Ethereum to Solana, fastest route, about 20 s"),
    ).toBeInTheDocument();
  });
});
