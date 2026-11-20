import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { App } from "./App";
import { event, pendingApproval, stats } from "./test-fixtures";
import type { ApprovalView, EventOf } from "./types";

let approvals: ApprovalView[] = [];
let stale = false;
const posts: { url: string; body: unknown }[] = [];

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

beforeEach(() => {
  approvals = [];
  stale = false;
  posts.length = 0;
  window.location.hash = "";
  vi.stubGlobal("EventSource", FakeSource);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === "POST") {
        posts.push({ url, body: init.body ? JSON.parse(String(init.body)) : undefined });
        return new Response(JSON.stringify({ ok: true, status: "frozen", error: "" }));
      }
      if (url.startsWith("/api/events/history")) {
        return new Response(
          JSON.stringify({
            events: [event<EventOf<"frozen">>("frozen", { frozen: true, via: "voice" })],
          }),
        );
      }
      const bodies: Record<string, unknown> = {
        "/api/stats": { ...stats, stream_stale: stale },
        "/api/approvals": { approvals },
        "/api/wallets": { wallets: [] },
      };
      const body = Object.entries(bodies).find(([path]) => url.startsWith(path))?.[1];
      return new Response(JSON.stringify(body ?? {}), { status: body ? 200 : 404 });
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

test("loads stored history into the feed on start", async () => {
  window.location.hash = "#activity";
  render(<App />);
  expect(screen.getByText("Switchboard")).toBeInTheDocument();
  expect(await screen.findByText("The vault is frozen")).toBeInTheDocument();
});

test("the tabs switch the page, and the choice is kept in the URL hash", async () => {
  const user = userEvent.setup();
  render(<App />);
  const nav = screen.getByRole("navigation", { name: "Main" });
  expect(within(nav).getByRole("link", { name: "Home" })).toHaveAttribute("aria-current", "page");
  expect(screen.getByLabelText("Where should the money go?")).toBeVisible();

  await user.click(within(nav).getByRole("link", { name: "Markets" }));
  expect(await screen.findByRole("heading", { name: "Move a position" })).toBeInTheDocument();
  expect(window.location.hash).toBe("#markets");
  expect(within(nav).getByRole("link", { name: "Markets" })).toHaveAttribute("aria-current", "page");
  expect(screen.getByLabelText("Where should the money go?")).not.toBeVisible();

  await user.click(within(nav).getByRole("link", { name: "Controls" }));
  expect(await screen.findByRole("heading", { name: "Limits" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Health" })).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Move a position" })).not.toBeInTheDocument();
});

test("a reload opens the tab named in the hash", async () => {
  window.location.hash = "#controls";
  render(<App />);
  expect(await screen.findByRole("heading", { name: "Limits" })).toBeInTheDocument();
  expect(
    within(screen.getByRole("navigation", { name: "Main" })).getByRole("link", { name: "Controls" }),
  ).toHaveAttribute("aria-current", "page");
});

test("Freeze is in the header on every tab and freezes the vault", async () => {
  const user = userEvent.setup();
  render(<App />);
  const header = screen.getByRole("banner");
  await user.click(within(screen.getByRole("navigation", { name: "Main" })).getByRole("link", { name: "Markets" }));
  await user.click(await within(header).findByRole("button", { name: "Freeze" }));
  expect(posts).toContainEqual({ url: "/api/freeze", body: { frozen: true } });
});

test("the status chip is hidden when all is well and shown when spending is paused", async () => {
  stale = false;
  const { unmount } = render(<App />);
  await screen.findByRole("button", { name: "Freeze" });
  act(() => FakeSource.last?.onopen?.());
  await waitFor(() => expect(screen.queryByText("Connecting…")).not.toBeInTheDocument());
  expect(screen.queryByText("Live")).not.toBeInTheDocument();
  expect(screen.queryByText("Paused")).not.toBeInTheDocument();
  unmount();

  stale = true;
  render(<App />);
  act(() => FakeSource.last?.onopen?.());
  expect(await screen.findByText("Paused")).toBeInTheDocument();
});

test("a Needs-you card appears when an approval is requested, and not before", async () => {
  render(<App />);
  expect(await screen.findByText("The vault is frozen")).toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "Needs you" })).not.toBeInTheDocument();

  approvals = [pendingApproval];
  act(() =>
    FakeSource.last?.emit(
      "approval.requested",
      event<EventOf<"approval.requested">>("approval.requested", {
        request_id: "7",
        agent: "0xa",
        payee: "0xnew",
        amount_usd: 250,
        reason: "OVER_TRANSFER_CAP",
      }),
    ),
  );
  const card = await screen.findByRole("region", { name: "Needs you" });
  expect(within(card).getByText("OVER_TRANSFER_CAP")).toBeInTheDocument();
  expect(within(card).getByRole("button", { name: "Deny" })).toBeInTheDocument();
  expect(within(card).getByRole("button", { name: "Approve" })).toBeInTheDocument();
  // Announced to screen readers through the always-present status region.
  expect(await screen.findByText("1 request needs your approval.")).toBeInTheDocument();
});
