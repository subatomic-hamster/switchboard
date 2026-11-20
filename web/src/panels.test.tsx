import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ApprovalPanel } from "./components/ApprovalPanel";
import { MicButton } from "./components/MicButton";
import { RingView } from "./components/RingView";
import { FreezeButton } from "./components/FreezeButton";
import { HealthPanel } from "./components/HealthPanel";
import { StatsPanel } from "./components/StatsPanel";
import type { Recognition } from "./speech";
import { event, pendingApproval, stats } from "./test-fixtures";
import type { EventOf } from "./types";

function respond(routes: Record<string, unknown>) {
  const calls: { url: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (!(url in routes)) throw new Error(`unexpected request to ${url}`);
      return new Response(JSON.stringify(routes[url]));
    }),
  );
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("ApprovalPanel", () => {
  it("renders nothing when no request is pending", () => {
    const { container } = render(
      <ApprovalPanel
        approvals={[{ ...pendingApproval, status: "approved", challenge: null }]}
        callsLive={false}
        passkeyRegistered={false}
        onChanged={() => undefined}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the word, both cases and the reason code", () => {
    render(
      <ApprovalPanel
        approvals={[pendingApproval]}
        callsLive={false}
        passkeyRegistered={false}
        onChanged={() => undefined}
      />,
    );
    expect(screen.getByTestId("challenge-word")).toHaveTextContent("harbor");
    expect(screen.getByText("OVER_TRANSFER_CAP")).toBeInTheDocument();
    expect(screen.getByText("Operator: the case for")).toBeInTheDocument();
    expect(screen.getByText("Risk officer: the case against")).toBeInTheDocument();
    expect(screen.getByText(/50 dollars over your 200 dollars transfer limit/)).toBeInTheDocument();
    expect(screen.getByText(/“Yes” is not enough/)).toBeInTheDocument();
  });

  it("in dry run, stands in for the call and reports a wrong word", async () => {
    const calls = respond({
      "/api/approvals/7/simulate-voice": { ok: false, status: "pending", error: "challenge_mismatch" },
    });
    const onChanged = vi.fn();
    const user = userEvent.setup();
    render(
      <ApprovalPanel
        approvals={[pendingApproval]}
        callsLive={false}
        passkeyRegistered={false}
        onChanged={onChanged}
      />,
    );
    await user.type(screen.getByPlaceholderText("Amount you would say"), "250");
    await user.type(screen.getByPlaceholderText("Word you would say"), "lantern");
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText("That is not the word on this screen.")).toBeInTheDocument();
    expect(calls[0]).toEqual({
      url: "/api/approvals/7/simulate-voice",
      body: { amount_spoken: "250", challenge: "lantern" },
    });
    expect(onChanged).toHaveBeenCalled();
  });

  it("with live calls, offers no stand-in form, only deny", async () => {
    const calls = respond({ "/api/approvals/7/deny": { ok: true, status: "denied", error: "" } });
    const user = userEvent.setup();
    render(
      <ApprovalPanel
        approvals={[pendingApproval]}
        callsLive
        passkeyRegistered={false}
        onChanged={() => undefined}
      />,
    );
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.getByText(/Your phone is ringing/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Deny" }));
    expect(calls[0]?.url).toBe("/api/approvals/7/deny");
  });

  it("says when a passkey is also needed", () => {
    render(
      <ApprovalPanel
        approvals={[{ ...pendingApproval, requires_passkey: true }]}
        callsLive={false}
        passkeyRegistered={false}
        onChanged={() => undefined}
      />,
    );
    expect(screen.getByText(/Passkey also required/)).toBeInTheDocument();
    expect(screen.getByText(/None is set up yet/)).toBeInTheDocument();
  });
});

describe("StatsPanel", () => {
  it("shows budget left, the no-ask limit and calls used", () => {
    render(<StatsPanel stats={stats} />);
    expect(screen.getByText("$300")).toBeInTheDocument();
    expect(screen.getByText("up to $200")).toBeInTheDocument();
    expect(screen.getByText("Calls today")).toBeInTheDocument();
    expect(
      screen.getByRole("meter", { name: "Share of today's budget spent" }),
    ).toHaveAttribute("aria-valuenow", "200");
    expect(screen.getByRole("meter", { name: "Share of the daily call limit used" })).toHaveAttribute(
      "aria-valuenow",
      "0",
    );
  });

  it("warns at a one hundred percent approval rate", () => {
    render(
      <StatsPanel
        stats={{
          ...stats,
          approvals: {
            total: 3,
            approved: 3,
            denied: 0,
            rate_pct: 100,
            warning: "You have approved all 3 requests.",
          },
        }}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("You have approved all 3 requests.");
  });
});

describe("FreezeButton", () => {
  it("freezes the vault, and offers to unfreeze once frozen", async () => {
    const calls = respond({ "/api/freeze": { ok: true, status: "frozen", error: "" } });
    const user = userEvent.setup();
    const { rerender } = render(<FreezeButton stats={stats} onChanged={() => undefined} />);
    await user.click(screen.getByRole("button", { name: "Freeze" }));
    expect(calls[0]).toEqual({ url: "/api/freeze", body: { frozen: true } });

    rerender(<FreezeButton stats={{ ...stats, frozen: true }} onChanged={() => undefined} />);
    expect(screen.getByRole("button", { name: "Unfreeze" })).toBeInTheDocument();
  });
});

describe("HealthPanel", () => {
  it("shows the vault, the stream and the passkey, with the stream switch only against the stub", () => {
    const { rerender } = render(<HealthPanel stats={stats} onChanged={() => undefined} />);
    expect(screen.getByText("Open")).toBeInTheDocument();
    expect(screen.getByText("Delivering")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Kill the stream" })).toBeInTheDocument();

    rerender(
      <HealthPanel
        stats={{ ...stats, frozen: true, stream_stale: true, using_p1_stubs: false }}
        onChanged={() => undefined}
      />,
    );
    expect(screen.getByText("Frozen")).toBeInTheDocument();
    expect(screen.getByText("Quiet, spending paused")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Kill the stream" })).not.toBeInTheDocument();
  });

  it("kills the stream against the stub", async () => {
    const calls = respond({ "/api/chain/stub/stream": { ok: true, status: "stale", error: "" } });
    const user = userEvent.setup();
    render(<HealthPanel stats={stats} onChanged={() => undefined} />);
    await user.click(screen.getByRole("button", { name: "Kill the stream" }));
    expect(calls[0]).toEqual({ url: "/api/chain/stub/stream", body: { stale: true } });
  });
});

describe("MicButton", () => {
  it("explains itself where the browser cannot listen", () => {
    render(<MicButton onText={() => undefined} onFinal={() => undefined} />);
    expect(screen.getByRole("button", { name: /Voice input needs/ })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
  });

  it("passes interim text, then the finished sentence once", async () => {
    let instance: Recognition | null = null;
    class FakeRecognition {
      lang = "";
      continuous = false;
      interimResults = false;
      onresult: Recognition["onresult"] = null;
      onerror: Recognition["onerror"] = null;
      onend: Recognition["onend"] = null;
      start() {
        instance = this as unknown as Recognition;
      }
      stop() {
        this.onend?.();
      }
    }
    vi.stubGlobal("SpeechRecognition", FakeRecognition);
    const onText = vi.fn();
    const onFinal = vi.fn();
    const user = userEvent.setup();
    render(<MicButton onText={onText} onFinal={onFinal} />);
    await user.click(screen.getByRole("button", { name: "Speak" }));
    expect(screen.getByRole("button", { name: "Listening…" })).toHaveAttribute("aria-pressed", "true");

    const heard = instance as Recognition | null;
    act(() => {
      heard?.onresult?.({
        resultIndex: 0,
        results: [{ isFinal: false, 0: { transcript: "move two hundred" } }],
      });
      heard?.onresult?.({
        resultIndex: 0,
        results: [{ isFinal: true, 0: { transcript: "move two hundred dollars to Sam" } }],
      });
      heard?.onend?.();
    });
    expect(onText).toHaveBeenCalledWith("move two hundred");
    expect(onFinal).toHaveBeenCalledTimes(1);
    expect(onFinal).toHaveBeenCalledWith("move two hundred dollars to Sam");
    expect(screen.getByRole("button", { name: "Speak" })).toBeInTheDocument();
  });
});

describe("RingView", () => {
  it("draws the vault, every destination, and describes its state", () => {
    const requested = event<EventOf<"approval.requested">>("approval.requested", {
      request_id: "7",
      agent: "0xa",
      payee: "0xnew",
      amount_usd: 250,
      reason: "OVER_TRANSFER_CAP",
    });
    render(<RingView signals={[requested]} wallets={[]} frozen stale={false} />);
    const ring = screen.getByRole("img");
    expect(ring).toHaveAccessibleName(
      "Vault ring. Spending is paused. 1 transfer waiting for approval.",
    );
    for (const label of ["Vault", "Ethereum", "Solana", "Arc", "New address", "Gemini", "Polymarket US"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.getByText("$250")).toBeInTheDocument();
    expect(screen.getByText("frozen")).toBeInTheDocument();
    // The legend is gone; the held transfer is still drawn and counted in the ring's name.
    expect(screen.queryByText("Held for your approval")).not.toBeInTheDocument();
  });
});
