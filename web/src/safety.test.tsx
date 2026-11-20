import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { HostileBox } from "./components/HostileBox";
import { RulesBox } from "./components/RulesBox";
import type { HostileResult, RuleView } from "./types";

const pendingRule: RuleView = {
  proposal_id: "r_1",
  readback:
    "You want to raise your per-transfer limit from 200 dollars to 300 dollars. To confirm, say confirm and the word on your Switchboard screen.",
  needs_word: true,
  status: "pending",
  text: "raise my transfer limit to 300",
  challenge: "lantern",
  expires_at: "2099-01-01T00:00:00+00:00",
};

function serve(handler: (url: string, body: unknown) => unknown) {
  const calls: { url: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      calls.push({ url, body });
      const answer = handler(url, body);
      if (answer instanceof Response) return answer;
      if (answer === undefined) throw new Error(`unexpected request to ${url}`);
      return new Response(JSON.stringify(answer));
    }),
  );
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("RulesBox", () => {
  it("reads the rule back, shows the word, and confirms with it", async () => {
    let rules: RuleView[] = [];
    const calls = serve((url) => {
      if (url === "/api/rules") return { rules };
      if (url === "/api/rules/propose") {
        rules = [pendingRule];
        return pendingRule;
      }
      if (url === "/api/rules/r_1/confirm") {
        rules = [{ ...pendingRule, status: "applied", challenge: null }];
        return { ok: true, status: "applied", error: "" };
      }
      return undefined;
    });
    const onChanged = vi.fn();
    const user = userEvent.setup();
    render(<RulesBox callsLive={false} refreshKey={0} onChanged={onChanged} />);

    await user.type(screen.getByLabelText("Change a rule in words"), "raise my transfer limit to 300");
    await user.click(screen.getByRole("button", { name: "Read it back" }));
    expect(await screen.findByText(/raise your per-transfer limit from 200 dollars/)).toBeInTheDocument();
    expect(screen.getByText("lantern")).toBeInTheDocument();

    const confirm = screen.getByRole("button", { name: "Confirm" });
    expect(confirm).toBeDisabled();
    await user.type(screen.getByPlaceholderText("Word you would say"), "lantern");
    await user.click(confirm);
    expect(await screen.findByText("Applied. The policy has changed.")).toBeInTheDocument();
    expect(calls.find((call) => call.url === "/api/rules/r_1/confirm")?.body).toEqual({
      challenge: "lantern",
    });
    expect(onChanged).toHaveBeenCalled();
  });

  it("says why a rule cannot be used", async () => {
    serve((url) => {
      if (url === "/api/rules") return { rules: [] };
      if (url === "/api/rules/propose") {
        return new Response(
          JSON.stringify({ detail: "A transfer limit of 900 dollars would be above your daily budget." }),
          { status: 422 },
        );
      }
      return undefined;
    });
    const user = userEvent.setup();
    render(<RulesBox callsLive={false} refreshKey={0} onChanged={() => undefined} />);
    await user.type(screen.getByLabelText("Change a rule in words"), "set my transfer limit to 900");
    await user.click(screen.getByRole("button", { name: "Read it back" }));
    expect(await screen.findByRole("status")).toHaveTextContent("above your daily budget");
  });

  it("with live calls, sends the owner to the phone to confirm", async () => {
    serve((url) => (url === "/api/rules" ? { rules: [pendingRule] } : undefined));
    render(<RulesBox callsLive refreshKey={0} onChanged={() => undefined} />);
    expect(await screen.findByText("Confirm on the call.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument();
  });
});

describe("HostileBox", () => {
  it("shows what the reader noticed and that the worst case is a phone call", async () => {
    const result: HostileResult = {
      reader_flags: ["claims special authority", "demands a payment"],
      reader_outcome: "The reader has no tools. It noted the instruction-like text and did nothing else.",
      demanded_amount_usd: 5000,
      demanded_payee: "new address ending bad0",
      worst_case: "phone_call",
      reason: "OVER_TRANSFER_CAP",
      request_id: "9",
      explanation: "Worst case: your phone rings.",
    };
    const calls = serve((url) => (url === "/api/hostile" ? result : undefined));
    const onChanged = vi.fn();
    const user = userEvent.setup();
    render(<HostileBox onChanged={onChanged} />);
    const box = screen.getByLabelText<HTMLTextAreaElement>("Text from a web page");
    expect(box.value).toContain("SYSTEM OVERRIDE");
    await user.click(screen.getByRole("button", { name: "Feed it to the agent" }));

    expect(await screen.findByText("Worst case: a phone call")).toBeInTheDocument();
    expect(screen.getByText("claims special authority")).toBeInTheDocument();
    expect(screen.getByText(/it would ask the vault for \$5,000 to new address ending bad0/)).toBeInTheDocument();
    expect(screen.getByText("OVER_TRANSFER_CAP")).toBeInTheDocument();
    expect(screen.getByText(/The reader has no tools/)).toBeInTheDocument();
    expect(calls[0]?.url).toBe("/api/hostile");
    expect(onChanged).toHaveBeenCalled();
  });
});
