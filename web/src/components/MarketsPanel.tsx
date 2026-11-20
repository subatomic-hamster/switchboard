import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "../api";
import { betName, money, type Tone } from "../format";
import { canSpeak, speak } from "../speech";
import type { BotNote, MarketBrief, MarketListing, MoveOption, Opinion, TrackRecord } from "../types";
import { StatusMark, ToneIcon } from "./StatusMark";

const CHECKS: { key: keyof MarketBrief["match"]["checks"]; label: string }[] = [
  { key: "event", label: "Event" },
  { key: "strike", label: "Threshold" },
  { key: "direction", label: "Direction" },
  { key: "time", label: "Event time" },
  { key: "source", label: "Who decides" },
];

const CHECK_TONE: Record<string, Tone> = {
  same: "good",
  different: "critical",
  unknown: "warning",
  "n/a": "neutral",
};

const MODE_LABEL: Record<MoveOption["mode"], string> = {
  take: "Sell now",
  post: "Post and wait",
};

const price = (value: number) => `${(value * 100).toFixed(value * 100 % 1 === 0 ? 0 : 1)}¢`;
const percent = (value: number) => `${Math.round(value * 100)}%`;

function TrackRecordLine({ record }: { record: TrackRecord }) {
  if (record.revealed === 0 || record.mean_brier == null || record.market_brier == null) {
    return (
      <p className="muted">
        Track record: no forecasts scored yet
        {record.pending > 0 ? `, ${record.pending} waiting on their events` : ""}.
      </p>
    );
  }
  return (
    <p className="muted">
      Track record: {record.revealed} scored. Average Brier{" "}
      <strong>{record.mean_brier.toFixed(3)}</strong> against the market price's{" "}
      <strong>{record.market_brier.toFixed(3)}</strong> on the same events (lower is better; 0.25
      is a coin flip).
    </p>
  );
}

function OpinionBlock({ opinion }: { opinion: Opinion }) {
  const label = opinion.stance === "hold" ? "I'd hold" : "Moving is defensible";
  // The spoken summary opens with the stance; the mark already shows it.
  const summary = opinion.summary.startsWith(`${label}.`)
    ? opinion.summary.slice(label.length + 1).trimStart()
    : opinion.summary;
  return (
    <div className="opinion">
      <div className="opinion-head">
        <StatusMark tone={opinion.stance === "hold" ? "warning" : "good"} label={label} />
        <span>{summary}</span>
      </div>
      <details className="fold">
        <summary>What each model said</summary>
        <table className="grid">
          <caption className="visually-hidden">What each forecasting model said</caption>
          <thead>
            <tr>
              <th scope="col">Model family</th>
              <th scope="col" className="num">
                Probability
              </th>
              <th scope="col">The model's note</th>
            </tr>
          </thead>
          <tbody>
            {opinion.models.map((model) => (
              <tr key={`${model.family}-${model.model}`}>
                <th scope="row">
                  {model.family}
                  <span className="muted"> · {model.model}</span>
                </th>
                <td className="num">
                  {model.probability != null ? percent(model.probability) : "no answer"}
                </td>
                <td className="muted">{model.rationale || model.error}</td>
              </tr>
            ))}
            <tr>
              <th scope="row">Market price</th>
              <td className="num">{percent(opinion.market_price)}</td>
              <td className="muted">The starting point for every model</td>
            </tr>
            <tr>
              <th scope="row">Combined</th>
              <td className="num">
                <strong>{percent(opinion.probability)}</strong>
              </td>
              <td className="muted">Models averaged, then pulled halfway back to the market</td>
            </tr>
          </tbody>
        </table>
        <p className="muted receipt">
          Receipt {opinion.receipt_id}: hash <code>{opinion.commitment.slice(0, 20)}…</code>{" "}
          committed before the event ({opinion.anchor_ref}).
          {opinion.overridden ? " You overrode this opinion." : ""}
        </p>
      </details>
    </div>
  );
}

export interface MarketsPanelProps {
  /** A market chosen elsewhere (a spoken "move my Rockets position…"). */
  selectedSlug?: string | null;
  /** Contracts held in that market, when known: the move starts with all of them. */
  selectedQty?: number;
  /** Bumps when an event arrives, so receipts and briefs refresh. */
  refreshKey: number;
}

/** Move a position: one price per venue, a one-line verdict, how to sell, and the opinion. */
export function MarketsPanel({ selectedSlug, selectedQty, refreshKey }: MarketsPanelProps) {
  const [markets, setMarkets] = useState<MarketListing[]>([]);
  const [slug, setSlug] = useState("");
  const [qty, setQty] = useState(4);
  const [mode, setMode] = useState<MoveOption["mode"] | null>(null);
  const [brief, setBrief] = useState<MarketBrief | null>(null);
  const [notes, setNotes] = useState<BotNote[]>([]);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState("");

  useEffect(() => {
    api
      .deskMarkets()
      .then((list) => {
        setMarkets(list);
        setSlug((current) => current || list[0]?.slug || "");
      })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    if (selectedSlug) setSlug(selectedSlug);
    if (selectedQty) setQty(Math.floor(selectedQty));
  }, [selectedSlug, selectedQty]);

  const load = useCallback(() => {
    if (!slug || !(qty > 0)) return;
    api
      .deskBrief(slug, qty)
      .then(setBrief)
      .catch((caught: unknown) =>
        setMessage(caught instanceof ApiError ? caught.message : "Could not load the market."),
      );
    api
      .researchNotes(slug)
      .then(setNotes)
      .catch(() => setNotes([]));
  }, [slug, qty]);

  useEffect(load, [load, refreshKey]);

  async function act(name: string, action: () => Promise<string>) {
    setBusy(name);
    setMessage("");
    try {
      setMessage(await action());
    } catch (caught) {
      setMessage(caught instanceof ApiError ? caught.message : "Something went wrong.");
    } finally {
      setBusy("");
      load();
    }
  }

  const listing = brief?.listing;
  const refused = brief?.match.verdict === "refuse";
  const options = brief?.options ?? [];
  const cheapest = options.reduce<MoveOption | undefined>(
    (best, option) => (!best || option.cost_usd < best.cost_usd ? option : best),
    undefined,
  );
  const chosen = mode ?? cheapest?.mode ?? "post";
  const matched = brief
    ? CHECKS.filter((check) => ["same", "n/a"].includes(brief.match.checks[check.key])).length
    : 0;
  const holding = brief?.opinion?.stance === "hold" && !brief.opinion.overridden;

  return (
    <section className="card markets" aria-labelledby="markets-title">
      <div className="card-head">
        <h1 id="markets-title">Move a position</h1>
        {brief && canSpeak() && (
          <button type="button" className="chip" onClick={() => speak(brief.spoken)}>
            Read aloud
          </button>
        )}
      </div>

      <div className="market-pick">
        <label className="field-label grow">
          Bet
          <select value={slug} onChange={(event) => setSlug(event.target.value)}>
            {markets.map((market) => (
              <option key={market.slug} value={market.slug} title={market.title}>
                {betName(market.title, market.event_time)}
              </option>
            ))}
          </select>
        </label>
        <label className="field-label qty">
          Contracts
          <input
            type="number"
            min={0.01}
            step={0.01}
            value={qty}
            onChange={(event) => setQty(Number(event.target.value))}
          />
        </label>
      </div>

      {brief && listing && (
        <>
          <div className="venues-row">
            <div className="venue">
              <span className="muted">From</span>
              <span className="venue-name">Polymarket US</span>
              <span>
                Sell at <strong>{price(listing.polymarket.bid)}</strong>{" "}
                <span className="muted">
                  · {Math.round(listing.polymarket.bid_depth).toLocaleString("en-US")} wanted
                </span>
              </span>
            </div>
            <svg className="venue-arrow" width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
              <path d="M5 12h14M13 6l6 6-6 6" />
            </svg>
            <div className="venue">
              <span className="muted">To</span>
              <span className="venue-name">Gemini</span>
              {listing.gemini ? (
                <span>
                  Buy at <strong>{price(listing.gemini.ask)}</strong>
                </span>
              ) : (
                <span className="muted">No equivalent contract</span>
              )}
            </div>
          </div>

          <details className={`verdict verdict-${refused ? "critical" : "good"}`} open={refused}>
            <summary>
              <span className="verdict-icon">
                <ToneIcon tone={refused ? "critical" : "good"} size={20} />
              </span>
              <span className="verdict-title">{refused ? "Not the same bet" : "Same bet on both"}</span>
              <span className="verdict-count">· {matched} of {CHECKS.length} checks match</span>
            </summary>
            <ul className="checks" aria-label="Equivalence checks">
              {CHECKS.map((check) => {
                const value = brief.match.checks[check.key];
                return (
                  <li key={check.key}>
                    <span>{check.label}</span>
                    <StatusMark tone={CHECK_TONE[value] ?? "neutral"} label={value} />
                  </li>
                );
              })}
            </ul>
            {brief.match.notes.length > 0 && (
              <p className="muted">Note: {brief.match.notes.join("; ")}.</p>
            )}
            <p className="muted mono contract">
              {listing.slug} · {listing.gemini_symbol ?? "no equivalent contract"}
            </p>
          </details>
          {refused && <p className="verdict-why">{brief.spoken}</p>}

          {!refused && options.length > 0 && (
            <>
              <fieldset className="how">
                <legend>How to sell</legend>
                {options.map((option) => (
                  <label key={option.mode} className={`opt${chosen === option.mode ? " opt-on" : ""}`}>
                    <input
                      type="radio"
                      name="how"
                      checked={chosen === option.mode}
                      onChange={() => setMode(option.mode)}
                    />
                    <span className="opt-text">
                      <span className="opt-title">
                        {MODE_LABEL[option.mode]}
                        {options.length > 1 && option === cheapest && (
                          <span className="badge">Cheaper</span>
                        )}
                      </span>
                      <span className="muted">
                        Costs {money(option.cost_usd)} ({option.cost_pct}%)
                      </span>
                    </span>
                  </label>
                ))}
              </fieldset>

              {brief.opinion && <OpinionBlock opinion={brief.opinion} />}

              <div className="actions market-actions">
                <button
                  type="button"
                  className="btn"
                  disabled={busy !== ""}
                  title={brief.opinion ? undefined : brief.opinion_unavailable}
                  onClick={() =>
                    void act("forecast", async () => {
                      await api.forecast(slug, qty);
                      return "";
                    })
                  }
                >
                  {busy === "forecast" ? "Asking the models…" : "Get a second opinion"}
                </button>
                <span className="grow" />
                <span className="muted">Practice mode: no real orders</span>
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={busy !== ""}
                  onClick={() =>
                    void act("move", async () => {
                      if (brief.opinion?.stance === "hold" && !brief.opinion.overridden) {
                        await api.overrideForecast(brief.opinion.receipt_id);
                      }
                      const move = await api.deskMove(slug, qty, chosen);
                      return `Move ${move.move_id} ran as a dry run: no order was placed.`;
                    })
                  }
                >
                  {holding ? "Override and move" : "Move"} {qty} contract{qty === 1 ? "" : "s"}
                </button>
              </div>
            </>
          )}

          {notes.length > 0 && (
            <details className="fold research">
              <summary title="Written by a bot from outside sources. It does not change the check above.">
                Bot research, unverified
              </summary>
              {notes.map((note) => (
                <blockquote key={note.ts}>{note.body}</blockquote>
              ))}
            </details>
          )}
          <details className="fold">
            <summary>Details</summary>
            {!refused && options.length > 0 && (
              <p>
                <strong>Will I get a bid?</strong> {brief.bid_answer}
              </p>
            )}
            <TrackRecordLine record={brief.track_record} />
            <p className="muted disclaimer">{brief.disclaimer}</p>
          </details>
        </>
      )}

      {message && (
        <p role="status" className="note">
          {message}
        </p>
      )}
    </section>
  );
}
