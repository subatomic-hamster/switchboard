// The ring view's model: where things sit, and what each event does on screen.
// Pure functions, so the motion can be tested without a browser.
import type { Tone } from "./format";
import type { SwitchboardEvent, Wallet } from "./types";

export const VIEW = { width: 640, height: 440 };
export const CENTER = { x: 320, y: 220 };
export const RING_RADIUS = 104;
export const NODE_RADIUS = 172;

export type NodeId = "eth-sepolia" | "solana-devnet" | "arc-testnet" | "new" | "polymarket" | "gemini";

export interface RingNode {
  id: NodeId;
  label: string;
  sub: string;
  angle: number; // degrees, 0 = right, clockwise
}

export const NODES: RingNode[] = [
  { id: "eth-sepolia", label: "Ethereum", sub: "wallets", angle: 210 },
  { id: "solana-devnet", label: "Solana", sub: "wallets", angle: 270 },
  { id: "arc-testnet", label: "Arc", sub: "wallets", angle: 330 },
  { id: "new", label: "New address", sub: "never paid before", angle: 30 },
  { id: "gemini", label: "Gemini", sub: "venue", angle: 90 },
  { id: "polymarket", label: "Polymarket US", sub: "venue", angle: 150 },
];

export interface Point {
  x: number;
  y: number;
}

export function polar(angle: number, radius: number): Point {
  const radians = (angle * Math.PI) / 180;
  return { x: CENTER.x + radius * Math.cos(radians), y: CENTER.y + radius * Math.sin(radians) };
}

export function nodePoint(id: NodeId): Point {
  const node = NODES.find((item) => item.id === id) ?? NODES[0]!;
  return polar(node.angle, NODE_RADIUS);
}

function nodeAngle(id: NodeId): number {
  return (NODES.find((item) => item.id === id) ?? NODES[0]!).angle;
}

/** What a particle is doing. Times are milliseconds on the view's clock. */
export type ParticleKind =
  | "pass" // vault -> through the ring -> destination
  | "held" // vault -> ring, waits for the owner's answer
  | "released" // was held, approved: the gate opens and it fades (the transfer follows)
  | "rejected" // was held or blocked: stops at the ring and fades
  | "inbound"; // venue -> vault (the sell leg of a position move)

export interface Particle {
  key: string;
  kind: ParticleKind;
  node: NodeId;
  tone: Tone;
  label: string;
  start: number;
  /** When a held particle was resolved. */
  settled?: number;
  requestId?: string;
}

export interface Flash {
  tone: Tone;
  at: number;
  angle: number;
}

export interface Pulse {
  node: NodeId;
  at: number;
}

export interface RingState {
  particles: Particle[];
  flashes: Flash[];
  pulses: Pulse[];
  /** Transfers sent but not yet arrived; their destination node shows a pulsing ring. */
  inFlight: { id: string; node: NodeId; at: number }[];
  frozen: boolean;
  stale: boolean;
}

export const EMPTY: RingState = {
  particles: [],
  flashes: [],
  pulses: [],
  inFlight: [],
  frozen: false,
  stale: false,
};

export const TIMING = {
  pass: 1700,
  toRing: 650,
  fade: 700,
  flash: 900,
  pulse: 900,
  inbound: 1300,
  /** Give up on an "in flight" marker whose arrival never showed up. */
  inFlight: 15 * 60_000,
};

export type Sound = "pass" | "ask" | "block" | null;

function nodeForAddress(address: string, wallets: Wallet[]): NodeId {
  const wallet = wallets.find((item) => item.address.toLowerCase() === address.toLowerCase());
  return wallet ? wallet.chain : "new";
}

function nodeForLabel(label: string, wallets: Wallet[]): NodeId {
  const wallet = wallets.find((item) => item.label === label);
  if (wallet) return wallet.chain;
  return label.toLowerCase().startsWith("new address") ? "new" : "arc-testnet";
}

function venueNode(venue: string): NodeId {
  return venue === "gemini" ? "gemini" : "polymarket";
}

const dollars = (amount: number) =>
  `$${Number.isInteger(amount) ? amount : amount.toFixed(2)}`;

/** Apply one event. Returns the next state and the tone to play, if any. */
export function applyEvent(
  state: RingState,
  event: SwitchboardEvent,
  now: number,
  wallets: Wallet[],
): { state: RingState; sound: Sound } {
  const key = `e${event.id}`;
  switch (event.type) {
    case "transfer.sent": {
      const d = event.data;
      const node: NodeId = d.payee_label.toLowerCase().startsWith("new address")
        ? "new"
        : d.to_chain;
      const particle: Particle = {
        key,
        kind: "pass",
        node,
        tone: "good",
        label: dollars(d.amount_usd),
        start: now,
      };
      return {
        state: {
          ...state,
          particles: [...state.particles, particle],
          inFlight: [...state.inFlight, { id: d.transfer_id, node, at: now }],
        },
        sound: "pass",
      };
    }
    case "transfer.attested": {
      const d = event.data;
      // Normally already in flight from `sent`; add it if the sent event was missed.
      if (state.inFlight.some((item) => item.id === d.transfer_id)) return { state, sound: null };
      return {
        state: {
          ...state,
          inFlight: [...state.inFlight, { id: d.transfer_id, node: d.to_chain, at: now }],
        },
        sound: null,
      };
    }
    case "transfer.arrived":
      return {
        state: {
          ...state,
          pulses: [...state.pulses, { node: event.data.to_chain, at: now }],
          inFlight: state.inFlight.filter((item) => item.id !== event.data.transfer_id),
        },
        sound: null,
      };
    case "approval.requested": {
      const d = event.data;
      const node = nodeForAddress(d.payee, wallets);
      const particle: Particle = {
        key,
        kind: "held",
        node,
        tone: "warning",
        label: dollars(d.amount_usd),
        start: now,
        requestId: d.request_id,
      };
      return {
        state: {
          ...state,
          particles: [...state.particles, particle],
          flashes: [
            ...state.flashes,
            { tone: "warning", at: now + TIMING.toRing, angle: nodeAngle(node) },
          ],
        },
        sound: "ask",
      };
    }
    case "approval.resolved": {
      const approved = event.data.approved;
      let angle: number | null = null;
      const particles = state.particles.map((particle) => {
        if (particle.kind !== "held" || particle.requestId !== event.data.request_id) {
          return particle;
        }
        angle = nodeAngle(particle.node);
        return {
          ...particle,
          kind: approved ? ("released" as const) : ("rejected" as const),
          tone: approved ? ("good" as const) : ("critical" as const),
          // A rejected particle is already at the ring; start its fade from there.
          start: approved ? particle.start : now - TIMING.toRing,
          settled: now,
        };
      });
      const flashes =
        angle === null
          ? state.flashes
          : [...state.flashes, { tone: approved ? ("good" as const) : ("critical" as const), at: now, angle }];
      return { state: { ...state, particles, flashes }, sound: approved ? null : "block" };
    }
    case "blocked": {
      const d = event.data;
      const node = nodeForLabel(d.payee_label, wallets);
      const particle: Particle = {
        key,
        kind: "rejected",
        node,
        tone: "critical",
        label: d.amount_usd != null ? dollars(d.amount_usd) : "",
        start: now,
        settled: now + TIMING.toRing,
      };
      return {
        state: {
          ...state,
          particles: [...state.particles, particle],
          flashes: [
            ...state.flashes,
            { tone: "critical", at: now + TIMING.toRing, angle: nodeAngle(node) },
          ],
        },
        sound: "block",
      };
    }
    case "trade.filled": {
      const d = event.data;
      const particle: Particle = {
        key,
        kind: d.side === "sell" ? "inbound" : "pass",
        node: venueNode(d.venue),
        tone: "neutral",
        label: `${d.qty} @ ${d.price.toFixed(2)}`,
        start: now,
      };
      return { state: { ...state, particles: [...state.particles, particle] }, sound: null };
    }
    case "escrow.updated": {
      // A link payment: leaves for a new address, lights it when claimed, comes back if refunded.
      const d = event.data;
      if (d.status === "claimed") {
        return { state: { ...state, pulses: [...state.pulses, { node: "new", at: now }] }, sound: null };
      }
      const particle: Particle = {
        key,
        kind: d.status === "refunded" ? "inbound" : "pass",
        node: "new",
        tone: d.status === "refunded" ? "neutral" : "good",
        label: dollars(d.amount_usd),
        start: now,
      };
      return {
        state: { ...state, particles: [...state.particles, particle] },
        sound: d.status === "deposited" ? "pass" : null,
      };
    }
    case "frozen":
      return { state: { ...state, frozen: event.data.frozen }, sound: event.data.frozen ? "block" : null };
    case "stream.stale":
      return { state: { ...state, stale: event.data.stale }, sound: event.data.stale ? "block" : null };
    default:
      return { state, sound: null };
  }
}

function lifetime(particle: Particle): number {
  switch (particle.kind) {
    case "pass":
      return TIMING.pass + TIMING.fade;
    case "inbound":
      return TIMING.inbound + TIMING.fade;
    case "held":
      return Number.POSITIVE_INFINITY;
    case "released":
    case "rejected":
      return (particle.settled ?? particle.start) - particle.start + TIMING.fade + TIMING.toRing;
  }
}

/** Drop everything that has finished animating. */
export function prune(state: RingState, now: number): RingState {
  const particles = state.particles.filter((p) => now - p.start < lifetime(p));
  const flashes = state.flashes.filter((f) => now - f.at < TIMING.flash);
  const pulses = state.pulses.filter((p) => now - p.at < TIMING.pulse);
  const inFlight = state.inFlight.filter((item) => now - item.at < TIMING.inFlight);
  if (
    particles.length === state.particles.length &&
    flashes.length === state.flashes.length &&
    pulses.length === state.pulses.length &&
    inFlight.length === state.inFlight.length
  ) {
    return state;
  }
  return { ...state, particles, flashes, pulses, inFlight };
}

export function isAnimating(state: RingState): boolean {
  return state.particles.length > 0 || state.flashes.length > 0 || state.pulses.length > 0;
}

const ease = (t: number) => (t <= 0 ? 0 : t >= 1 ? 1 : 1 - (1 - t) ** 3);

function along(from: Point, to: Point, t: number): Point {
  return { x: from.x + (to.x - from.x) * t, y: from.y + (to.y - from.y) * t };
}

export interface Placed extends Point {
  opacity: number;
  atRing: boolean;
}

/** Where a particle is at time `now`. */
export function place(particle: Particle, now: number): Placed {
  const angle = nodeAngle(particle.node);
  const ring = polar(angle, RING_RADIUS);
  const node = nodePoint(particle.node);
  const elapsed = now - particle.start;
  switch (particle.kind) {
    case "pass": {
      const t = ease(elapsed / TIMING.pass);
      const fade = Math.max(0, 1 - Math.max(0, elapsed - TIMING.pass) / TIMING.fade);
      return { ...along(CENTER, node, t), opacity: fade, atRing: false };
    }
    case "inbound": {
      const t = ease(elapsed / TIMING.inbound);
      const fade = Math.max(0, 1 - Math.max(0, elapsed - TIMING.inbound) / TIMING.fade);
      return { ...along(node, CENTER, t), opacity: fade, atRing: false };
    }
    case "held": {
      const t = ease(elapsed / TIMING.toRing);
      return { ...along(CENTER, ring, t), opacity: 1, atRing: t >= 1 };
    }
    case "released":
    case "rejected": {
      const t = ease(elapsed / TIMING.toRing);
      const settled = particle.settled ?? particle.start;
      const fade = Math.max(0, 1 - Math.max(0, now - settled) / TIMING.fade);
      return { ...along(CENTER, ring, t), opacity: now < settled ? 1 : fade, atRing: t >= 1 };
    }
  }
}
