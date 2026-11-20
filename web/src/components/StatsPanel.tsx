import { money } from "../format";
import type { StatsResponse } from "../types";
import { StatusMark } from "./StatusMark";

function Meter({ used, total, label }: { used: number; total: number; label: string }) {
  const share = total > 0 ? Math.min(1, used / total) : 0;
  return (
    <div
      className="meter"
      role="meter"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={total}
      aria-valuenow={Math.min(used, total)}
    >
      <div className="meter-fill" style={{ width: `${share * 100}%` }} />
    </div>
  );
}

/** Three numbers: what is left today, what sends without asking, and how many calls are used. */
export function StatsPanel({ stats }: { stats: StatsResponse | null }) {
  if (!stats) {
    return <p className="empty">Waiting for the server…</p>;
  }
  const { policy, approvals } = stats;
  return (
    <section className="tiles" aria-label="Limits">
      <div className="card tile">
        <span className="tile-label">Left today</span>
        <span className="tile-value">
          {money(stats.budget_remaining_usd)}
          <span className="tile-of"> of {money(policy.daily_budget_usd)}</span>
        </span>
        <Meter
          used={stats.spent_today_usd}
          total={policy.daily_budget_usd}
          label="Share of today's budget spent"
        />
      </div>
      <div
        className="card tile"
        title={`Above it, your phone rings. Above ${money(stats.passkey_threshold_usd)}, a passkey too.`}
      >
        <span className="tile-label">Sends without asking</span>
        <span className="tile-value">up to {money(policy.transfer_cap_usd)}</span>
        <span className="tile-note">More than that rings your phone</span>
      </div>
      <div className="card tile" title="Past the limit, requests are denied without a call.">
        <span className="tile-label">Calls today</span>
        <span className="tile-value">
          {stats.calls_today}
          <span className="tile-of"> of {stats.max_calls_per_day}</span>
        </span>
        <Meter
          used={stats.calls_today}
          total={stats.max_calls_per_day}
          label="Share of the daily call limit used"
        />
      </div>
      {approvals.warning && (
        <div className="note note-warning tiles-wide" role="alert">
          <StatusMark tone="warning" label="Rubber-stamping" />
          <span>{approvals.warning}</span>
        </div>
      )}
    </section>
  );
}
