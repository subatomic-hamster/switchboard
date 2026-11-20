import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import { CommandBar } from "./components/CommandBar";
import { transferPlan } from "./test-fixtures";
import type { Plan, Suggestion } from "./demo-types";
import type { ExecuteResponse } from "./types";

function Harness({
  onQuote,
  suggestions,
}: {
  onQuote?: (plan: Plan) => void;
  suggestions?: Suggestion[] | null;
}) {
  const [text, setText] = useState("");
  return <CommandBar text={text} onTextChange={setText} onQuote={onQuote} suggestions={suggestions} />;
}

function respond(routes: Record<string, { status?: number; body: unknown }>) {
  const calls: { url: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      const match = routes[url];
      if (!match) throw new Error(`unexpected request to ${url}`);
      return new Response(JSON.stringify(match.body), { status: match.status ?? 200 });
    }),
  );
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("CommandBar", () => {
  it("quotes both routes, reads the quote back, and sends by the chosen route", async () => {
    const executing: ExecuteResponse = {
      status: "executing",
      reason: null,
      request_id: null,
      transfer_id: "t_1",
      detail: "Within your limits. Sending now.",
    };
    const calls = respond({
      "/api/intent": { body: transferPlan },
      "/api/plans/p_1/execute": { body: executing },
    });
    const onQuote = vi.fn();
    const user = userEvent.setup();
    render(<Harness onQuote={onQuote} />);

    await user.type(
      screen.getByLabelText("Where should the money go?"),
      "move 200 from my eth wallet to sam's sol wallet",
    );
    await user.click(screen.getByRole("button", { name: "Get quote" }));

    // The big amount, the payee, the route line and the facts for the first route.
    expect(await screen.findByText("$200", { selector: ".quote-amount" })).toBeInTheDocument();
    expect(screen.getByText(/to Sam's Solana wallet/)).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Route" })).toHaveTextContent(
      "EthereumArcSolana",
    );
    expect(screen.getByText("$0.30")).toBeInTheDocument();
    expect(screen.getByText("about 15 min")).toBeInTheDocument();
    // The other route is folded away but reachable.
    expect(screen.getByText("$0.60 · about 20 s")).toBeInTheDocument();
    expect(onQuote).toHaveBeenCalledWith(transferPlan);
    expect(calls[0]).toEqual({
      url: "/api/intent",
      body: { text: "move 200 from my eth wallet to sam's sol wallet" },
    });

    expect(screen.getByRole("button", { name: "Send $200" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Send by this route" }));
    expect(await screen.findByText("Sending")).toBeInTheDocument();
    expect(calls[1]).toEqual({ url: "/api/plans/p_1/execute", body: { route_id: "fastest" } });
  });

  it("shows the reason code when the vault needs approval", async () => {
    respond({
      "/api/intent": {
        body: {
          ...transferPlan,
          preview: {
            outcome: "ask",
            reason: "OVER_TRANSFER_CAP",
            detail: "That is over your 200 dollars transfer limit.",
          },
        },
      },
      "/api/plans/p_1/execute": {
        body: {
          status: "needs_approval",
          reason: "OVER_TRANSFER_CAP",
          request_id: "7",
          transfer_id: null,
          detail: "The vault needs your approval. Your phone will ring.",
        },
      },
    });
    const user = userEvent.setup();
    render(<Harness />);
    await user.type(screen.getByLabelText("Where should the money go?"), "send 250 to sam");
    await user.click(screen.getByRole("button", { name: "Get quote" }));
    // The one-line note that this will need approval.
    expect(await screen.findByText(/over your 200 dollars transfer limit/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Send $200" }));
    expect(await screen.findByText("Needs approval")).toBeInTheDocument();
    expect(screen.getByText("OVER_TRANSFER_CAP")).toBeInTheDocument();
    expect(screen.getByText(/Your phone will ring/)).toBeInTheDocument();
  });

  it("shows live suggestions in the box on click, with the reason as a tooltip", async () => {
    const calls = respond({});
    const user = userEvent.setup();
    render(
      <Harness
        suggestions={[{ label: "Buy BTC", text: "Put 30 dollars into Bitcoin", why: "Live window" }]}
      />,
    );
    expect(screen.queryByRole("button", { name: "Move my Rockets position" })).not.toBeInTheDocument();
    const chip = screen.getByRole("button", { name: "Buy BTC" });
    expect(chip).toHaveAttribute("title", "Live window");
    await user.click(chip);
    expect(screen.getByLabelText("Where should the money go?")).toHaveValue("Put 30 dollars into Bitcoin");
    expect(calls).toHaveLength(0);
  });

  it("keeps the static examples when there are no live suggestions", () => {
    render(<Harness suggestions={null} />);
    expect(screen.getByRole("button", { name: "Move my Rockets position" })).toBeInTheDocument();
  });

  it("quotes a market buy as a plan card and Confirm executes the plan", async () => {
    const buy: Plan = {
      plan_id: "p_9",
      kind: "market_buy",
      routes: [],
      spoken_quote: "Buying.",
      parser: "rules",
      market_buy: {
        venue: "Polymarket US",
        slug: "btc-5m",
        title: "Bitcoin 5 minute",
        side: "Up",
        amount_usd: 30,
        price: 0.52,
        contracts: 57.69,
        fee_usd: 0.15,
        from_place: "my Ethereum wallet",
        ends_at: new Date(Date.now() + 125_000).toISOString(),
      },
    };
    const calls = respond({
      "/api/intent": { body: buy },
      "/api/plans/p_9/execute": {
        body: { status: "executing", detail: "Bought 57.69 contracts." },
      },
    });
    const user = userEvent.setup();
    render(<Harness />);
    await user.type(screen.getByLabelText("Where should the money go?"), "put 30 into bitcoin");
    await user.click(screen.getByRole("button", { name: "Get quote" }));
    expect(await screen.findByText("Bitcoin 5 minute")).toBeInTheDocument();
    expect(screen.getByText(/Buy/)).toHaveTextContent("Buy Up on Bitcoin 5 minute (Polymarket US)");
    expect(screen.getByText("$30", { selector: ".quote-amount" })).toBeInTheDocument();
    expect(screen.getByText("52¢")).toBeInTheDocument();
    expect(screen.getByText("~57.69")).toBeInTheDocument();
    expect(screen.getByText("$0.15")).toBeInTheDocument();
    expect(screen.getByText(/from my Ethereum wallet/)).toBeInTheDocument();
    expect(screen.getByText(/Ends in 2:0\d|Ends in 2:1\d|Ends in 1:5\d/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    expect(await screen.findByText("Bought 57.69 contracts.")).toBeInTheDocument();
    expect(screen.getByText("Bought")).toBeInTheDocument();
    expect(calls[1]).toEqual({ url: "/api/plans/p_9/execute", body: { route_id: "cheapest" } });
  });

  it("shows the server's own explanation when a sentence cannot be used", async () => {
    respond({
      "/api/intent": {
        status: 422,
        body: { detail: "I have no wallet on file for Alex. Give me their wallet address." },
      },
    });
    const user = userEvent.setup();
    render(<Harness />);
    await user.type(screen.getByLabelText("Where should the money go?"), "send 20 to alex");
    await user.click(screen.getByRole("button", { name: "Get quote" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("no wallet on file for Alex");
  });
});
