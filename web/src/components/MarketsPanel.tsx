import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "../api";
import { money, type Tone } from "../format";
import { canSpeak, speak } from "../speech";
import type { MarketBrief, MarketListing, Opinion, TrackRecord, Venue } from "../types";
import { StatusMark } from "./StatusMark";

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

const VENUE_TONE: Record<Venue["status"], Tone> = {
  connected: "good",
  fixtures: "warning",
  planned: "neutral",
};

const VENUE_LABEL: Record<Venue["status"], string> = {
  connected: "Connected",
  fixtures: "Recorded data",
  planned: "Planned",
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
  return (
    <div className="opinion">
      <div className="quote-head">
        <h3>The operator's opinion</h3>
        <StatusMark
          tone={opinion.stance === "hold" ? "warning" : "good"}
          label={opinion.stance === "hold" ? "I'd hold" : "Moving is defensible"}
        />
      </div>
      <p>{opinion.summary}</p>
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
    </div>
  );
}

export interface MarketsPanelProps {
  /** A market chosen elsewhere (a spoken "move my Rockets position…"). */
  selectedSlug?: string | null;
  /** Bumps when an event arrives, so receipts and briefs refresh. */
  refreshKey: number;
}

/** Both venues' prices side by side, the equivalence check, the move cost, and the opinion. */
export function MarketsPanel({ selectedSlug, refreshKey }: MarketsPanelProps) {
  const [markets, setMarkets] = useState<MarketListing[]>([]);
  const [venues, setVenues] = useState<Venue[]>([]);
  const [slug, setSlug] = useState("");
  const [qty, setQty] = useState(4);
  const [brief, setBrief] = useState<MarketBrief | null>(null);
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
    api.venues().then(setVenues).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (selectedSlug) setSlug(selectedSlug);
  }, [selectedSlug]);

  const load = useCallback(() => {
    if (!slug || !(qty > 0)) return;
    api
      .deskBrief(slug, qty)
      .then(setBrief)
      .catch((caught: unknown) =>
        setMessage(caught instanceof ApiError ? caught.message : "Could not load the market."),
      );
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

  return (
    <section className="panel" aria-labelledby="markets-title">
      <div className="panel-head">
        <h2 id="markets-title">Markets</h2>
        {brief && canSpeak() && (
          <button type="button" className="chip" onClick={() => speak(brief.spoken)}>
            Hear the operator
          </button>
        )}
      </div>

      <div className="row market-pick">
        <label className="grow">
          <span className="visually-hidden">Market</span>
          <select value={slug} onChange={(event) => setSlug(event.target.value)}>
            {markets.map((market) => (
              <option key={market.slug} value={market.slug}>
                {market.title}
              </option>
            ))}
          </select>
        </label>
        <label className="qty">
          <span className="muted">Contracts</span>
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
          <table className="grid">
            <caption className="visually-hidden">Prices on both venues</caption>
            <thead>
              <tr>
                <th scope="col">Venue</th>
                <th scope="col" className="num">
                  Bid
                </th>
                <th scope="col" className="num">
                  Ask
                </th>
                <th scope="col" className="num">
                  Bid depth
                </th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <th scope="row">
                  Polymarket US
                  <span className="muted mono contract"> {listing.slug}</span>
                </th>
                <td className="num">{price(listing.polymarket.bid)}</td>
                <td className="num">{price(listing.polymarket.ask)}</td>
                <td className="num">{listing.polymarket.bid_depth}</td>
              </tr>
              <tr>
                <th scope="row">
                  Gemini
                  <span className="muted mono contract">
                    {" "}
                    {listing.gemini_symbol ?? "no equivalent contract"}
                  </span>
                </th>
                <td className="num">{listing.gemini ? price(listing.gemini.bid) : "—"}</td>
                <td className="num">{listing.gemini ? price(listing.gemini.ask) : "—"}</td>
                <td className="num">{listing.gemini ? listing.gemini.bid_depth : "—"}</td>
              </tr>
            </tbody>
          </table>

          <div className={`notice notice-${refused ? "critical" : "good"}`}>
            <StatusMark
              tone={refused ? "critical" : "good"}
              label={refused ? "Not the same bet" : "Same bet"}
            />
            <span>
              {refused
                ? brief.spoken
                : `All five fields agree${brief.match.notes.length ? `. Note: ${brief.match.notes.join("; ")}.` : "."}`}
            </span>
          </div>
          <ul className="checks" aria-label="Equivalence checks">
            {CHECKS.map((check) => {
              const value = brief.match.checks[check.key];
              return (
                <li key={check.key}>
                  <span className="muted">{check.label}</span>
                  <StatusMark tone={CHECK_TONE[value] ?? "neutral"} label={value} />
                </li>
              );
            })}
          </ul>

          {!refused && brief.options.length > 0 && (
            <>
              <table className="grid">
                <caption className="visually-hidden">What the move costs, two ways</caption>
                <thead>
                  <tr>
                    <th scope="col">Moving {brief.qty} contracts</th>
                    <th scope="col" className="num">
                      Sell at
                    </th>
                    <th scope="col" className="num">
                      Buy at
                    </th>
                    <th scope="col" className="num">
                      Fees
                    </th>
                    <th scope="col" className="num">
                      Cost
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {brief.options.map((option) => (
                    <tr key={option.mode}>
                      <th scope="row">{option.label}</th>
                      <td className="num">{price(option.sell_price)}</td>
                      <td className="num">{price(option.buy_price)}</td>
                      <td className="num">{money(option.fees_usd)}</td>
                      <td className="num">
                        <strong>{money(option.cost_usd)}</strong> ({option.cost_pct}%)
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p>
                <strong>Will I get a bid?</strong> {brief.bid_answer}
              </p>

              {brief.opinion ? (
                <OpinionBlock opinion={brief.opinion} />
              ) : (
                <p className="muted">{brief.opinion_unavailable}</p>
              )}
              <TrackRecordLine record={brief.track_record} />

              <div className="row">
                <button
                  type="button"
                  disabled={busy !== ""}
                  onClick={() =>
                    void act("forecast", async () => {
                      await api.forecast(slug, qty);
                      return "";
                    })
                  }
                >
                  {busy === "forecast"
                    ? "Asking the models…"
                    : brief.opinion
                      ? "Ask again"
                      : "Ask for the operator's opinion"}
                </button>
                <button
                  type="button"
                  className="primary"
                  disabled={busy !== ""}
                  onClick={() =>
                    void act("move", async () => {
                      if (brief.opinion?.stance === "hold" && !brief.opinion.overridden) {
                        await api.overrideForecast(brief.opinion.receipt_id);
                      }
                      const move = await api.deskMove(slug, qty, "post");
                      return `Move ${move.move_id} ran as a dry run: no order was placed.`;
                    })
                  }
                >
                  {brief.opinion?.stance === "hold" && !brief.opinion.overridden
                    ? "Override and move (dry run)"
                    : "Move the position (dry run)"}
                </button>
              </div>
            </>
          )}
          <p className="muted disclaimer">{brief.disclaimer}</p>
        </>
      )}

      {message && (
        <p role="status" className="notice">
          {message}
        </p>
      )}

      <ul className="venues" aria-label="Venues">
        {venues.map((venue) => (
          <li key={venue.id}>
            <span>{venue.name}</span>
            <StatusMark tone={VENUE_TONE[venue.status]} label={VENUE_LABEL[venue.status]} />
          </li>
        ))}
      </ul>
    </section>
  );
}
