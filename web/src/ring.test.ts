import {
  applyEvent,
  CENTER,
  EMPTY,
  isAnimating,
  nodePoint,
  place,
  polar,
  prune,
  RING_RADIUS,
  TIMING,
} from "./ring";
import { event } from "./test-fixtures";
import type { EventOf, Wallet } from "./types";

const wallets: Wallet[] = [
  {
    owner: "sam",
    label: "Sam's Solana wallet",
    chain: "solana-devnet",
    address: "Sam11111111111111111111111111111111111111111",
    allowlisted: true,
  },
];

const distance = (point: { x: number; y: number }) =>
  Math.hypot(point.x - CENTER.x, point.y - CENTER.y);

describe("ring model", () => {
  it("a transfer passes through the ring to its destination, then fades", () => {
    const sent = event<EventOf<"transfer.sent">>("transfer.sent", {
      transfer_id: "t_1",
      plan_id: "p_1",
      from_chain: "eth-sepolia",
      to_chain: "solana-devnet",
      amount_usd: 200,
      payee_label: "Sam's Solana wallet",
      route_id: "fastest",
      eta_s: 20,
    });
    const { state, sound } = applyEvent(EMPTY, sent, 1000, wallets);
    expect(sound).toBe("pass");
    const particle = state.particles[0]!;
    expect(particle).toMatchObject({ kind: "pass", node: "solana-devnet", tone: "good", label: "$200" });

    expect(distance(place(particle, 1000))).toBeCloseTo(0);
    const end = place(particle, 1000 + TIMING.pass);
    expect(end.x).toBeCloseTo(nodePoint("solana-devnet").x);
    expect(end.y).toBeCloseTo(nodePoint("solana-devnet").y);
    expect(distance(end)).toBeGreaterThan(RING_RADIUS);
    expect(end.opacity).toBe(1);
    expect(prune(state, 1000 + TIMING.pass + TIMING.fade + 1).particles).toHaveLength(0);
  });

  it("a request for approval stops at the ring, flashes, and waits", () => {
    const requested = event<EventOf<"approval.requested">>("approval.requested", {
      request_id: "7",
      agent: "0xa",
      payee: "0x9c1e8a4f2b7d6c5a3e1f0b9d8c7a6e5f4d3c4f2c",
      amount_usd: 250,
      reason: "OVER_TRANSFER_CAP",
    });
    const { state, sound } = applyEvent(EMPTY, requested, 0, wallets);
    expect(sound).toBe("ask");
    const particle = state.particles[0]!;
    expect(particle).toMatchObject({ kind: "held", node: "new", tone: "warning", requestId: "7" });
    expect(state.flashes[0]).toMatchObject({ tone: "warning", at: TIMING.toRing });

    const waiting = place(particle, 60_000);
    expect(distance(waiting)).toBeCloseTo(RING_RADIUS);
    expect(waiting.atRing).toBe(true);
    // Still there a minute later: it does not expire on its own.
    expect(prune(state, 60_000).particles).toHaveLength(1);
    expect(isAnimating(prune(state, 60_000))).toBe(true);
  });

  it("an approval opens the gate; a denial turns the held transfer red", () => {
    const requested = event<EventOf<"approval.requested">>("approval.requested", {
      request_id: "7",
      agent: "0xa",
      payee: "Sam11111111111111111111111111111111111111111",
      amount_usd: 250,
      reason: "OVER_TRANSFER_CAP",
    });
    const held = applyEvent(EMPTY, requested, 0, wallets).state;
    expect(held.particles[0]!.node).toBe("solana-devnet");

    const approved = applyEvent(
      held,
      event<EventOf<"approval.resolved">>("approval.resolved", {
        request_id: "7",
        approved: true,
        via: "voice",
      }),
      5000,
      wallets,
    );
    expect(approved.sound).toBeNull();
    expect(approved.state.particles[0]).toMatchObject({ kind: "released", tone: "good" });
    expect(prune(approved.state, 5000 + TIMING.fade + TIMING.toRing + 1).particles).toHaveLength(0);

    const denied = applyEvent(
      held,
      event<EventOf<"approval.resolved">>("approval.resolved", {
        request_id: "7",
        approved: false,
        via: "expiry",
      }),
      5000,
      wallets,
    );
    expect(denied.sound).toBe("block");
    expect(denied.state.particles[0]).toMatchObject({ kind: "rejected", tone: "critical" });
    expect(denied.state.flashes.at(-1)).toMatchObject({ tone: "critical", at: 5000 });
  });

  it("a blocked transfer never gets past the ring", () => {
    const blocked = event<EventOf<"blocked">>("blocked", {
      reason: "FROZEN",
      plan_id: "p_3",
      amount_usd: 200,
      payee_label: "Sam's Solana wallet",
      detail: "The vault is frozen.",
    });
    const { state, sound } = applyEvent(EMPTY, blocked, 0, wallets);
    expect(sound).toBe("block");
    const particle = state.particles[0]!;
    for (const at of [0, 200, TIMING.toRing, TIMING.toRing + 300, 5000]) {
      expect(distance(place(particle, at))).toBeLessThanOrEqual(RING_RADIUS + 0.001);
    }
    expect(place(particle, TIMING.toRing + TIMING.fade).opacity).toBe(0);
  });

  it("a position move runs venue to vault, then vault to venue", () => {
    const sell = event<EventOf<"trade.filled">>("trade.filled", {
      move_id: "m_1",
      venue: "polymarket",
      slug: "s",
      side: "sell",
      qty: 4,
      price: 0.46,
      dry_run: true,
    });
    const { state } = applyEvent(EMPTY, sell, 0, wallets);
    const particle = state.particles[0]!;
    expect(particle).toMatchObject({ kind: "inbound", node: "polymarket", tone: "neutral" });
    expect(place(particle, 0).x).toBeCloseTo(nodePoint("polymarket").x);
    expect(distance(place(particle, TIMING.inbound))).toBeCloseTo(0);
  });

  it("freeze and stale-stream events set the paused flags", () => {
    const frozen = applyEvent(
      EMPTY,
      event<EventOf<"frozen">>("frozen", { frozen: true, via: "voice" }),
      0,
      wallets,
    );
    expect(frozen.state.frozen).toBe(true);
    expect(frozen.sound).toBe("block");
    const stale = applyEvent(
      EMPTY,
      event<EventOf<"stream.stale">>("stream.stale", { stale: true, seconds_since_delivery: 120 }),
      0,
      wallets,
    );
    expect(stale.state.stale).toBe(true);
  });

  it.each(["eth-sepolia", "solana-devnet"] as const)(
    "arc to %s: lights the destination, in flight from sent until arrived",
    (to) => {
      const base = { transfer_id: "t_9", to_chain: to, amount_usd: 1 };
      const sent = applyEvent(
        EMPTY,
        event<EventOf<"transfer.sent">>("transfer.sent", {
          ...base, from_chain: "arc-testnet", payee_label: "", route_id: "", eta_s: 0,
        }),
        0,
        wallets,
      ).state;
      expect(sent.particles[0]).toMatchObject({ node: to });
      expect(sent.inFlight).toEqual([{ id: "t_9", node: to, at: 0 }]);
      const attested = applyEvent(
        sent,
        event<EventOf<"transfer.attested">>("transfer.attested", {
          ...base, tx_hash: "", forwarding: true, detail: "",
        }),
        100,
        wallets,
      ).state;
      expect(attested.inFlight).toHaveLength(1);
      const arrived = applyEvent(
        attested,
        event<EventOf<"transfer.arrived">>("transfer.arrived", { ...base, tx_hash: "" }),
        200,
        wallets,
      ).state;
      expect(arrived.inFlight).toEqual([]);
      expect(arrived.pulses).toEqual([{ node: to, at: 200 }]);
      // A missed `sent` still shows in flight once attested; stale markers expire.
      const lone = applyEvent(
        EMPTY,
        event<EventOf<"transfer.attested">>("transfer.attested", {
          ...base, tx_hash: "", forwarding: false, detail: "",
        }),
        0,
        wallets,
      ).state;
      expect(lone.inFlight[0]).toMatchObject({ node: to });
      expect(prune(lone, TIMING.inFlight + 1).inFlight).toEqual([]);
    },
  );

  it("places nodes on a circle around the vault", () => {
    expect(distance(polar(0, 100))).toBeCloseTo(100);
    expect(polar(270, 100).y).toBeCloseTo(CENTER.y - 100);
  });
});
