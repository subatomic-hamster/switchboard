import { useState } from "react";

import { ApiError, api } from "../api";
import { passkeysSupported, registerPasskey } from "../passkey";
import type { StatsResponse } from "../types";

const CALL_PROVIDER_NAME: Record<string, string> = {
  agentphone: "AgentPhone",
  elevenlabs_twilio: "ElevenLabs and Twilio",
};

type Level = "good" | "bad" | "off";

function Line({
  level,
  label,
  children,
}: {
  level: Level;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <li className="line">
      <span className={`dot dot-${level}`} aria-hidden="true" />
      <span className="grow">{label}</span>
      {children}
    </li>
  );
}

export interface HealthPanelProps {
  stats: StatsResponse | null;
  onChanged: () => void;
}

/** Is the vault open, is the event stream delivering, and is a passkey set up. */
export function HealthPanel({ stats, onChanged }: HealthPanelProps) {
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  async function act(action: () => Promise<unknown>, done = "") {
    setBusy(true);
    setMessage("");
    try {
      await action();
      setMessage(done);
    } catch (caught) {
      setMessage(caught instanceof ApiError ? caught.message : "Something went wrong.");
    } finally {
      setBusy(false);
      onChanged();
    }
  }

  if (!stats) return null;
  const { approvals } = stats;
  return (
    <section className="card" aria-labelledby="health-title">
      <h2 id="health-title">Health</h2>
      <ul className="lines">
        <Line level={stats.frozen ? "bad" : "good"} label="Vault">
          <span className="muted">{stats.frozen ? "Frozen" : "Open"}</span>
        </Line>
        <Line level={stats.stream_stale ? "bad" : "good"} label="Event stream">
          <span className="muted" title="If it goes quiet, spending pauses until it recovers.">
            {stats.stream_stale ? "Stale" : "Delivering"}
          </span>
          {stats.using_p1_stubs && (
            <button
              type="button"
              className="btn"
              disabled={busy}
              onClick={() => void act(() => api.setStreamStale(!stats.stream_stale))}
            >
              {stats.stream_stale ? "Restore the stream" : "Kill the stream"}
            </button>
          )}
        </Line>
        <Line level={stats.passkey_registered ? "good" : "off"} label="Passkey">
          {stats.passkey_registered ? (
            <span className="muted">Ready</span>
          ) : passkeysSupported() ? (
            <button
              type="button"
              className="btn"
              disabled={busy}
              title={`Needed, with the call, for anything over $${stats.passkey_threshold_usd}`}
              onClick={() => void act(registerPasskey, "Passkey registered.")}
            >
              Set up
            </button>
          ) : (
            <span className="muted">Not available here</span>
          )}
        </Line>
      </ul>
      {message && (
        <p role="status" className="muted">
          {message}
        </p>
      )}
      <details className="fold">
        <summary>More status</summary>
        <ul className="lines">
          <Line level="off" label="Approvals">
            <span className="muted">
              {approvals.total === 0
                ? "None decided yet"
                : `${approvals.approved} approved, ${approvals.denied} denied`}
            </span>
          </Line>
          <Line level={stats.calls_live ? "good" : "off"} label="Phone calls">
            <span className="muted">
              {stats.calls_live
                ? `Live through ${CALL_PROVIDER_NAME[stats.call_provider] ?? stats.call_provider}`
                : "Practice: none placed"}
            </span>
          </Line>
          <Line level={stats.using_p1_stubs ? "off" : "good"} label="Chain and venues">
            <span className="muted">{stats.using_p1_stubs ? "Practice data" : "Live"}</span>
          </Line>
          <Line level={stats.trading_enabled ? "good" : "off"} label="Trading">
            <span className="muted">{stats.trading_enabled ? "On" : "Off"}</span>
          </Line>
        </ul>
      </details>
    </section>
  );
}
