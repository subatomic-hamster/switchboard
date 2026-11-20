import { type FormEvent, useState } from "react";

import { ApiError, type EscrowCreated, api } from "../api";
import { money } from "../format";

const DAY = 24 * 3600;

function fail(caught: unknown): string {
  if (caught instanceof ApiError) {
    if (caught.status === 503) return "Send by link is not set up on this server.";
    return caught.message;
  }
  return "Something went wrong.";
}

/** Send to someone with no wallet: a link they claim. The secret lives in this component's
 * state only (never storage, never a log) and is gone when the owner clicks Done. */
export function SendByLink() {
  const [open, setOpen] = useState(false);
  const [amount, setAmount] = useState("");
  const [note, setNote] = useState("");
  const [days, setDays] = useState("7");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [created, setCreated] = useState<EscrowCreated | null>(null);
  const [sent, setSent] = useState(0);
  const [copied, setCopied] = useState(false);
  const [refunded, setRefunded] = useState("");

  const value = Number(amount);
  const valid = Number.isFinite(value) && value > 0;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!valid) return;
    setBusy(true);
    setError("");
    try {
      setCreated(await api.createEscrow(value, Number(days) * DAY));
      setSent(value);
    } catch (caught) {
      setError(fail(caught));
    } finally {
      setBusy(false);
    }
  }

  async function copy() {
    if (!created) return;
    try {
      await navigator.clipboard.writeText(created.claim_link);
      setCopied(true);
    } catch {
      setError("Could not copy. Select the link and copy it by hand.");
    }
  }

  async function refund() {
    if (!created) return;
    setBusy(true);
    setError("");
    try {
      await api.refundEscrow(created.escrow_id);
      setRefunded("Refunded to your wallet.");
    } catch (caught) {
      const message = fail(caught);
      setError(message.includes("AlreadySettled") ? "Already claimed or refunded." : message);
    } finally {
      setBusy(false);
    }
  }

  function done() {
    setCreated(null);
    setAmount("");
    setNote("");
    setCopied(false);
    setRefunded("");
    setError("");
    setOpen(false);
  }

  if (!open) {
    return (
      <button type="button" className="chip" onClick={() => setOpen(true)}>
        No wallet? Send a link
      </button>
    );
  }

  const expiry = created ? new Date(created.expiry) : null;
  const expired = !!expiry && expiry.getTime() <= Date.now();

  return (
    <section className="link-box" aria-label="Send by link">
      {!created ? (
        <form onSubmit={submit} className="field-row">
          <label className="field-label">
            Amount (USD)
            <input
              type="number"
              inputMode="decimal"
              min="0"
              step="0.01"
              value={amount}
              onChange={(event) => setAmount(event.target.value)}
            />
          </label>
          <label className="field-label">
            Note (optional)
            <input
              type="text"
              value={note}
              maxLength={80}
              onChange={(event) => setNote(event.target.value)}
              placeholder="For you to remember"
            />
          </label>
          <label className="field-label">
            Expires in
            <select value={days} onChange={(event) => setDays(event.target.value)}>
              <option value="1">1 day</option>
              <option value="7">7 days</option>
            </select>
          </label>
          <button type="button" className="btn" onClick={done}>
            Cancel
          </button>
          <button type="submit" className="btn btn-primary" disabled={busy || !valid}>
            {busy ? "Creating…" : "Create link"}
          </button>
        </form>
      ) : (
        <div className="link-done">
          <p>
            <strong>{money(sent)}</strong>
            {note.trim() && <span className="muted"> · {note.trim()}</span>}
          </p>
          <div className="field-row">
            <label className="field-label">
              Claim link
              <input type="text" readOnly value={created.claim_link} onFocus={(e) => e.target.select()} />
            </label>
            <button type="button" className="btn btn-primary" onClick={() => void copy()}>
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
          <p className="muted">
            Anyone with this link can claim it until {expiry?.toLocaleString()}. Unclaimed money comes
            back to you.
          </p>
          {refunded && (
            <p className="note note-good" role="status">
              {refunded}
            </p>
          )}
          <div className="actions actions-start">
            <button
              type="button"
              className="btn"
              disabled={busy || !expired || !!refunded}
              title={expired ? undefined : "Available once the link has expired"}
              onClick={() => void refund()}
            >
              Cancel and refund
            </button>
            <button type="button" className="btn" onClick={done}>
              Done
            </button>
          </div>
        </div>
      )}
      {error && (
        <p className="note note-critical" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
