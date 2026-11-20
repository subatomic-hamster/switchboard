import { useState } from "react";

import { ApiError, api } from "../api";

const ACTIONS = [
  { id: "offer", label: "Show Gemini offer", done: "Gemini offer is on." },
  { id: "settle", label: "Settle Rockets as a win", done: "Rockets settled as a win." },
  { id: "reset", label: "Reset demo", done: "Demo reset." },
] as const;

/** Controls for the scripted demo. Rendered only in demo mode. */
export function DemoCard() {
  const [message, setMessage] = useState("");
  const [bad, setBad] = useState(false);
  const [busy, setBusy] = useState(false);

  async function run(action: (typeof ACTIONS)[number]) {
    setBusy(true);
    try {
      await api.demoAction(action.id);
      setBad(false);
      setMessage(action.done);
    } catch (caught) {
      setBad(true);
      setMessage(caught instanceof ApiError ? caught.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card" aria-labelledby="demo-title">
      <h2 id="demo-title">Demo</h2>
      <div className="actions actions-start">
        {ACTIONS.map((action) => (
          <button key={action.id} type="button" className="btn" disabled={busy} onClick={() => void run(action)}>
            {action.label}
          </button>
        ))}
      </div>
      <p className={bad ? "inline-error" : "muted"} role="status">
        {message}
      </p>
    </section>
  );
}
