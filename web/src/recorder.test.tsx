import { act, fireEvent, render, screen } from "@testing-library/react";

import { FlightRecorder, replayDelay, type ReplayFrame } from "./components/FlightRecorder";
import { event } from "./test-fixtures";
import type { EventOf, SwitchboardEvent } from "./types";

const sent = (ts: string) =>
  event<EventOf<"transfer.sent">>(
    "transfer.sent",
    {
      transfer_id: "t",
      plan_id: "p_1",
      from_chain: "eth-sepolia",
      to_chain: "solana-devnet",
      amount_usd: 200,
      payee_label: "Sam's Solana wallet",
      route_id: "fastest",
      eta_s: 20,
    },
    ts,
  );

const tape: SwitchboardEvent[] = [
  sent("2026-11-19T12:00:00+00:00"),
  event<EventOf<"frozen">>("frozen", { frozen: true, via: "voice" }, "2026-11-19T12:01:00+00:00"),
  event<EventOf<"blocked">>(
    "blocked",
    { reason: "FROZEN", plan_id: "p_2", amount_usd: 40, payee_label: "Sam's Arc wallet", detail: "" },
    "2026-11-19T13:01:00+00:00",
  ),
];

describe("replayDelay", () => {
  it("compresses real gaps and clamps both ends", () => {
    const start = "2026-11-19T12:00:00+00:00";
    // One minute becomes one second at 1x.
    expect(replayDelay(start, "2026-11-19T12:01:00+00:00", 1)).toBe(1000);
    // A quiet hour does not stall the replay.
    expect(replayDelay(start, "2026-11-19T13:00:00+00:00", 1)).toBe(1800);
    // A burst is still visible.
    expect(replayDelay(start, start, 1)).toBe(250);
    expect(replayDelay(start, "2026-11-19T13:00:00+00:00", 4)).toBe(450);
  });
});

describe("FlightRecorder", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("replays the recording event by event, then returns to live", () => {
    const frames: (ReplayFrame | null)[] = [];
    render(<FlightRecorder events={tape} onFrame={(frame) => frames.push(frame)} />);
    expect(screen.getByRole("status")).toHaveTextContent("3 events recorded");

    fireEvent.click(screen.getByRole("button", { name: "Replay today" }));
    // Starting a replay clears the ring first.
    expect(frames.at(-1)).toMatchObject({ signals: [], resetKey: 1 });

    act(() => vi.advanceTimersByTime(1));
    expect(frames.at(-1)?.signals).toHaveLength(1);
    expect(screen.getByRole("status")).toHaveTextContent("Event 1 of 3");
    expect(screen.getByText(/\$200 leaving \w+ for Solana/)).toBeInTheDocument();

    act(() => vi.advanceTimersByTime(replayDelay(tape[0]!.ts, tape[1]!.ts, 4) + 1));
    expect(frames.at(-1)?.signals).toHaveLength(2);
    act(() => vi.advanceTimersByTime(replayDelay(tape[1]!.ts, tape[2]!.ts, 4) + 1));
    const last = frames.at(-1);
    expect(last?.signals.map((item) => item.type)).toEqual(["transfer.sent", "frozen", "blocked"]);
    // Replayed events are copies, so the ring animates them even if it saw the originals live.
    expect(last?.signals[0]).not.toBe(tape[0]);
    expect(screen.getByRole("status")).toHaveTextContent("Event 3 of 3");

    fireEvent.click(screen.getByRole("button", { name: "Back to live" }));
    expect(frames.at(-1)).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("The ring is showing live activity.");
  });

  it("jumping on the scrub bar pauses, clears the ring and shows that moment", () => {
    const frames: (ReplayFrame | null)[] = [];
    render(<FlightRecorder events={tape} onFrame={(frame) => frames.push(frame)} />);
    fireEvent.change(screen.getByLabelText("Position in the recording"), { target: { value: "2" } });
    expect(frames.at(-1)).toMatchObject({ signals: [], resetKey: 1 });
    expect(screen.getByRole("status")).toHaveTextContent("Event 2 of 3");
    expect(screen.getByText(/The vault is frozen/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    act(() => vi.advanceTimersByTime(2000));
    expect(frames.at(-1)?.signals.map((item) => item.type)).toEqual(["blocked"]);
  });

  it("has nothing to replay before anything has happened", () => {
    render(<FlightRecorder events={[]} onFrame={() => undefined} />);
    expect(screen.getByRole("button", { name: "Replay today" })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("Nothing recorded yet.");
  });
});
