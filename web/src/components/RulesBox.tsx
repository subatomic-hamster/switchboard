import { type FormEvent, useCallback, useEffect, useState } from "react";

import { ApiError, api } from "../api";
import { canSpeak, speak } from "../speech";
import type { RuleView } from "../types";
import { MicButton } from "./MicButton";

const ERRORS: Record<string, string> = {
  challenge_mismatch: "That is not the word on this screen.",
  too_many_attempts: "Three wrong attempts. The rule was dropped.",
  expired: "The proposal expired. Say the rule again.",
  already_resolved: "That proposal has already been settled.",
};

export interface RulesBoxProps {
  callsLive: boolean;
  refreshKey: number;
  onChanged: () => void;
}

/** Speak or type a rule, hear it read back, confirm, and the policy changes. */
export function RulesBox({ callsLive, refreshKey, onChanged }: RulesBoxProps) {
  const [text, setText] = useState("");
  const [rules, setRules] = useState<RuleView[]>([]);
  const [word, setWord] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api.rules().then(setRules).catch(() => undefined);
  }, []);
  useEffect(load, [load, refreshKey]);

  const pending = rules.find((rule) => rule.status === "pending");

  async function propose(sentence: string) {
    if (!sentence.trim()) return;
    setBusy(true);
    setMessage("");
    try {
      const proposal = await api.proposeRule(sentence.trim());
      if (canSpeak()) speak(proposal.readback);
      setText("");
      setWord("");
    } catch (caught) {
      const reason = caught instanceof ApiError ? caught.message : "Something went wrong.";
      setMessage(reason);
      if (canSpeak()) speak(reason);
    } finally {
      setBusy(false);
      load();
    }
  }

  async function settle(action: "confirm" | "cancel") {
    if (!pending) return;
    setBusy(true);
    setMessage("");
    try {
      const result =
        action === "confirm"
          ? await api.confirmRule(pending.proposal_id, word)
          : await api.cancelRule(pending.proposal_id);
      if (!result.ok) setMessage(ERRORS[result.error] ?? result.error);
      else setMessage(action === "confirm" ? "Applied. The policy has changed." : "Cancelled.");
    } catch (caught) {
      setMessage(caught instanceof ApiError ? caught.message : "Something went wrong.");
    } finally {
      setBusy(false);
      load();
      onChanged();
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void propose(text);
  }

  return (
    <div className="subpanel">
      <h3>Change a rule by voice</h3>
      <p className="muted">
        Try “raise my transfer limit to 300”, “lower my daily budget to 300” or “stop paying
        Sam's Arc wallet”.
      </p>
      <form className="command-form" onSubmit={onSubmit}>
        <label htmlFor="rule" className="visually-hidden">
          A rule, in a sentence
        </label>
        <input
          id="rule"
          type="text"
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder="Say or type a rule"
          autoComplete="off"
          maxLength={300}
        />
        <MicButton onText={setText} onFinal={(sentence) => void propose(sentence)} />
        <button type="submit" disabled={busy || !text.trim()}>
          Read it back
        </button>
      </form>

      {pending && (
        <div className="notice notice-warning rule-pending">
          <div>
            <p>{pending.readback}</p>
            {pending.challenge && (
              <p>
                Your word: <strong className="word-inline">{pending.challenge}</strong>
              </p>
            )}
            {callsLive ? (
              <p className="muted">Confirm on the call.</p>
            ) : (
              <div className="row">
                {pending.needs_word && (
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
                )}
                <button
                  type="button"
                  className="primary"
                  disabled={busy || (pending.needs_word && !word)}
                  onClick={() => void settle("confirm")}
                >
                  Confirm
                </button>
                <button type="button" disabled={busy} onClick={() => void settle("cancel")}>
                  Cancel
                </button>
              </div>
            )}
          </div>
        </div>
      )}
      {message && (
        <p role="status" className="muted">
          {message}
        </p>
      )}
    </div>
  );
}
