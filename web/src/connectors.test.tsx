import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ApprovalPanel } from "./components/ApprovalPanel";
import { ConnectorsPanel } from "./components/ConnectorsPanel";
import { pendingApproval } from "./test-fixtures";
import type { ConnectorList, ConnectorView } from "./types";

const chatgpt: ConnectorView = {
  id: 1,
  name: "ChatGPT",
  kind: "chatgpt",
  auth: "link",
  token_hint: "ab12",
  created: "2026-11-19T10:00:00Z",
  revoked: false,
  auto_send_usd: 30,
  daily_usd: 100,
  sent_today_usd: 45,
  asks_today: 1,
  last_used: null,
};

function listOf(connectors: ConnectorView[], extra: Partial<ConnectorList> = {}): ConnectorList {
  return {
    connectors,
    mcp_url: "https://sb.example/api/connect/mcp",
    api_url: "https://sb.example/api/connect/v1",
    public: true,
    simulated: true,
    max_asks_per_day: 3,
    ...extra,
  };
}

interface Call {
  method: string;
  url: string;
  body: unknown;
}

/** Answers by "METHOD url". Records every call. */
function respond(routes: Record<string, unknown>) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const call = {
        method: init?.method ?? "GET",
        url: String(input),
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      };
      calls.push(call);
      const key = `${call.method} ${call.url}`;
      if (!(key in routes)) throw new Error(`unexpected request: ${key}`);
      return new Response(JSON.stringify(routes[key]));
    }),
  );
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("ConnectorsPanel", () => {
  it("shows each assistant with its limit and what it has done today", async () => {
    respond({
      "GET /api/connectors": listOf([
        chatgpt,
        { ...chatgpt, id: 2, name: "Claude", kind: "claude", auto_send_usd: 0, daily_usd: 0 },
        { ...chatgpt, id: 3, name: "Old", revoked: true },
      ]),
    });
    render(<ConnectorsPanel refreshKey={0} />);

    const first = (await screen.findByText("ChatGPT", { selector: "strong" })).closest("li") as HTMLElement;
    expect(within(first).getByText(/Sends up to \$30 without asking, \$100 a day/)).toBeVisible();
    expect(within(first).getByText(/sent \$45 today/)).toBeVisible();
    expect(within(first).getByText(/asked you 1 time today/)).toBeVisible();
    expect(within(first).getByText(/secret address ending ab12/)).toBeVisible();

    const second = screen.getByText("Claude", { selector: "strong" }).closest("li") as HTMLElement;
    expect(within(second).getByText(/Asks you every time/)).toBeVisible();
    expect(screen.queryByText("Old")).not.toBeInTheDocument();
    expect(screen.getByText("1 revoked connector not shown.")).toBeVisible();
    expect(screen.getByText(/simulated transfers/)).toBeVisible();
  });

  it("says so when nothing outside this computer can reach it", async () => {
    respond({ "GET /api/connectors": listOf([], { public: false }) });
    render(<ConnectorsPanel refreshKey={0} />);
    expect(await screen.findByText(/No public address is set/)).toBeVisible();
    expect(screen.getByText(/None connected/)).toBeVisible();
  });

  it("connects an assistant that asks every time and shows its secret once", async () => {
    const secret = "https://sb.example/api/connect/link/swb_secret/mcp";
    const calls = respond({
      "GET /api/connectors": listOf([]),
      "POST /api/connectors": {
        connector: { ...chatgpt, auto_send_usd: 0, daily_usd: 0 },
        token: "swb_secret",
        mcp_url: secret,
        api_url: "https://sb.example/api/connect/v1",
      },
    });
    const user = userEvent.setup();
    render(<ConnectorsPanel refreshKey={0} />);
    await screen.findByText(/None connected/);
    // The extra options are folded away, but the hint is there.
    expect(screen.getByText(/At 0 it asks you about every transfer/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Connect" }));

    const made = calls.find((call) => call.method === "POST");
    expect(made?.body).toEqual({
      name: "ChatGPT",
      kind: "chatgpt",
      auth: "link",
      auto_send_usd: 0,
      daily_usd: 0,
    });
    expect(await screen.findByText(/ChatGPT is connected/)).toBeVisible();
    // A secret address is the secret: it is shown, and the bare key is not shown beside it.
    const shown = screen.getAllByTestId("connector-secret").map((node) => node.textContent);
    expect(shown).toEqual([secret]);
    expect(screen.getByText(/Anyone who has the address can act as this assistant/)).toBeVisible();
    expect(screen.getByText(/turn on developer mode/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "I have copied it" }));
    expect(screen.queryByText(secret)).not.toBeInTheDocument();
  });

  it("shows the address and the key separately for a key-type connector", async () => {
    respond({
      "GET /api/connectors": listOf([]),
      "POST /api/connectors": {
        connector: { ...chatgpt, name: "Research bot", kind: "other", auth: "header" },
        token: "swb_key",
        mcp_url: "https://sb.example/api/connect/mcp",
        api_url: "https://sb.example/api/connect/v1",
      },
    });
    const user = userEvent.setup();
    render(<ConnectorsPanel refreshKey={0} />);
    await screen.findByText(/None connected/);
    await user.selectOptions(screen.getByLabelText("Assistant"), "other");
    expect(screen.getByLabelText("How it connects")).toHaveValue("header");
    await user.click(screen.getByRole("button", { name: "Connect" }));
    await screen.findByText(/Research bot is connected/);
    const shown = screen.getAllByTestId("connector-secret").map((node) => node.textContent);
    expect(shown).toEqual(["https://sb.example/api/connect/mcp", "swb_key"]);
  });

  it("changes a limit and revokes", async () => {
    const calls = respond({
      "GET /api/connectors": listOf([chatgpt]),
      "PATCH /api/connectors/1": { ...chatgpt, auto_send_usd: 10, daily_usd: 20 },
      "DELETE /api/connectors/1": { ok: true, status: "revoked", error: "" },
    });
    const user = userEvent.setup();
    render(<ConnectorsPanel refreshKey={0} />);
    const row = (await screen.findByText("ChatGPT", { selector: "strong" })).closest("li") as HTMLElement;

    await user.click(within(row).getByRole("button", { name: "Change limit" }));
    const single = within(row).getByLabelText("Without asking, up to");
    await user.clear(single);
    await user.type(single, "10");
    const daily = within(row).getByLabelText("In a day, up to");
    await user.clear(daily);
    await user.type(daily, "20");
    await user.click(within(row).getByRole("button", { name: "Save limit" }));
    expect(calls.find((call) => call.method === "PATCH")?.body).toEqual({
      auto_send_usd: 10,
      daily_usd: 20,
    });

    await user.click(within(row).getByRole("button", { name: "Revoke" }));
    expect(calls.some((call) => call.method === "DELETE")).toBe(true);
  });
});

describe("ApprovalPanel", () => {
  it("says which connected assistant asked", () => {
    render(
      <ApprovalPanel
        approvals={[
          { ...pendingApproval, reason: "OVER_AGENT_LIMIT", source: "connector:ChatGPT" },
        ]}
        callsLive={false}
        passkeyRegistered={false}
        onChanged={() => undefined}
      />,
    );
    expect(screen.getByText("OVER_AGENT_LIMIT")).toBeVisible();
    expect(screen.getByText(/asked by ChatGPT, not you/)).toBeVisible();
    expect(screen.getByText(/More than that assistant may send without asking/)).toBeVisible();
  });
});
