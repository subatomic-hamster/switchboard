import { useState } from "react";

import { ApiError, api } from "../api";
import type { StatsResponse } from "../types";

function LockIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      <rect x="5" y="11" width="14" height="10" rx="2" />
      <path d="M8 11V7a4 4 0 0 1 8 0v4" />
    </svg>
  );
}

/** The kill switch, on every tab. Freezes every spend until it is pressed again. */
export function FreezeButton({
  stats,
  onChanged,
}: {
  stats: StatsResponse | null;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (!stats) return null;

  async function toggle() {
    if (!stats) return;
    setBusy(true);
    setError("");
    try {
      await api.freeze(!stats.frozen);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not reach the server.");
    } finally {
      setBusy(false);
      onChanged();
    }
  }

  return (
    <>
      {error && (
        <span role="alert" className="inline-error">
          {error}
        </span>
      )}
      <button
        type="button"
        className={`btn ${stats.frozen ? "btn-primary" : "btn-danger"}`}
        disabled={busy}
        title={
          stats.frozen
            ? "Let the vault spend again"
            : "Refuse every spend until you unfreeze. You can also say “freeze” on any approval call."
        }
        onClick={() => void toggle()}
      >
        <LockIcon />
        {stats.frozen ? "Unfreeze" : "Freeze"}
      </button>
    </>
  );
}
