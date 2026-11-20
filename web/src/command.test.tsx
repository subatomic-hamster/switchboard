import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import { CommandBar } from "./components/CommandBar";
import { transferPlan } from "./test-fixtures";
import type { ExecuteResponse, IntentResponse } from "./types";

function Harness({ onQuote }: { onQuote?: (plan: IntentResponse) => void }) {
  const [text, setText] = useState("");
  return <CommandBar text={text} onTextChange={setText} onQuote={onQuote} />;
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
