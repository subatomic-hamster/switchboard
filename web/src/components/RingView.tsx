import { useEffect, useReducer, useRef, useState } from "react";

import type { Tone } from "../format";
import {
  applyEvent,
  CENTER,
  EMPTY,
  isAnimating,
  NODES,
  nodePoint,
  place,
  polar,
  prune,
  RING_RADIUS,
  type RingState,
  TIMING,
  VIEW,
} from "../ring";
import { playTone } from "../speech";
import type { SwitchboardEvent, Wallet } from "../types";
import { StatusMark } from "./StatusMark";

const TONE_VAR: Record<Tone, string> = {
  good: "var(--good)",
  warning: "var(--warning)",
  critical: "var(--critical)",
  neutral: "var(--muted)",
};

export interface RingViewProps {
  /**
   * Events to animate, oldest first: live ones, or replayed ones from the flight recorder.
   * Each event object is animated once, however many arrive in one render.
   */
  signals: SwitchboardEvent[];
  wallets: Wallet[];
  frozen: boolean;
  stale: boolean;
  /** Shown over the ring while the flight recorder is replaying. */
  replayLabel?: string;
  /** When this changes, everything on the ring is cleared (a replay starts or jumps). */
  resetKey?: number;
}

function arc(angle: number, spread: number): string {
  const from = polar(angle - spread, RING_RADIUS);
  const to = polar(angle + spread, RING_RADIUS);
  return `M ${from.x} ${from.y} A ${RING_RADIUS} ${RING_RADIUS} 0 0 1 ${to.x} ${to.y}`;
}

/**
 * The vault sits inside a ring. A transfer the vault allows passes through the ring to its
 * destination. One that needs approval stops at the ring and waits, with a flash and a tone.
 * One that is blocked stops at the ring and fades.
 */
export function RingView({
  signals,
  wallets,
  frozen,
  stale,
  replayLabel,
  resetKey = 0,
}: RingViewProps) {
  const stateRef = useRef<RingState>(EMPTY);
  const [, redraw] = useReducer((count: number) => count + 1, 0);
  const [sound, setSound] = useState(true);
  const soundRef = useRef(sound);
  soundRef.current = sound;
  const seen = useRef(new WeakSet<SwitchboardEvent>());
  const frame = useRef<number | null>(null);
  const walletsRef = useRef(wallets);
  walletsRef.current = wallets;

  useEffect(() => {
    stateRef.current = EMPTY;
    seen.current = new WeakSet();
    redraw();
  }, [resetKey]);

  useEffect(() => {
    const tick = () => {
      stateRef.current = prune(stateRef.current, performance.now());
      redraw();
      frame.current = isAnimating(stateRef.current) ? requestAnimationFrame(tick) : null;
    };
    const fresh = signals.filter((signal) => !seen.current.has(signal));
    if (fresh.length === 0) return;
    for (const signal of fresh) {
      seen.current.add(signal);
      const result = applyEvent(stateRef.current, signal, performance.now(), walletsRef.current);
      stateRef.current = result.state;
      if (soundRef.current) playTone(result.sound);
    }
    redraw();
    if (frame.current === null && typeof requestAnimationFrame !== "undefined") {
      frame.current = requestAnimationFrame(tick);
    }
  }, [signals]);

  useEffect(
    () => () => {
      if (frame.current !== null) cancelAnimationFrame(frame.current);
    },
    [],
  );

  const now = typeof performance !== "undefined" ? performance.now() : 0;
  const state = stateRef.current;
  const paused = frozen || stale;
  const held = state.particles.filter((particle) => particle.kind === "held").length;

  return (
    <section className="panel ring-panel" aria-labelledby="ring-title">
      <div className="panel-head">
        <h2 id="ring-title">The ring</h2>
        <label className="toggle">
          <input
            type="checkbox"
            checked={sound}
            onChange={(event) => setSound(event.target.checked)}
          />
          Sound
        </label>
      </div>
      <svg
        className="ring"
        viewBox={`0 0 ${VIEW.width} ${VIEW.height}`}
        role="img"
        aria-label={`Vault ring. ${paused ? "Spending is paused. " : ""}${held} transfer${
          held === 1 ? "" : "s"
        } waiting for approval.`}
      >
        {NODES.map((node) => {
          const point = nodePoint(node.id);
          const edge = polar(node.angle, RING_RADIUS);
          return (
            <line
              key={`spoke-${node.id}`}
              x1={edge.x}
              y1={edge.y}
              x2={point.x}
              y2={point.y}
              className="ring-spoke"
            />
          );
        })}

        <circle
          cx={CENTER.x}
          cy={CENTER.y}
          r={RING_RADIUS}
          className={`ring-line${paused ? " ring-line-paused" : ""}`}
        />
        {state.flashes.map((flash, index) => {
          const age = now - flash.at;
          if (age < 0) return null;
          const strength = Math.max(0, 1 - age / TIMING.flash);
          return (
            <g key={`flash-${index}-${flash.at}`} opacity={strength}>
              <circle
                cx={CENTER.x}
                cy={CENTER.y}
                r={RING_RADIUS}
                fill="none"
                stroke={TONE_VAR[flash.tone]}
                strokeWidth={2 + 6 * strength}
              />
              <path
                d={arc(flash.angle, 22)}
                fill="none"
                stroke={TONE_VAR[flash.tone]}
                strokeWidth={10}
                strokeLinecap="round"
              />
            </g>
          );
        })}

        <g className="vault">
          <rect x={CENTER.x - 34} y={CENTER.y - 26} width={68} height={52} rx={10} />
          <text x={CENTER.x} y={CENTER.y - 2} textAnchor="middle" className="vault-label">
            Vault
          </text>
          <text x={CENTER.x} y={CENTER.y + 14} textAnchor="middle" className="node-sub">
            {frozen ? "frozen" : stale ? "paused" : "on Arc"}
          </text>
        </g>

        {NODES.map((node) => {
          const point = nodePoint(node.id);
          const pulse = state.pulses.find((item) => item.node === node.id);
          const pulseStrength = pulse ? Math.max(0, 1 - (now - pulse.at) / TIMING.pulse) : 0;
          const below = point.y > CENTER.y;
          return (
            <g key={node.id}>
              {pulseStrength > 0 && (
                <circle
                  cx={point.x}
                  cy={point.y}
                  r={14 + 16 * (1 - pulseStrength)}
                  fill="none"
                  stroke="var(--good)"
                  strokeWidth={2}
                  opacity={pulseStrength}
                />
              )}
              <circle cx={point.x} cy={point.y} r={12} className="ring-node" />
              <text
                x={point.x}
                y={point.y + (below ? 30 : -30)}
                textAnchor="middle"
                className="node-label"
              >
                {node.label}
              </text>
              <text
                x={point.x}
                y={point.y + (below ? 44 : -17)}
                textAnchor="middle"
                className="node-sub"
              >
                {node.sub}
              </text>
            </g>
          );
        })}

        {state.particles.map((particle) => {
          const position = place(particle, now);
          if (position.opacity <= 0) return null;
          const waiting = particle.kind === "held" && position.atRing;
          const breathe = waiting ? 1 + 0.18 * Math.sin(now / 220) : 1;
          return (
            <g key={particle.key} opacity={position.opacity}>
              <circle
                cx={position.x}
                cy={position.y}
                r={7 * breathe}
                fill={TONE_VAR[particle.tone]}
                className="particle"
              />
              {particle.label && (
                <text x={position.x} y={position.y - 13} textAnchor="middle" className="particle-label">
                  {particle.label}
                </text>
              )}
            </g>
          );
        })}

        {replayLabel && (
          <text x={12} y={VIEW.height - 12} textAnchor="start" className="replay-label">
            {replayLabel}
          </text>
        )}
      </svg>
      <div className="legend">
        <StatusMark tone="good" label="Passed through" />
        <StatusMark tone="warning" label="Held for your approval" />
        <StatusMark tone="critical" label="Blocked or denied" />
        <StatusMark tone="neutral" label="Position move (dry run)" />
      </div>
    </section>
  );
}
