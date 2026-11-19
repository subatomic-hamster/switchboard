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

  return (
    <div className="approval">
      <div className="approval-head">
        <p className="approval-amount">
          <strong>{money(approval.amount_usd)}</strong> to {approval.payee_label}
        </p>
        <StatusMark tone="warning" label={`Decide within ${seconds} s`} />
      </div>
      <p className="feed-detail">
        <code className="reason-code">{approval.reason}</code>
        {REASON_TEXT[approval.reason]}
        {approval.source === "hostile" ? " · triggered by outside content, not by you" : ""}
      </p>

      <div className="word-box">
        <span className="word-caption">Your word for this request</span>
        <span className="word" data-testid="challenge-word">
          {approval.challenge}
        </span>
        <span className="word-caption">
          To approve, say the amount and this word on the call. “Yes” is not enough.
        </span>
      </div>

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

      {approval.requires_passkey && (
        <div className="notice notice-warning">
          <span>
            Large amount: a passkey is also required.{" "}
            {approval.passkey_ok
              ? "Passkey confirmed."
              : passkeyRegistered
                ? ""
                : "No passkey is registered yet; register one in Safety controls."}
          </span>
          {!approval.passkey_ok && passkeyRegistered && passkeysSupported() && (
            <button
              type="button"
              disabled={busy}
              onClick={() => void act(() => confirmWithPasskey(approval.request_id))}
            >
              Confirm with passkey
            </button>
          )}
        </div>
      )}

      {callsLive ? (
        <p className="muted">
          Your phone is ringing. Approve on the call. {approval.voice_ok ? "Voice part done." : ""}
        </p>
      ) : (
        <form className="simulate" onSubmit={simulate}>
          <p className="muted">
            Dry run: no call was placed. Stand in for the call here; the same checks apply.
          </p>
          <div className="row">
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
            <button type="submit" className="primary" disabled={busy || !amount || !word}>
              Approve
            </button>
          </div>
        </form>
      )}

      <div className="row approval-actions">
        <button
          type="button"
          className="danger"
          disabled={busy}
          onClick={() => void act(() => api.denyApproval(approval.request_id))}
        >
          Deny
        </button>
        {message && (
          <span role="status" className="approval-message">
            {message}
          </span>
        )}
      </div>
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
    <section className="panel approval-panel" aria-labelledby="approval-title">
      <h2 id="approval-title">Needs your say</h2>
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
