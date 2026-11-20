import { type KeyboardEvent, useEffect, useRef, useState } from "react";

import { ApiError, api } from "../api";
import type { DemoPosition, Insight, LinkedAccount } from "../demo-types";
import { betName, cents, contracts, money, signedMoney, type Tone } from "../format";
import { Countdown } from "./Countdown";
import { StatusMark, ToneIcon } from "./StatusMark";

const FEW = 6;

const VERDICT: Record<Insight["verdict"], { tone: Tone; label: string }> = {
  go: { tone: "good", label: "Go" },
  against: { tone: "critical", label: "Against" },
  neutral: { tone: "neutral", label: "Neutral" },
};

/** Briefly "flash-up" or "flash-down" when the price changes. CSS drops the animation for reduced motion. */
function useFlash(price: number): string {
  const previous = useRef(price);
  const [flash, setFlash] = useState("");
  useEffect(() => {
    if (price === previous.current) return;
    setFlash(price > previous.current ? "flash-up" : "flash-down");
    previous.current = price;
    const timer = setTimeout(() => setFlash(""), 900);
    return () => clearTimeout(timer);
  }, [price]);
  return flash;
}

function InsightPanel({ position, onClose }: { position: DemoPosition; onClose: () => void }) {
  const [insight, setInsight] = useState<Insight | null>(null);
  const [error, setError] = useState("");
  const box = useRef<HTMLElement>(null);

  useEffect(() => {
    box.current?.focus();
    let stale = false;
    api
      .demoInsight(position.id)
      .then((result) => !stale && setInsight(result))
      .catch((caught) => !stale && setError(caught instanceof ApiError ? caught.message : "Research failed."));
    return () => {
      stale = true;
    };
  }, [position.id]);

  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      onClose();
    }
  };
  const verdict = insight && VERDICT[insight.verdict];

  return (
    <section
      ref={box}
      tabIndex={-1}
      className="insight"
      aria-label={`Research: ${betName(position.title)}`}
      onKeyDown={onKeyDown}
    >
      <div aria-live="polite">
        {!insight && !error && <p className="muted">Checking…</p>}
        {error && <p className="inline-error">{error}</p>}
        {insight && verdict && (
          <>
            <div className="insight-head">
              <StatusMark tone={verdict.tone} label={verdict.label} />
              <strong>{insight.headline}</strong>
            </div>
            <p>{insight.body}</p>
            {insight.checks.length > 0 && (
              <ul className="checks insight-checks" aria-label="Checks">
                {insight.checks.map((check) => (
                  <li key={check.field}>
                    <span className={`tone-${check.same ? "good" : "critical"}`}>
                      <ToneIcon tone={check.same ? "good" : "critical"} />
                    </span>
                    <strong>{check.field}</strong>
                    <span className="muted">
                      {check.same ? "same" : "different"}
                      {check.detail ? `: ${check.detail}` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {insight.alternative && (
              <p>
                <span className="muted">Instead: </span>
                {insight.alternative}
              </p>
            )}
            <p className="muted insight-source">
              {insight.source === "grok" ? "by Grok" : "fallback"}
            </p>
          </>
        )}
      </div>
      <button type="button" className="btn" onClick={onClose}>
        Close
      </button>
    </section>
  );
}

function PositionCard({ position, onMove }: { position: DemoPosition; onMove: (text: string) => void }) {
  const [research, setResearch] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  const flash = useFlash(position.price);
  const name = betName(position.title);
  const open = position.status === "open";
  const other = position.venue === "Gemini" ? "Polymarket US" : "Gemini";
  // The move engine runs between Polymarket US and Gemini; linked Polymarket positions are research only.
  const movable = position.venue !== "Polymarket";
  const closeResearch = () => {
    setResearch(false);
    opener.current?.focus();
  };

  return (
    <li className={`position${open ? "" : " position-done"}`}>
      <div className="position-top">
        <span className="badge">{position.venue}</span>
        {position.status === "won" && <StatusMark tone="good" label="Won" />}
        {position.status === "lost" && <StatusMark tone="critical" label="Lost" />}
        {open && position.ends_at && <Countdown until={position.ends_at} before="ends in " />}
      </div>
      <h3 className="position-title">{name}</h3>
      <p className="muted">
        {position.side} · {contracts(position.contracts)} contracts
      </p>
      <div className="position-nums">
        <span className={`odds ${flash}`} aria-label={`Odds ${cents(position.price)}`}>
          {cents(position.price)}
        </span>
        <span>{money(position.value_usd)}</span>
        <span className={position.pnl_usd < 0 ? "tone-critical" : "tone-good"}>
          {signedMoney(position.pnl_usd)}
        </span>
      </div>
      {position.adjacent && (
        <p className="muted">
          Similar on {position.adjacent.venue}: {position.adjacent.title}
          {position.adjacent.price != null && ` ${cents(position.adjacent.price)}`}
        </p>
      )}
      {open && (
        <div className="actions actions-start">
          <button
            ref={opener}
            type="button"
            className="btn"
            aria-expanded={research}
            onClick={() => (research ? closeResearch() : setResearch(true))}
          >
            Research
            <span className="visually-hidden"> {name}</span>
          </button>
          {movable && (
            <button
              type="button"
              className="btn"
              onClick={() => onMove(`Move my ${name} position to ${other}`)}
            >
              Move
              <span className="visually-hidden"> {name}</span>
            </button>
          )}
        </div>
      )}
      {research && <InsightPanel position={position} onClose={closeResearch} />}
    </li>
  );
}

/** Your positions: a card each, with live odds. Moved ones are left out. Positions held through
 * Switchboard (Polymarket US, Gemini) come first, then the linked account's, biggest first. */
export function Positions({
  positions,
  account,
  onMove,
}: {
  positions: DemoPosition[];
  account: LinkedAccount | null;
  onMove: (text: string) => void;
}) {
  const [all, setAll] = useState(false);
  const live = positions
    .filter((position) => position.status !== "moved")
    .sort(
      (a, b) =>
        Number(a.venue === "Polymarket") - Number(b.venue === "Polymarket") ||
        b.value_usd - a.value_usd,
    );
  const shown = all ? live : live.slice(0, FEW);
  return (
    <section className="card" aria-labelledby="positions-title">
      <div className="card-head">
        <h2 id="positions-title">Your positions</h2>
        {account?.linked && (
          <a className="badge" href={account.profile_url} target="_blank" rel="noopener noreferrer">
            Polymarket linked · {account.name}
          </a>
        )}
      </div>
      {live.length === 0 ? (
        <p className="empty">None yet.</p>
      ) : (
        <ul className="positions">
          {shown.map((position) => (
            <PositionCard key={position.id} position={position} onMove={onMove} />
          ))}
        </ul>
      )}
      {live.length > FEW && (
        <button type="button" className="link-button" onClick={() => setAll((open) => !open)}>
          {all ? "Show fewer" : `Show all (${live.length})`}
        </button>
      )}
    </section>
  );
}
