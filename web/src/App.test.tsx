import { render, screen } from "@testing-library/react";

import { App } from "./App";
import { event, pendingApproval, stats } from "./test-fixtures";
import type { EventOf } from "./types";

const BODIES: Record<string, unknown> = {
  "/api/stats": stats,
  "/api/approvals": { approvals: [pendingApproval] },
  "/api/wallets": { wallets: [] },
  "/api/venues": { venues: [] },
};

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/events/history")) {
        return new Response(
          JSON.stringify({
            events: [event<EventOf<"frozen">>("frozen", { frozen: true, via: "voice" })],
          }),
        );
      }
      const body = Object.entries(BODIES).find(([path]) => url.startsWith(path))?.[1];
      return new Response(JSON.stringify(body ?? {}), { status: body ? 200 : 404 });
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

test("loads stored history into the feed on start", async () => {
  render(<App />);
  expect(screen.getByRole("heading", { name: "Switchboard" })).toBeInTheDocument();
  expect(await screen.findByText("The vault is frozen")).toBeInTheDocument();
});
