import { type FormEvent, useEffect, useState } from "react";

import { ApiError, api } from "../api";
import { money, REASON_TEXT } from "../format";
import { confirmWithPasskey, passkeysSupported } from "../passkey";
import type { ApprovalView, OkResponse } from "../types";
import { StatusMark } from "./StatusMark";

const ERRORS: Record<string, string> = {
  amount_mismatch: "That is not the amount. Say the exact amount of this request.",
  challenge_mismatch: "That is not the word on this screen.",
  too_many_attempts: "Three wrong attempts. The request has been denied.",
  expired: "The request expired, so it was denied.",
  already_resolved: "This request has already been decided.",
};

function useCountdown(expiresAt: string): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  return Math.max(0, Math.round((new Date(expiresAt).getTime() - now) / 1000));
}

function sourceNote(source: string): string {
  if (source === "hostile") return " · from outside content, not you";
  if (source.startsWith("connector:")) return ` · asked by ${source.slice("connector:".length)}, not you`;
  return "";
}

function PendingApproval({
  approval,
  callsLive,
  passkeyRegistered,
  onChanged,
}: {
  approval: ApprovalView;
  callsLive: boolean;
  passkeyRegistered: boolean;
  onChanged: () => void;
}) {
  const seconds = useCountdown(approval.expires_at);
  const [amount, setAmount] = useState("");
  const [word, setWord] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  async function act(action: () => Promise<OkResponse>) {
    setBusy(true);
    setMessage("");
    try {
      const result = await action();
      if (!result.ok) setMessage(ERRORS[result.error] ?? result.error);
      else if (result.status === "awaiting_passkey") {
        setMessage("Voice part done. Now confirm with your passkey.");
      } else if (result.status === "awaiting_voice") {
        setMessage("Passkey confirmed. The amount and the word are still needed on the call.");
      }
    } catch (caught) {
      setMessage(caught instanceof ApiError ? caught.message : "Something went wrong.");
    } finally {
      setBusy(false);
      onChanged();
    }
  }

  function simulate(event: FormEvent) {
    event.preventDefault();
    void act(() => api.simulateVoice(approval.request_id, amount, word));
  }

  const deny = (
    <button
      type="button"
      className="btn"
      disabled={busy}
      onClick={() => void act(() => api.denyApproval(approval.request_id))}
    >
      Deny
    </button>
  );
  const reason = REASON_TEXT[approval.reason];

  return (
    <div className="approval">
      <div className="approval-main">
        <span className="approval-icon" aria-hidden="true">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2" />
          </svg>
        </span>
        <div className="approval-text">
          <span className="approval-title">
            <strong>{money(approval.amount_usd)}</strong> to {approval.payee_label}
          </span>
          <span className="approval-reason">
            <code className="reason-code">{approval.reason}</code>
            {reason.charAt(0).toUpperCase() + reason.slice(1)}
            {sourceNote(approval.source)}
          </span>
        </div>
        <StatusMark tone="warning" label={`${seconds} s left`} />
      </div>

      <p className="word-line">
        Your word <strong className="word" data-testid="challenge-word">{approval.challenge}</strong>
        <span className="muted">Say the amount and this word on the call. “Yes” is not enough.</span>
      </p>

      {approval.requires_passkey && (
        <div className="note note-warning">
          <span>
            Passkey also required.{" "}
            {approval.passkey_ok
              ? "Confirmed."
              : passkeyRegistered
                ? ""
                : "None is set up yet: set one up in Controls."}
          </span>
          {!approval.passkey_ok && passkeyRegistered && passkeysSupported() && (
            <button
              type="button"
              className="btn"
              disabled={busy}
              onClick={() => void act(() => confirmWithPasskey(approval.request_id))}
            >
              Confirm with passkey
            </button>
          )}
        </div>
      )}

      <details className="fold">
        <summary>Why?</summary>
        <div className="cases">
          <div>
            <h3>Operator: the case for</h3>
            <p>{approval.case_for}</p>
          </div>
          <div>
            <h3>Risk officer: the case against</h3>
            <p>{approval.case_against}</p>
          </div>
        </div>
      </details>

      {callsLive ? (
        <div className="actions">
          <p className="muted grow">
            Your phone is ringing. Approve on the call. {approval.voice_ok ? "Voice part done." : ""}
          </p>
          {deny}
        </div>
      ) : (
        <form className="simulate" onSubmit={simulate}>
          <p className="muted">Practice mode: type what you would say on the call.</p>
          <div className="simulate-row">
            <label>
              <span className="visually-hidden">Amount you would say</span>
              <input
                type="text"
                inputMode="decimal"
                placeholder="Amount you would say"
                value={amount}
                onChange={(event) => setAmount(event.target.value)}
              />
            </label>
            <label>
              <span className="visually-hidden">Word you would say</span>
              <input
                type="text"
                placeholder="Word you would say"
                value={word}
                onChange={(event) => setWord(event.target.value)}
                autoComplete="off"
              />
            </label>
            <div className="actions">
              {deny}
              <button type="submit" className="btn btn-warn" disabled={busy || !amount || !word}>
                Approve
              </button>
            </div>
          </div>
        </form>
      )}
      {message && (
        <p role="status" className="approval-message">
          {message}
        </p>
      )}
    </div>
  );
}

export interface ApprovalPanelProps {
  approvals: ApprovalView[];
  callsLive: boolean;
  passkeyRegistered: boolean;
  onChanged: () => void;
}

/** What needs the owner's say right now. Hidden when nothing is waiting. */
export function ApprovalPanel({
  approvals,
  callsLive,
  passkeyRegistered,
  onChanged,
}: ApprovalPanelProps) {
  const pending = approvals.filter((approval) => approval.status === "pending");
  if (pending.length === 0) return null;
  return (
    <section className="card approval-panel" aria-label="Needs you">
      {pending.map((approval) => (
        <PendingApproval
          key={approval.request_id}
          approval={approval}
          callsLive={callsLive}
          passkeyRegistered={passkeyRegistered}
          onChanged={onChanged}
        />
      ))}
    </section>
  );
}
