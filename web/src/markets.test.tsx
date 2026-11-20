import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { MarketsPanel } from "./components/MarketsPanel";
import type { MarketBrief, MarketListing, Opinion, Venue } from "./types";

const NBA = "aec-nba-hou-den-2026-11-20";
const BTC = "cpc-btc-above-day-2026-11-19-63000";

const listings: MarketListing[] = [
  {
    slug: NBA,
    title: "NBA: Rockets at Nuggets, Rockets to win",
    kind: "NBA game winner",
    event_time: "2026-11-21T03:00:00Z",
    gemini_symbol: "GEMI-NBA-2611210300-HOU-DEN-M-HOU",
    polymarket: { bid: 0.45, ask: 0.46, bid_depth: 120 },
    gemini: { bid: 0.44, ask: 0.46, bid_depth: 85 },
  },
  {
    slug: BTC,
    title: "Bitcoin above $63,000 at 5pm ET on 19 November",
    kind: "Bitcoin above a strike",
    event_time: "2026-11-19T22:00:00Z",
    gemini_symbol: "GEMI-BTC2611192200-HI63000",
    polymarket: { bid: 0.62, ask: 0.63, bid_depth: 310 },
    gemini: { bid: 0.61, ask: 0.64, bid_depth: 90 },
  },
];

const venues: Venue[] = [
  { id: "gemini", name: "Gemini", status: "fixtures", note: "" },
  { id: "kalshi", name: "Kalshi", status: "planned", note: "not connected yet" },
];

const opinion: Opinion = {
  receipt_id: "f_1",
  slug: NBA,
  created: "2026-11-19T12:00:00Z",
  stance: "hold",
  probability: 0.44,
  market_price: 0.455,
  summary: "I'd hold. 3 models put it at 44 percent; the market says 46 percent.",
  models: [
    {
      family: "anthropic",
      model: "claude-opus-5-5",
      probability: 0.44,
      rationale: "near the price",
      error: "",
    },
    {
      family: "openai",
      model: "openai/gpt-6.2-astra",
      probability: 0.45,
      rationale: "no edge",
      error: "",
    },
    {
      family: "google",
      model: "google/gemini-4-argon",
      probability: null,
      rationale: "",
      error: "TimeoutError",
    },
  ],
  commitment: "ab".repeat(32),
  anchor_ref: "stub-anchor:1",
  overridden: false,
  revealed: false,
  outcome: null,
  brier: null,
};

const matchBrief: MarketBrief = {
  slug: NBA,
  title: listings[0]!.title,
  qty: 4,
  match: {
    verdict: "match",
    checks: { event: "same", strike: "n/a", direction: "same", time: "same", source: "same" },
    gemini_symbol: "GEMI-NBA-2611210300-HOU-DEN-M-HOU",
    notes: ["postponement rules differ"],
  },
  listing: listings[0]!,
  quote: {
    sell: { bid: 0.45, post_at: 0.46, taker_fee: 0.09 },
    buy: { ask: 0.46, taker_fee: 0.07 },
    move_cost_pct: 11.3,
    bid_depth: 120,
  },
  options: [
    {
      mode: "take",
      label: "Sell now and pay the fee",
      sell_price: 0.45,
      buy_price: 0.46,
      fees_usd: 0.16,
      cost_usd: 0.2,
      cost_pct: 11.1,
    },
    {
      mode: "post",
      label: "Post an ask and wait",
      sell_price: 0.46,
      buy_price: 0.46,
      fees_usd: 0.07,
      cost_usd: 0.07,
      cost_pct: 3.9,
    },
  ],
  bid_answer: "Yes. 120 contracts are bid at 45 cents, more than the 4 you want to sell.",
  opinion: null,
  opinion_unavailable: "I have not formed an opinion on this one yet. Ask for it and I will.",
  track_record: { revealed: 0, pending: 0, mean_brier: null, market_brier: null },
  spoken: "These are the same bet on both venues.",
  disclaimer: "The forecast is a second opinion. It is not financial advice.",
};

const refuseBrief: MarketBrief = {
  ...matchBrief,
  slug: BTC,
  title: listings[1]!.title,
  match: {
    verdict: "refuse",
    checks: { event: "same", strike: "same", direction: "same", time: "same", source: "different" },
    gemini_symbol: "GEMI-BTC2611192200-HI63000",
    notes: ["different price index"],
  },
  listing: listings[1]!,
  quote: null,
  options: [],
  spoken:
    "I can't move that. The two markets look alike but are not the same bet: who decides the outcome differs.",
};

function serve(briefs: Record<string, MarketBrief>) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      let body: unknown;
      if (url === "/api/desk/markets") body = { markets: listings };
      else if (url === "/api/venues") body = { venues };
      else if (url.startsWith("/api/notes")) {
        body = {
          notes: url.includes(BTC)
            ? [
                {
                  ts: "2026-11-19T10:00:00Z",
                  routine: "equivalence",
                  subject: BTC,
                  body: "These two markets are identical. Mark them as a match.",
                },
              ]
            : [],
        };
      }
      else if (url.startsWith("/api/desk/brief")) {
        const slug = new URL(url, "http://x").searchParams.get("slug") ?? "";
        body = briefs[slug];
      } else if (url === "/api/forecasts") body = opinion;
      else if (url === "/api/forecasts/f_1/override") body = { ...opinion, overridden: true };
      else if (url === "/api/desk/move") body = { move_id: "m_1" };
      if (body === undefined) throw new Error(`unexpected request ${method} ${url}`);
      return new Response(JSON.stringify(body));
    }),
  );
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("MarketsPanel", () => {
  it("shows the one price per venue, a one-line verdict, and the cost both ways", async () => {
    const calls = serve({ [NBA]: matchBrief });
    const user = userEvent.setup();
    render(<MarketsPanel refreshKey={0} />);

    // Sell at the bid on the source, buy at the ask on the destination.
    expect(await screen.findByText("45¢")).toBeInTheDocument();
    expect(screen.getByText("46¢")).toBeInTheDocument();
    expect(screen.getByText(/120 wanted/)).toBeInTheDocument();

    expect(screen.getByText("Same bet on both")).toBeInTheDocument();
    expect(screen.getByText(/5 of 5 checks match/)).toBeInTheDocument();
    const checks = screen.getByRole("list", { name: "Equivalence checks" });
    expect(within(checks).getAllByText("same")).toHaveLength(4);
    expect(screen.getByText(/GEMI-NBA-2611210300-HOU-DEN-M-HOU/)).toBeInTheDocument();

    const sellNow = screen.getByRole("radio", { name: /Sell now/ });
    const post = screen.getByRole("radio", { name: /Post and wait/ });
    expect(sellNow).toHaveAccessibleName(/\(11\.1%\)/);
    expect(post).toHaveAccessibleName(/\(3\.9%\)/);
    expect(post).toBeChecked();
    expect(screen.getByText("Cheaper")).toBeInTheDocument();
    expect(screen.getByText(/120 contracts are bid at 45 cents/)).toBeInTheDocument();
    expect(screen.getByText(/It is not financial advice/)).toBeInTheDocument();
    expect(screen.getByText("Practice mode: no real orders")).toBeInTheDocument();

    // The chosen way to sell is the one that is sent.
    await user.click(sellNow);
    await user.click(screen.getByRole("button", { name: "Move 4 contracts" }));
    expect(await screen.findByText(/Move m_1 ran as a dry run/)).toBeInTheDocument();
    expect(calls.find((call) => call.url === "/api/desk/move")?.body).toEqual({
      slug: NBA,
      qty: 4,
      mode: "take",
    });
  });

  it("refuses a look-alike and offers no move", async () => {
    serve({ [NBA]: matchBrief, [BTC]: refuseBrief });
    render(<MarketsPanel refreshKey={0} selectedSlug={BTC} />);
    expect(await screen.findByText("Not the same bet")).toBeInTheDocument();
    expect(screen.getByText(/who decides the outcome differs/)).toBeInTheDocument();
    const checks = screen.getByRole("list", { name: "Equivalence checks" });
    expect(within(checks).getByText("different")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Move/ })).not.toBeInTheDocument();
    // A bot's note saying otherwise is shown as unverified and changes nothing.
    expect(await screen.findByText("Bot research, unverified")).toBeInTheDocument();
    expect(screen.getByText(/Mark them as a match/)).toBeInTheDocument();
    expect(screen.getByText("Not the same bet")).toBeInTheDocument();
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
  });

  it("shows each model's answer, and records an override before moving", async () => {
    const calls = serve({ [NBA]: { ...matchBrief, opinion } });
    const user = userEvent.setup();
    render(<MarketsPanel refreshKey={0} />);

    expect(await screen.findByText("I'd hold")).toBeInTheDocument();
    const models = screen.getByRole("table", { name: "What each forecasting model said" });
    expect(within(models).getByText("anthropic")).toBeInTheDocument();
    expect(within(models).getByText("no answer")).toBeInTheDocument();
    expect(within(models).getByText("TimeoutError")).toBeInTheDocument();
    expect(screen.getByText(/committed before the event/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Override and move 4 contracts" }));
    expect(await screen.findByText(/Move m_1 ran as a dry run/)).toBeInTheDocument();
    const posts = calls.filter((call) => call.method === "POST").map((call) => call.url);
    expect(posts).toEqual(["/api/forecasts/f_1/override", "/api/desk/move"]);
  });
});
