import { render, screen, within } from "@testing-library/react";

import { EventFeed } from "./components/EventFeed";
import { mergeEvents } from "./events";
import { collapseBlips, describe as describeEvent, duration, explorerLink, money } from "./format";
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
    expect(line.title).toBe("Sell 4 at 46¢ on Polymarket US");
    expect(line.detail).toBe("Practice: no order placed");
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
    expect(within(items[1]!).getByText("$200 leaving Ethereum for Solana")).toBeInTheDocument();
    expect(
      within(items[1]!).getByText("to Sam's Solana wallet, fastest route, about 20 s"),
    ).toBeInTheDocument();
  });
});

const EVM = `0x${"ab".repeat(32)}`;
const SIG = "5".repeat(88);

describe("explorer links", () => {
  it("maps chain and hash to an explorer, and refuses stub or malformed hashes", () => {
    expect(explorerLink("arc-testnet", EVM)?.url).toBe(`https://testnet.arcscan.app/tx/${EVM}`);
    expect(explorerLink("eth-sepolia", EVM)?.url).toBe(`https://sepolia.etherscan.io/tx/${EVM}`);
    expect(explorerLink("solana-devnet", SIG)?.url).toBe(
      `https://explorer.solana.com/tx/${SIG}?cluster=devnet`,
    );
    expect(explorerLink("arc-testnet", "0xstubt_1")).toBeNull();
    expect(explorerLink("eth-sepolia", EVM.slice(0, 60))).toBeNull();
    expect(explorerLink("solana-devnet", EVM)).toBeNull(); // 0x is not base58
    expect(explorerLink("solana-devnet", "5".repeat(20))).toBeNull();
    expect(explorerLink("arc-testnet", null)).toBeNull();
  });

  it("links sent to the source chain, attested to Arc, arrived to the destination", () => {
    const sent = event<EventOf<"transfer.sent">>("transfer.sent", {
      transfer_id: "t_1", from_chain: "arc-testnet", to_chain: "solana-devnet", amount_usd: 1,
      payee_label: "", route_id: "", eta_s: 0, tx_hash: EVM,
    });
    const attested = event<EventOf<"transfer.attested">>("transfer.attested", {
      transfer_id: "t_1", to_chain: "solana-devnet", amount_usd: 1, tx_hash: EVM, forwarding: true, detail: "",
    });
    const arrived = event<EventOf<"transfer.arrived">>("transfer.arrived", {
      transfer_id: "t_1", to_chain: "solana-devnet", amount_usd: 1, tx_hash: SIG,
    });
    const stub = event<EventOf<"transfer.sent">>("transfer.sent", {
      transfer_id: "t_2", from_chain: "arc-testnet", to_chain: "eth-sepolia", amount_usd: 1,
      payee_label: "", route_id: "", eta_s: 0, tx_hash: "0xstubt_1",
    });
    render(<EventFeed events={[sent, attested, arrived, stub]} />);
    const links = screen.getAllByRole("link");
    expect(links.map((a) => a.getAttribute("href"))).toEqual([
      `https://explorer.solana.com/tx/${SIG}?cluster=devnet`,
      `https://testnet.arcscan.app/tx/${EVM}`,
      `https://testnet.arcscan.app/tx/${EVM}`,
    ]);
    expect(links[0]).toHaveAttribute("target", "_blank");
    expect(links[0]).toHaveAttribute("rel", "noopener noreferrer");
    expect(links[0]).toHaveTextContent("View on Solana Explorer");
  });
});

describe("cross-chain wording", () => {
  it("reads sent, attested, arrived as one story", () => {
    const sent = describeEvent(
      event<EventOf<"transfer.sent">>("transfer.sent", {
        transfer_id: "t_1", from_chain: "arc-testnet", to_chain: "eth-sepolia", amount_usd: 1,
        payee_label: "", route_id: "", eta_s: 0,
      }),
    );
    expect(sent.title).toBe("$1 leaving Arc for Ethereum");
    expect(sent.detail).toBe("");
    const attested = describeEvent(
      event<EventOf<"transfer.attested">>("transfer.attested", {
        transfer_id: "t_1", to_chain: "eth-sepolia", amount_usd: 1, tx_hash: "", forwarding: true, detail: "",
      }),
    );
    expect(attested.title).toBe("Circle confirmed the burn; minting on Ethereum");
    expect(attested.detail).not.toContain("t_1");
    const arrived = describeEvent(
      event<EventOf<"transfer.arrived">>("transfer.arrived", {
        transfer_id: "t_1", to_chain: "eth-sepolia", amount_usd: 1, tx_hash: "",
      }),
    );
    expect(arrived.title).toBe("$1 arrived on Ethereum");
    expect(arrived.detail).toBe("");
  });
});

describe("stream blips", () => {
  const stale = (value: boolean, ts: string) =>
    event<EventOf<"stream.stale">>("stream.stale", { stale: value, seconds_since_delivery: 0 }, ts);

  it("hides a stale/recovered pair within a minute, keeps a real outage", () => {
    const blip = [stale(true, "2026-11-19T12:00:00+00:00"), stale(false, "2026-11-19T12:00:30+00:00")];
    expect(collapseBlips(blip)).toEqual([]);
    const outage = [stale(true, "2026-11-19T12:00:00+00:00"), stale(false, "2026-11-19T12:05:00+00:00")];
    expect(collapseBlips(outage)).toHaveLength(2);
    const open = [stale(true, "2026-11-19T12:00:00+00:00")];
    expect(collapseBlips(open)).toBe(open);
    // Two restarts with no recovery between: one row, the first.
    const twice = [stale(true, "2026-11-19T12:00:00+00:00"), stale(true, "2026-11-19T12:20:00+00:00")];
    expect(collapseBlips(twice)).toEqual([twice[0]]);
  });

  it("the feed shows nothing for a restart blip", () => {
    render(
      <EventFeed
        events={[stale(true, "2026-11-19T12:00:00+00:00"), stale(false, "2026-11-19T12:00:20+00:00")]}
      />,
    );
    expect(screen.getByText(/Nothing yet/)).toBeInTheDocument();
  });
});
