import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { App } from "./App";
import type { DemoPosition, Insight, Portfolio } from "./demo-types";
import { describe as describeEvent } from "./format";
import { stats } from "./test-fixtures";
import type { SwitchboardEvent } from "./types";

class FakeSource {
  static last: FakeSource | null = null;
  readyState = 1;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  private listeners = new Map<string, (message: MessageEvent<string>) => void>();
  constructor() {
    FakeSource.last = this;
  }
  addEventListener(type: string, listener: (message: MessageEvent<string>) => void) {
    this.listeners.set(type, listener);
  }
  close() {}
  emit(type: string, payload: unknown) {
    this.listeners.get(type)?.({ data: JSON.stringify(payload) } as MessageEvent<string>);
  }
}

const inMinutes = (m: number) => new Date(Date.now() + m * 60_000).toISOString();

const rockets: DemoPosition = {
  id: "pos-1",
  venue: "Polymarket US",
  slug: "rockets",
  title: "Will Rockets win against Nuggets in the game?",
  side: "Yes",
  contracts: 100,
  avg_price: 0.5,
  price: 0.62,
  value_usd: 62,
  pnl_usd: 12,
  ends_at: inMinutes(2),
  status: "open",
  url: "",
};

const portfolioBase: Portfolio = {
  account: null,
  positions: [rockets],
  offer: null,
  balances: [],
};

const insight: Insight = {
  verdict: "against",
  headline: "Different index",
  body: "The indexes differ.",
  model: "grok",
  source: "grok",
  checks: [
    { field: "Index", same: false, detail: "BRTI vs Lukka" },
    { field: "Window", same: true, detail: "5 minutes" },
  ],
  alternative: "Next Polymarket window",
};

let demoOn = true;
let portfolio: Portfolio = portfolioBase;
let recent: unknown[] = [];
let suggestions = [{ label: "Buy BTC", text: "Put 30 dollars into Bitcoin", why: "Live window" }];
const posts: string[] = [];
const gets: string[] = [];

beforeEach(() => {
  demoOn = true;
  portfolio = portfolioBase;
  recent = [];
  suggestions = [{ label: "Buy BTC", text: "Put 30 dollars into Bitcoin", why: "Live window" }];
  posts.length = 0;
  gets.length = 0;
  window.location.hash = "";
  vi.stubGlobal("EventSource", FakeSource);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
      if (init?.method === "POST") {
        posts.push(url);
        return json({ ok: true });
      }
      gets.push(url);
      if (url.startsWith("/api/demo/")) {
        if (!demoOn) return json({ detail: "Not found" }, 404);
        if (url.startsWith("/api/demo/suggestions")) return json({ suggestions });
        if (url.startsWith("/api/demo/portfolio")) return json(portfolio);
        if (url.startsWith("/api/demo/recent")) return json({ actions: recent });
        if (url.startsWith("/api/demo/insight")) return json(insight);
      }
      if (url.startsWith("/api/events/history")) return json({ events: [] });
      if (url.startsWith("/api/stats")) return json(stats);
      if (url.startsWith("/api/approvals")) return json({ approvals: [] });
      if (url.startsWith("/api/wallets")) return json({ wallets: [] });
      return json({}, 404);
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

describe("demo mode on Home", () => {
  it("shows the server's suggestions instead of the examples", async () => {
    const user = userEvent.setup();
    render(<App />);
    const chip = await screen.findByRole("button", { name: "Buy BTC" });
    expect(chip).toHaveAttribute("title", "Live window");
    expect(screen.queryByRole("button", { name: "Move my Rockets position" })).not.toBeInTheDocument();
    await user.click(chip);
    expect(screen.getByLabelText("Where should the money go?")).toHaveValue("Put 30 dollars into Bitcoin");
  });

  it("falls back to the static examples, and hides the demo parts, when the route is a 404", async () => {
    demoOn = false;
    render(<App />);
    expect(await screen.findByText("shows what happened.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move my Rockets position" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Your positions" })).not.toBeInTheDocument();
    expect(within(screen.getByRole("main")).getByRole("link", { name: "Activity" })).toHaveAttribute(
      "href",
      "#activity",
    );
  });

  it("renders positions with live odds, a countdown, and flashes when the price changes", async () => {
    render(<App />);
    const odds = await screen.findByLabelText("Odds 62¢");
    expect(odds).not.toHaveClass("flash-up");
    expect(screen.getByText("Polymarket US", { selector: ".position .badge" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Rockets to beat Nuggets" })).toBeInTheDocument();
    expect(screen.getByText(/^ends in 1:5\d|^ends in 2:0\d/)).toBeInTheDocument();
    expect(screen.getByText("+$12")).toBeInTheDocument();

    portfolio = { ...portfolio, positions: [{ ...rockets, price: 0.66 }] };
    // A new event makes Home refetch at once.
    act(() =>
      FakeSource.last?.emit("demo.updated", { id: 900, type: "demo.updated", ts: new Date().toISOString(), data: { title: "Tick", detail: "" } }),
    );
    await waitFor(() => expect(screen.getByLabelText("Odds 66¢")).toHaveClass("flash-up"));
  });

  it("lists the biggest positions first, six at first, then all, and shows the linked account and similar market", async () => {
    portfolio = {
      ...portfolioBase,
      account: { venue: "Polymarket", name: "Trader1", wallet: "0x1", profile_url: "https://polymarket.com/@t", linked: true },
      positions: Array.from({ length: 8 }, (_, i) => ({
        ...rockets,
        id: `pm-${i}`,
        venue: "Polymarket" as const,
        title: `Market ${i}`,
        value_usd: 10 + i,
        ends_at: null,
        adjacent: i === 7 ? { venue: "Gemini", title: "Twin", symbol: "T", price: 0.4 } : null,
      })),
    };
    const user = userEvent.setup();
    render(<App />);
    const link = await screen.findByRole("link", { name: /Polymarket linked · Trader1/ });
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link).toHaveAttribute("href", "https://polymarket.com/@t");
    const cards = screen.getAllByRole("heading", { level: 3 });
    expect(cards[0]).toHaveTextContent("Market 7");
    expect(cards).toHaveLength(6);
    expect(screen.getByText("Similar on Gemini: Twin 40¢")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show all (8)" }));
    expect(screen.getAllByRole("heading", { level: 3 })).toHaveLength(8);
  });

  it("hides moved positions and marks a won one", async () => {
    portfolio = {
      ...portfolioBase,
      positions: [
        { ...rockets, id: "a", title: "Old one", status: "moved" },
        { ...rockets, id: "b", title: "Done one", status: "won" },
      ],
    };
    render(<App />);
    expect(await screen.findByText("Done one")).toBeInTheDocument();
    expect(screen.queryByText("Old one")).not.toBeInTheDocument();
    expect(screen.getByText("Won")).toBeInTheDocument();
  });

  it("Research shows the verdict, checks and source; Escape closes it and focus returns", async () => {
    const user = userEvent.setup();
    render(<App />);
    const button = await screen.findByRole("button", { name: /Research/ });
    await user.click(button);
    const panel = await screen.findByRole("region", { name: /Research/ });
    expect(await within(panel).findByText("Against")).toBeInTheDocument();
    expect(within(panel).getByText("Different index")).toBeInTheDocument();
    expect(within(panel).getByText("The indexes differ.")).toBeInTheDocument();
    const checks = within(panel).getByRole("list", { name: "Checks" });
    expect(checks).toHaveTextContent("Indexdifferent: BRTI vs Lukka");
    expect(checks).toHaveTextContent("Windowsame: 5 minutes");
    expect(within(panel).getByText("Next Polymarket window")).toBeInTheDocument();
    expect(within(panel).getByText("by Grok")).toBeInTheDocument();
    expect(gets).toContain("/api/demo/insight?position_id=pos-1");
    expect(panel).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("region", { name: /Research/ })).not.toBeInTheDocument();
    expect(button).toHaveFocus();
  });

  it("Move fills the command box", async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: /^Move Rockets/ }));
    const box = screen.getByLabelText("Where should the money go?");
    expect(box).toHaveValue("Move my Rockets to beat Nuggets position to Gemini");
    expect(box).toHaveFocus();
  });

  it("shows the offer banner with a countdown", async () => {
    portfolio = {
      ...portfolioBase,
      offer: { venue: "Gemini", headline: "Gemini pays 3% back", rebate_pct: 3, expires_at: inMinutes(10) },
    };
    render(<App />);
    expect(await screen.findByText("Gemini pays 3% back")).toBeInTheDocument();
    expect(screen.getByText(/^(9:5\d|10:00) left$/)).toBeInTheDocument();
  });

  it("lists recent actions, six first, then the rest", async () => {
    recent = Array.from({ length: 8 }, (_, i) => ({
      ts: new Date(Date.now() - (i + 1) * 3_600_000).toISOString(),
      kind: "send",
      title: `Action ${i}`,
      detail: "d",
      amount_usd: 5,
      asset: "USDC",
      from: null,
      to: null,
    }));
    const user = userEvent.setup();
    render(<App />);
    expect(await screen.findByText("Action 0")).toBeInTheDocument();
    expect(screen.getByText("1 h ago")).toBeInTheDocument();
    expect(screen.getAllByText("$5 USDC")).toHaveLength(6);
    expect(screen.queryByText("Action 6")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show more (2)" }));
    expect(screen.getByText("Action 7")).toBeInTheDocument();
  });
});

describe("tabs and header in demo mode", () => {
  it("Activity holds the ring and the feed, and Controls leads with the limit numbers", async () => {
    const user = userEvent.setup();
    render(<App />);
    const nav = screen.getByRole("navigation", { name: "Main" });
    await user.click(within(nav).getByRole("link", { name: "Activity" }));
    expect(window.location.hash).toBe("#activity");
    expect(screen.getByRole("heading", { name: "Activity", level: 1 })).toBeVisible();
    expect(screen.getByText("Where it went")).toBeVisible();
    expect(screen.getByText("Nothing yet.")).toBeVisible();
    expect(screen.queryByText("Left today")).not.toBeInTheDocument();

    await user.click(within(nav).getByRole("link", { name: "Controls" }));
    expect(await screen.findByText("Left today")).toBeInTheDocument();
  });

  it("the Demo card calls the offer, settle and reset routes", async () => {
    window.location.hash = "#controls";
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Show Gemini offer" }));
    await user.click(screen.getByRole("button", { name: "Settle Rockets as a win" }));
    await user.click(screen.getByRole("button", { name: "Reset demo" }));
    expect(posts).toEqual(["/api/demo/offer", "/api/demo/settle", "/api/demo/reset"]);
    expect(await screen.findByText("Demo reset.")).toBeInTheDocument();
  });

  it("has no Demo card when demo mode is off", async () => {
    demoOn = false;
    window.location.hash = "#controls";
    render(<App />);
    expect(await screen.findByText("Left today")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Demo" })).not.toBeInTheDocument();
  });
});

test("the feed words a demo.updated event as neutral", () => {
  const line = describeEvent({
    id: 1,
    ts: "2026-11-20T12:00:00+00:00",
    type: "demo.updated",
    data: { title: "Offer on", detail: "3% for 10 minutes" },
  } as SwitchboardEvent);
  expect(line).toMatchObject({ tone: "neutral", title: "Offer on", detail: "3% for 10 minutes" });
});
