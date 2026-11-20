import { useState } from "react";

import { ApiError, api } from "../api";
import { passkeysSupported, registerPasskey } from "../passkey";
import type { StatsResponse } from "../types";
import { StatusMark } from "./StatusMark";

export interface SafetyPanelProps {
  stats: StatsResponse | null;
  onChanged: () => void;
  children?: React.ReactNode;
}

/** The switches: freeze, the stream (stubbed), and the passkey. */
export function SafetyPanel({ stats, onChanged, children }: SafetyPanelProps) {
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
  return (
    <section className="panel" aria-labelledby="safety-title">
      <h2 id="safety-title">Safety controls</h2>
      <ul className="switches">
        <li>
          <div>
            <strong>Kill switch</strong>
            <p className="muted">
              Freezes the vault. Every spend is refused until you unfreeze. You can also say
              “freeze” on any approval call.
            </p>
          </div>
          <div className="switch-side">
            <StatusMark
              tone={stats.frozen ? "critical" : "good"}
              label={stats.frozen ? "Frozen" : "Live"}
            />
            <button
              type="button"
              className={stats.frozen ? "" : "danger"}
              disabled={busy}
              onClick={() => void act(() => api.freeze(!stats.frozen))}
            >
              {stats.frozen ? "Unfreeze" : "Freeze everything"}
            </button>
          </div>
        </li>
        <li>
          <div>
            <strong>Event stream</strong>
            <p className="muted">
              If the stream that reports vault events goes quiet, spending pauses until it
              recovers.
              {stats.using_p1_stubs ? " This switch simulates that against the stub." : ""}
            </p>
          </div>
          <div className="switch-side">
            <StatusMark
              tone={stats.stream_stale ? "critical" : "good"}
              label={stats.stream_stale ? "Stale" : "Delivering"}
            />
            {stats.using_p1_stubs && (
              <button
                type="button"
                disabled={busy}
                onClick={() => void act(() => api.setStreamStale(!stats.stream_stale))}
              >
                {stats.stream_stale ? "Restore the stream" : "Kill the stream"}
              </button>
            )}
          </div>
        </li>
        <li>
          <div>
            <strong>Passkey</strong>
            <p className="muted">
              Needed, with the call, for anything over ${stats.passkey_threshold_usd}.
            </p>
          </div>
          <div className="switch-side">
            <StatusMark
              tone={stats.passkey_registered ? "good" : "neutral"}
              label={stats.passkey_registered ? "Registered" : "Not registered"}
            />
            {!stats.passkey_registered && passkeysSupported() && (
              <button
                type="button"
                disabled={busy}
                onClick={() => void act(registerPasskey, "Passkey registered.")}
              >
                Register a passkey
              </button>
            )}
          </div>
        </li>
      </ul>
      {message && (
        <p role="status" className="muted">
          {message}
        </p>
      )}
      {children}
    </section>
  );
}
