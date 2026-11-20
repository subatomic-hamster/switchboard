import { readFileSync } from "node:fs";

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ClaimPage, parseClaim } from "./ClaimPage";
import { SendByLink } from "./components/SendByLink";

const SECRET = "ab".repeat(32);
const LINK = `http://localhost:8787/claim#7:0x${SECRET}`;
const WALLET = "0x" + "12".repeat(20);

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

const future = () => new Date(Date.now() + 86_400_000).toISOString();

afterEach(() => {
  vi.unstubAllGlobals();
  window.location.hash = "";
});

describe("SendByLink", () => {
  async function open() {
    const user = userEvent.setup();
    render(<SendByLink />);
    await user.click(screen.getByRole("button", { name: "No wallet? Send a link" }));
    await user.type(screen.getByLabelText("Amount (USD)"), "5");
    return user;
  }

  it("creates the link, shows it with Copy, and holds the refund until expiry", async () => {
    const calls = respond({
      "/api/escrow/create": {
        body: { escrow_id: "7", secret: `0x${SECRET}`, claim_link: LINK, expiry: future(), tx_hash: "0x1" },
      },
    });
    const user = await open();
    await user.selectOptions(screen.getByLabelText("Expires in"), "1");
    await user.click(screen.getByRole("button", { name: "Create link" }));

    expect(calls[0]?.body).toEqual({ amount_usd: 5, expiry_s: 86400 });
    expect(await screen.findByDisplayValue(LINK)).toBeInTheDocument();
    expect(screen.getByText(/Anyone with this link can claim it until/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel and refund" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Copy" }));
    expect(await navigator.clipboard.readText()).toBe(LINK);
    expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument();
    expect(JSON.stringify({ ...localStorage })).not.toContain(SECRET);
  });

  it("refunds once the link has expired", async () => {
    const calls = respond({
      "/api/escrow/create": {
        body: {
          escrow_id: "7",
          secret: `0x${SECRET}`,
          claim_link: LINK,
          expiry: new Date(Date.now() - 1000).toISOString(),
          tx_hash: "0x1",
        },
      },
      "/api/escrow/7/refund": { body: { tx_hash: "0x2" } },
    });
    const user = await open();
    await user.click(screen.getByRole("button", { name: "Create link" }));
    await user.click(await screen.findByRole("button", { name: "Cancel and refund" }));
    expect(await screen.findByText("Refunded to your wallet.")).toBeInTheDocument();
    expect(calls.at(-1)?.url).toBe("/api/escrow/7/refund");
  });

  it("shows the server's one-line reason for a 409 and a short one for a 503", async () => {
    respond({
      "/api/escrow/create": { status: 409, body: { detail: "over the per-transfer limit of $100; ask for approval" } },
    });
    const user = await open();
    await user.click(screen.getByRole("button", { name: "Create link" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("over the per-transfer limit of $100");

    respond({ "/api/escrow/create": { status: 503, body: { detail: "escrow is not configured" } } });
    await user.click(screen.getByRole("button", { name: "Create link" }));
    expect(await screen.findByText("Send by link is not set up on this server.")).toBeInTheDocument();
  });
});

describe("ClaimPage", () => {
  it("parses only a well-formed fragment", () => {
    expect(parseClaim(`#7:0x${SECRET}`)).toEqual({ id: "7", secret: `0x${SECRET}` });
    expect(parseClaim(`#7:${SECRET}`)).toEqual({ id: "7", secret: `0x${SECRET}` });
    expect(parseClaim("#7:abc")).toBeNull();
    expect(parseClaim("")).toBeNull();
  });

  it("peeks with the fragment, validates the address, claims, and links the transaction", async () => {
    window.location.hash = `7:${SECRET}`;
    const calls = respond({
      "/api/escrow/peek": { body: { amount_usd: 5, expiry: future(), settled: false } },
      "/api/escrow/claim": { body: { tx_hash: "0xabc" } },
    });
    const user = userEvent.setup();
    render(<ClaimPage />);

    expect(await screen.findByText("$5")).toBeInTheDocument();
    expect(calls[0]?.body).toEqual({ escrow_id: "7", secret: `0x${SECRET}` });
    const claim = screen.getByRole("button", { name: "Claim" });
    await user.type(screen.getByLabelText("Your Arc wallet address"), "0x123");
    expect(claim).toBeDisabled();
    await user.clear(screen.getByLabelText("Your Arc wallet address"));
    await user.type(screen.getByLabelText("Your Arc wallet address"), WALLET.toUpperCase().replace("0X", "0x"));
    await user.click(claim);

    const link = await screen.findByRole("link", { name: "View the transaction" });
    expect(link).toHaveAttribute("href", "https://testnet.arcscan.app/tx/0xabc");
    expect(calls.at(-1)?.body).toMatchObject({ escrow_id: "7", to: expect.stringMatching(/^0x/) });
    expect(calls.every((call) => !call.url.includes("?") && !call.url.includes(SECRET))).toBe(true);
    // jsdom will not let a test spy on location.search, so check the source instead.
    const source = readFileSync("src/ClaimPage.tsx", "utf8");
    expect(source).not.toMatch(/location\.search|URLSearchParams/);
  });

  it("explains an already-claimed link and refuses a malformed one", async () => {
    window.location.hash = `7:${SECRET}`;
    respond({
      "/api/escrow/peek": { body: { amount_usd: 5, expiry: future(), settled: false } },
      "/api/escrow/claim": { status: 409, body: { detail: "Escrow reverted: AlreadySettled" } },
    });
    const user = userEvent.setup();
    const { unmount } = render(<ClaimPage />);
    await user.type(await screen.findByLabelText("Your Arc wallet address"), WALLET);
    await user.click(screen.getByRole("button", { name: "Claim" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already been claimed");
    unmount();

    window.location.hash = "7:nope";
    const calls = respond({});
    render(<ClaimPage />);
    expect(screen.getByRole("alert")).toHaveTextContent("not valid");
    expect(screen.queryByRole("button", { name: "Claim" })).toBeNull();
    expect(calls).toHaveLength(0);
  });
});
