import { useEffect, useRef, useState } from "react";

import { api } from "../api";
import { clock, describe } from "../format";
import { canSpeak, speak } from "../speech";
import type { SwitchboardEvent } from "../types";
import { StatusMark } from "./StatusMark";

export interface ReplayFrame {
  /** Events to animate on the ring, oldest first. New objects, so each animates once. */
  signals: SwitchboardEvent[];
  label: string;
  /** Changes whenever the ring should be cleared (a new run, or a jump on the scrub bar). */
  resetKey: number;
}

const SPEEDS = [1, 4, 16];

/**
 * How long to wait before showing the next event. Real gaps are compressed (a minute becomes
 * about a second at 1x) and clamped, so a quiet hour does not stall the replay and a burst
 * does not blur past.
 */
export function replayDelay(previousTs: string, nextTs: string, speed: number): number {
  const gap = Math.max(0, new Date(nextTs).getTime() - new Date(previousTs).getTime());
  const compressed = gap / (60 * speed);
  return Math.round(Math.min(Math.max(compressed, 250 / speed), 1800 / speed));
}

export interface FlightRecorderProps {
  events: SwitchboardEvent[];
  /** Called with a frame while replaying, and with null when returning to live. */
  onFrame: (frame: ReplayFrame | null) => void;
  /** The activity list, shown between the controls and the scrub bar. */
  children?: React.ReactNode;
}

/** The Activity card: a replay over everything that has happened, shown on the ring. */
export function FlightRecorder({ events, onFrame, children }: FlightRecorderProps) {
  const [active, setActive] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [position, setPosition] = useState(0);
  const [speed, setSpeed] = useState(4);
  const [summary, setSummary] = useState("");
  const shown = useRef<SwitchboardEvent[]>([]);
  const resetKey = useRef(0);
  const onFrameRef = useRef(onFrame);
  onFrameRef.current = onFrame;
  // Freeze the list while replaying, so new live events do not move the bar under the thumb.
  const tape = useRef<SwitchboardEvent[]>([]);
  if (!active) tape.current = events;
  const total = tape.current.length;

  useEffect(() => {
    if (!playing) return;
    if (position >= total) {
      setPlaying(false);
      return;
    }
    const event = tape.current[position]!;
    const previous = tape.current[position - 1];
    const wait = previous ? replayDelay(previous.ts, event.ts, speed) : 0;
    const timer = setTimeout(() => {
      shown.current = [...shown.current, { ...event }];
      onFrameRef.current({
        signals: shown.current,
        label: `Replaying ${clock(event.ts)}`,
        resetKey: resetKey.current,
      });
      setPosition(position + 1);
    }, wait);
    return () => clearTimeout(timer);
  }, [playing, position, speed, total]);

  function restart(at: number) {
    shown.current = [];
    resetKey.current += 1;
    setPosition(at);
    const event = tape.current[Math.min(at, total - 1)];
    onFrameRef.current({
      signals: [],
      label: event ? `Paused at ${clock(event.ts)}` : "Nothing recorded yet",
      resetKey: resetKey.current,
    });
  }

  function play() {
    if (!active || position >= total) {
      setActive(true);
      restart(position >= total ? 0 : position);
    }
    setPlaying(true);
  }

  function scrub(value: number) {
    setActive(true);
    setPlaying(false);
    restart(value);
  }

  function backToLive() {
    setPlaying(false);
    setActive(false);
    shown.current = [];
    onFrameRef.current(null);
  }

  async function hearSummary() {
    try {
      const day = await api.summary();
      setSummary(day.spoken);
      if (canSpeak()) speak(day.spoken);
    } catch {
      setSummary("The summary could not be fetched.");
    }
  }

  const current = active && total > 0 ? tape.current[Math.min(position, total) - 1] : undefined;
  const line = current ? describe(current) : null;

  return (
    <section className="card activity" aria-labelledby="recorder-title">
      <div className="card-head">
        <h2 id="recorder-title">Activity</h2>
        <div className="head-actions">
          {active && (
            <button type="button" className="chip" onClick={backToLive}>
              Back to live
            </button>
          )}
          {playing ? (
            <button type="button" className="chip" onClick={() => setPlaying(false)}>
              Pause
            </button>
          ) : (
            <button type="button" className="chip" disabled={total === 0} onClick={play}>
              {active && position > 0 && position < total ? "Resume" : "Replay today"}
            </button>
          )}
          <button
            type="button"
            className="chip"
            title="Hear today's summary"
            onClick={() => void hearSummary()}
          >
            Hear summary
          </button>
        </div>
      </div>
      <p className={active ? "muted recorder-status" : "visually-hidden"} role="status">
        {total === 0
          ? "Nothing recorded yet."
          : active
            ? `Event ${Math.min(position, total)} of ${total}`
            : `${total} events recorded. The ring is showing live activity.`}
      </p>
      {line && current && (
        <p className="recorder-event">
          <StatusMark tone={line.tone} label={line.label} />
          <span>
            {clock(current.ts)} · {line.title}
          </span>
        </p>
      )}
      {summary && <p className="spoken">{summary}</p>}
      {children}
      <details className="fold" open={active}>
        <summary>Scrub and speed</summary>
        <div className="scrub-row">
          <label className="scrub">
            <span className="visually-hidden">Position in the recording</span>
            <input
              type="range"
              min={0}
              max={total}
              step={1}
              value={Math.min(position, total)}
              disabled={total === 0}
              onChange={(event) => scrub(Number(event.target.value))}
            />
          </label>
          <label className="inline-field">
            Speed
            <select value={speed} onChange={(event) => setSpeed(Number(event.target.value))}>
              {SPEEDS.map((value) => (
                <option key={value} value={value}>
                  {value}x
                </option>
              ))}
            </select>
          </label>
        </div>
      </details>
    </section>
  );
}
