import { type FormEvent, useState } from "react";

import { ApiError, api } from "../api";
import { CHAIN_NAMES, duration, money, REASON_TEXT, type Tone } from "../format";
import type { ExecuteResponse, IntentResponse, Route } from "../types";
import { StatusMark } from "./StatusMark";

const EXAMPLES = [
  "Move two hundred dollars from my Ethereum wallet to Sam's Solana wallet",
  "Send 250 from my Arc wallet to Sam's Arc wallet",
  "Send 20 from my Arc wallet to 0x9c1e8a4f2b7d6c5a3e1f0b9d8c7a6e5f4d3c4f2c",
  "Move my Rockets position from Polymarket to Gemini",
];

const OUTCOME: Record<string, { tone: Tone; label: string }> = {
  pay: { tone: "good", label: "Will pay" },
  ask: { tone: "warning", label: "Will ask you" },
  block: { tone: "critical", label: "Will block" },
};

const RESULT: Record<ExecuteResponse["status"], { tone: Tone; label: string }> = {
  executing: { tone: "good", label: "Sending" },
  needs_approval: { tone: "warning", label: "Needs approval" },
  blocked: { tone: "critical", label: "Blocked" },
};

export interface CommandBarProps {
  /** Called with the spoken quote whenever a new plan is quoted. */
  onQuote?: (plan: IntentResponse) => void;
  /** Called when a market-move sentence resolves to a market. */
  onMarketMove?: (slug: string) => void;
  /** Lets a parent (voice input) supply the sentence. */
  text: string;
  onTextChange: (text: string) => void;
  micSlot?: React.ReactNode;
}

function RouteCard({
  route,
  same,
  busy,
  onRun,
}: {
  route: Route;
  same: boolean;
  busy: boolean;
  onRun: () => void;
}) {
  const title = same ? "Cheapest and fastest" : route.id === "cheapest" ? "Cheapest" : "Fastest";
  return (
    <div className="route">
      <div className="route-head">
        <strong>{title}</strong>
        <span className="route-cost">
          {money(route.cost_usd)} · about {duration(route.eta_s)}
        </span>
      </div>
      <ol className="route-steps">
        {route.steps.map((step, index) => (
          <li key={index}>
            {step.from_chain === step.to_chain
              ? `${step.description}`
              : `${CHAIN_NAMES[step.from_chain]} to ${CHAIN_NAMES[step.to_chain]}`}
            <span className="muted"> · {step.rail}</span>
          </li>
        ))}
      </ol>
      <button type="button" className="primary" disabled={busy} onClick={onRun}>
        Send by this route
      </button>
    </div>
  );
}

export function CommandBar({ onQuote, onMarketMove, text, onTextChange, micSlot }: CommandBarProps) {
  const [plan, setPlan] = useState<IntentResponse | null>(null);
  const [result, setResult] = useState<ExecuteResponse | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function quote(sentence: string) {
    if (!sentence.trim()) return;
    setBusy(true);
    setError("");
    setResult(null);
    setPlan(null);
    try {
      const response = await api.intent(sentence.trim());
      setPlan(response);
      onQuote?.(response);
      if (response.kind === "market_move" && response.market_move?.slug) {
        onMarketMove?.(response.market_move.slug);
      }
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }

  async function run(routeId: Route["id"]) {
    if (!plan) return;
    setBusy(true);
    setError("");
    try {
      setResult(await api.execute(plan.plan_id, routeId));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void quote(text);
  }

  const routes = plan?.routes ?? [];
  const [first, second] = routes;
  const sameRoute =
    !!first && !!second && JSON.stringify(first.steps) === JSON.stringify(second.steps);
  const outcome = plan?.preview ? OUTCOME[plan.preview.outcome] : undefined;

  return (
    <section className="panel command" aria-labelledby="command-title">
      <h2 id="command-title">Tell it where the money should go</h2>
      <form onSubmit={onSubmit} className="command-form">
        <label htmlFor="sentence" className="visually-hidden">
          Your request, in a sentence
        </label>
        <input
          id="sentence"
          type="text"
          value={text}
          onChange={(event) => onTextChange(event.target.value)}
          placeholder="Move two hundred dollars from my Ethereum wallet to Sam's Solana wallet"
          autoComplete="off"
          maxLength={500}
        />
        {micSlot}
        <button type="submit" className="primary" disabled={busy || !text.trim()}>
          {busy && !plan ? "Working…" : "Get a quote"}
        </button>
      </form>
      <div className="examples">
        <span className="muted">Try:</span>
        {EXAMPLES.map((example) => (
          <button
            key={example}
            type="button"
            className="chip"
            onClick={() => {
              onTextChange(example);
              void quote(example);
            }}
          >
            {example}
          </button>
        ))}
      </div>

      {error && (
        <p className="notice notice-critical" role="alert">
          {error}
        </p>
      )}

      {plan && plan.kind === "transfer" && plan.request && (
        <div className="quote">
          <div className="quote-head">
            <p className="quote-summary">
              <strong>{money(plan.request.amount_usd)}</strong> from {plan.request.source.label} to{" "}
              {plan.request.destination.label}
              {plan.request.to_asset === "EURC" ? ", converted to euros" : ""}
            </p>
            {outcome && <StatusMark tone={outcome.tone} label={outcome.label} />}
          </div>
          <p className="spoken">{plan.spoken_quote}</p>
          {!result && (
            <div className="routes">
              {first && (
                <RouteCard
                  route={first}
                  same={sameRoute}
                  busy={busy}
                  onRun={() => void run(first.id)}
                />
              )}
              {second && !sameRoute && (
                <RouteCard
                  route={second}
                  same={false}
                  busy={busy}
                  onRun={() => void run(second.id)}
                />
              )}
            </div>
          )}
          {result && (
            <div className={`notice notice-${RESULT[result.status].tone}`} role="status">
              <StatusMark tone={RESULT[result.status].tone} label={RESULT[result.status].label} />
              <span>
                {result.detail}
                {result.reason && (
                  <>
                    {" "}
                    <code className="reason-code">{result.reason}</code>
                    {REASON_TEXT[result.reason]}
                  </>
                )}
              </span>
            </div>
          )}
        </div>
      )}

      {plan && plan.kind === "market_move" && plan.market_move && (
        <div className="quote">
          <p className="quote-summary">
            Move a position from <strong>{plan.market_move.from_venue}</strong> to{" "}
            <strong>{plan.market_move.to_venue}</strong>
            {plan.market_move.slug ? `: ${plan.market_move.slug}` : ""}
          </p>
          <p className="spoken">
            {plan.market_move.slug
              ? "The markets panel below shows whether the two contracts are the same bet, and what the move would cost."
              : "I could not find that market in the listing. Pick one in the markets panel below."}
          </p>
        </div>
      )}
    </section>
  );
}
