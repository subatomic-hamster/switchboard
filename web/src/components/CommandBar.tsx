import { type FormEvent, useEffect, useState } from "react";

import { ApiError, api } from "../api";
import type { Plan, Suggestion } from "../demo-types";
import { betName, cents, CHAIN_NAMES, duration, money, REASON_TEXT, type Tone } from "../format";
import { canSpeak, speak, stopSpeaking } from "../speech";
import type { Chain, ExecuteResponse, Route } from "../types";
import { Countdown } from "./Countdown";
import { SendByLink } from "./SendByLink";
import { StatusMark } from "./StatusMark";

// The chip shows the short label; the full sentence is what gets quoted.
const EXAMPLES = [
  // Within the limits and live on testnet: Arc to Solana through CCTP, no call needed.
  { label: "$2 Arc to Sam's Solana", text: "Send 2 dollars from my Arc wallet to Sam's Solana wallet" },
  { label: "$250 to Sam's Arc wallet", text: "Send 250 from my Arc wallet to Sam's Arc wallet" },
  {
    label: "Move my Rockets position",
    text: "Move my Rockets position from Polymarket to Gemini",
  },
];

const RESULT: Record<ExecuteResponse["status"], { tone: Tone; label: string }> = {
  executing: { tone: "good", label: "Sending" },
  needs_approval: { tone: "warning", label: "Needs approval" },
  blocked: { tone: "critical", label: "Blocked" },
};

export interface CommandBarProps {
  /** Called with the spoken quote whenever a new plan is quoted. */
  onQuote?: (plan: Plan) => void;
  /** Live suggestions from demo mode; null or absent keeps the static examples. */
  suggestions?: Suggestion[] | null;
  /** Called when a market-move sentence resolves to a market. */
  onMarketMove?: (slug: string) => void;
  /** Takes the owner to the Markets tab. */
  onOpenMarkets?: () => void;
  /** Lets a parent (voice input) supply the sentence. */
  text: string;
  onTextChange: (text: string) => void;
  micSlot?: React.ReactNode;
  /** A sentence to quote at once, for example one that was just spoken. */
  submit?: { text: string; at: number } | null;
}

/** Ethereum, Arc, Solana: the chains a route touches, in order. */
function stops(route: Route): string[] {
  const first = route.steps[0];
  if (!first) return [];
  const chains: Chain[] = [first.from_chain];
  for (const step of route.steps) {
    if (chains[chains.length - 1] !== step.to_chain) chains.push(step.to_chain);
  }
  return chains.map((chain) => CHAIN_NAMES[chain]);
}

function PhoneIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      <path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2" />
    </svg>
  );
}

export function CommandBar({
  onQuote,
  suggestions,
  onMarketMove,
  onOpenMarkets,
  text,
  onTextChange,
  micSlot,
  submit,
}: CommandBarProps) {
  const [plan, setPlan] = useState<Plan | null>(null);
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

  function cancel() {
    stopSpeaking();
    setPlan(null);
    setResult(null);
    setError("");
  }

  useEffect(() => {
    if (submit) void quote(submit.text);
    // Only a new spoken sentence should trigger this, not a re-render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [submit]);

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void quote(text);
  }

  const [first, second] = plan?.routes ?? [];
  const sameRoute =
    !!first && !!second && JSON.stringify(first.steps) === JSON.stringify(second.steps);
  const request = plan?.kind === "transfer" ? plan.request : null;
  const outcome = plan?.preview?.outcome;
  const route = (item: Route) =>
    sameRoute ? "Cheapest and fastest" : item.id === "cheapest" ? "Cheapest" : "Fastest";

  return (
    <section className="card command">
      <label htmlFor="sentence" className="command-title">
        Where should the money go?
      </label>
      <form onSubmit={onSubmit} className="command-form">
        <input
          id="sentence"
          type="text"
          value={text}
          onChange={(event) => onTextChange(event.target.value)}
          placeholder="Send $20 from my Arc wallet to Sam"
          autoComplete="off"
          maxLength={500}
        />
        {micSlot}
        <button type="submit" className="btn btn-primary btn-big" disabled={busy || !text.trim()}>
          {busy && !plan ? "Working…" : "Get quote"}
        </button>
      </form>
      <div className="chips">
        {suggestions
          ? suggestions.map((suggestion) => (
              <button
                key={suggestion.label}
                type="button"
                className="chip"
                title={suggestion.why}
                onClick={() => onTextChange(suggestion.text)}
              >
                {suggestion.label}
              </button>
            ))
          : EXAMPLES.map((example) => (
              <button
                key={example.label}
                type="button"
                className="chip"
                onClick={() => {
                  onTextChange(example.text);
                  void quote(example.text);
                }}
              >
                {example.label}
              </button>
            ))}
        <SendByLink />
      </div>

      {error && (
        <p className="note note-critical" role="alert">
          {error}
        </p>
      )}

      {/* Always present, so a screen reader announces the quote when it appears. */}
      <div aria-live="polite" className="quote-slot">
        {request && first && (
          <div className="quote">
            <div className="quote-top">
              <div className="quote-who">
                <span className="muted">You're sending</span>
                <span className="quote-amount">{money(request.amount_usd)}</span>
                <span>
                  to {request.destination.label}{" "}
                  <span className="muted">
                    {request.destination.address ? ` · ends ${request.destination.address.slice(-4)}` : ""}
                    {request.to_asset === "EURC" ? " · as euros" : ""}
                  </span>
                </span>
              </div>
              {canSpeak() && (
                <button type="button" className="btn" onClick={() => plan && speak(plan.spoken_quote)}>
                  Read aloud
                </button>
              )}
            </div>

            <ol className="route-line" aria-label="Route">
              {stops(first).map((name, index, all) => (
                <li key={`${name}-${index}`}>
                  {index > 0 && <span className="route-bar" aria-hidden="true" />}
                  <span className="route-stop">
                    <span className={`route-dot${index > 0 && index < all.length - 1 ? " route-dot-via" : ""}`} aria-hidden="true" />
                    {name}
                  </span>
                </li>
              ))}
            </ol>

            <dl className="facts">
              <div>
                <dt>Fee</dt>
                <dd>{money(first.cost_usd)}</dd>
              </div>
              <div>
                <dt>Arrives in</dt>
                <dd>about {duration(first.eta_s)}</dd>
              </div>
              <div>
                <dt>Route</dt>
                <dd>{route(first)}</dd>
              </div>
            </dl>

            {!result && (outcome === "ask" || outcome === "block") && plan?.preview && (
              <p className={`note note-${outcome === "ask" ? "warning" : "critical"}`}>
                {outcome === "ask" ? (
                  <PhoneIcon />
                ) : (
                  <StatusMark tone="critical" label="Will block" />
                )}
                <span>{plan.preview.detail}</span>
              </p>
            )}

            {result ? (
              <div className={`note note-${RESULT[result.status].tone}`} role="status">
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
            ) : (
              <div className="actions">
                <button type="button" className="btn" onClick={cancel}>
                  Cancel
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={busy}
                  onClick={() => void run(first.id)}
                >
                  Send {money(request.amount_usd)}
                </button>
              </div>
            )}

            {!result && second && !sameRoute && (
              <details className="fold">
                <summary>Other route</summary>
                <div className="other-route">
                  <span>
                    {route(second)} · {stops(second).join(" → ")}
                  </span>
                  <span className="muted">
                    {money(second.cost_usd)} · about {duration(second.eta_s)}
                  </span>
                  <button type="button" className="btn" disabled={busy} onClick={() => void run(second.id)}>
                    Send by this route
                  </button>
                </div>
              </details>
            )}
          </div>
        )}

        {plan?.kind === "market_buy" && plan.market_buy && (
          <div className="quote quote-buy">
            <div className="quote-who">
              <span>
                Buy <strong>{plan.market_buy.side}</strong> on{" "}
                <strong>{betName(plan.market_buy.title)}</strong>{" "}
                <span className="muted">({plan.market_buy.venue})</span>
              </span>
              <span className="quote-amount">{money(plan.market_buy.amount_usd)}</span>
              <span className="muted">from {plan.market_buy.from_place}</span>
            </div>
            <dl className="facts">
              <div>
                <dt>Price</dt>
                <dd>{cents(plan.market_buy.price)}</dd>
              </div>
              <div>
                <dt>Contracts</dt>
                <dd>~{Math.round(plan.market_buy.contracts * 100) / 100}</dd>
              </div>
              <div>
                <dt>Fee</dt>
                <dd>{money(plan.market_buy.fee_usd)}</dd>
              </div>
            </dl>
            {plan.market_buy.ends_at && (
              <p className="muted">
                <Countdown until={plan.market_buy.ends_at} before="Ends in " />
              </p>
            )}
            {result ? (
              <div className={`note note-${RESULT[result.status].tone}`} role="status">
                <StatusMark tone={RESULT[result.status].tone} label={RESULT[result.status].label} />
                <span>{result.detail}</span>
              </div>
            ) : (
              <div className="actions">
                <button type="button" className="btn" onClick={cancel}>
                  Cancel
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={busy}
                  onClick={() => void run("cheapest")}
                >
                  Confirm
                </button>
              </div>
            )}
          </div>
        )}

        {plan?.kind === "market_move" && plan.market_move && (
          <div className="quote quote-move">
            <p>
              Move a position from <strong>{plan.market_move.from_venue}</strong> to{" "}
              <strong>{plan.market_move.to_venue}</strong>
              {plan.market_move.slug ? "" : ". I could not find that market, pick one in Markets."}
            </p>
            {onOpenMarkets && (
              <button type="button" className="btn btn-primary" onClick={onOpenMarkets}>
                Open Markets
              </button>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
