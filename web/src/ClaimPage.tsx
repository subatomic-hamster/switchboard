import { type FormEvent, useEffect, useState } from "react";

import { ApiError, type EscrowPeek, api } from "./api";
import { money } from "./format";

const ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const EXPLORER = "https://testnet.arcscan.app/tx/";

/** `#<id>:<secret>` from the fragment. The fragment never leaves the browser; nothing here
 * reads the query string. */
export function parseClaim(hash: string): { id: string; secret: string } | null {
  const match = /^#?([0-9]{1,18}):(0x)?([0-9a-fA-F]{64})$/.exec(hash);
  return match?.[1] && match[3] ? { id: match[1], secret: `0x${match[3]}` } : null;
}

/** The contract's refusal names (they arrive inside the server's message) as plain words. */
function explain(caught: unknown): string {
  if (!(caught instanceof ApiError)) return "Something went wrong. Try again.";
  const text = caught.message;
  if (text.includes("AlreadySettled")) return "This link has already been claimed or refunded.";
  if (text.includes("Expired")) return "This link has expired. The sender has been refunded.";
  if (text.includes("WrongSecret") || text.includes("UnknownDeposit") || caught.status === 422)
    return "This link is not valid. Ask the sender for a new one.";
  if (caught.status === 503) return "Claiming is not available right now.";
  return text;
}

export function ClaimPage() {
  // Read once; the fragment stays in the address bar, never in a request URL.
  const [link] = useState(() => parseClaim(window.location.hash));
  const [peek, setPeek] = useState<EscrowPeek | null>(null);
  const [address, setAddress] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(link ? "" : "This link is not valid. Ask the sender for a new one.");
  const [tx, setTx] = useState("");

  useEffect(() => {
    if (!link) return;
    api
      .peekEscrow(link.id, link.secret)
      .then(setPeek)
      .catch((caught: unknown) => setError(explain(caught)));
  }, [link]);

  const valid = ADDRESS.test(address.trim());
  const closed = !!peek && (peek.settled || new Date(peek.expiry).getTime() <= Date.now());

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!link || !valid) return;
    setBusy(true);
    setError("");
    try {
      setTx((await api.claimEscrow(link.id, link.secret, address.trim())).tx_hash);
    } catch (caught) {
      setError(explain(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="claim-page">
      <section className="card claim-card">
        <h1>You have money waiting</h1>
        {peek && !tx && <p className="quote-amount">{money(peek.amount_usd)}</p>}
        {peek && !peek.settled && !tx && (
          <p className="muted">Claim it by {new Date(peek.expiry).toLocaleString()}.</p>
        )}
        {tx ? (
          <p className="note note-good" role="status">
            <span>
              Sent{peek ? ` ${money(peek.amount_usd)}` : ""} to your wallet.{" "}
              <a href={`${EXPLORER}${tx}`} target="_blank" rel="noreferrer">
                View the transaction
              </a>
            </span>
          </p>
        ) : (
          link && (
            <form onSubmit={submit} className="claim-form">
              <label className="field-label">
                Your Arc wallet address
                <input
                  type="text"
                  value={address}
                  onChange={(event) => setAddress(event.target.value)}
                  placeholder="0x…"
                  autoComplete="off"
                  spellCheck={false}
                  aria-invalid={address.trim() !== "" && !valid}
                />
              </label>
              {address.trim() !== "" && !valid && (
                <p className="muted">That is not an address. It starts with 0x and has 40 more characters.</p>
              )}
              <button type="submit" className="btn btn-primary btn-big" disabled={busy || !valid || closed}>
                {busy ? "Claiming…" : "Claim"}
              </button>
              {busy && <p className="muted">This takes about a few seconds. Keep this page open.</p>}
            </form>
          )
        )}
        {error && (
          <p className="note note-critical" role="alert">
            {error}
          </p>
        )}
      </section>
    </main>
  );
}
