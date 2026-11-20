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

/** Headline numbers: budget left today, how often the owner says yes, and call usage. */
export function StatsPanel({ stats }: { stats: StatsResponse | null }) {
  if (!stats) {
    return (
      <section className="panel" aria-labelledby="stats-title">
        <h2 id="stats-title">Limits</h2>
        <p className="empty">Waiting for the server…</p>
      </section>
    );
  }
  const { policy, approvals } = stats;
  const rate = approvals.rate_pct;
  return (
    <section className="panel" aria-labelledby="stats-title">
      <h2 id="stats-title">Limits</h2>
      <div className="tiles">
        <div className="tile">
          <span className="tile-label">Budget left today</span>
          <span className="tile-value">{money(stats.budget_remaining_usd)}</span>
          <Meter
            used={stats.spent_today_usd}
            total={policy.daily_budget_usd}
            label="Share of today's budget spent"
          />
          <span className="tile-note">
            {money(stats.spent_today_usd)} spent of {money(policy.daily_budget_usd)}
          </span>
        </div>
        <div className="tile">
          <span className="tile-label">Per-transfer limit</span>
          <span className="tile-value">{money(policy.transfer_cap_usd)}</span>
          <span className="tile-note">
            Above it, your phone rings. Above {money(stats.passkey_threshold_usd)}, a passkey too.
          </span>
        </div>
        <div className="tile">
          <span className="tile-label">Approval rate</span>
          <span className="tile-value">{rate == null ? "—" : `${Math.round(rate)}%`}</span>
          <span className="tile-note">
            {approvals.total === 0
              ? "No requests decided yet"
              : `${approvals.approved} approved, ${approvals.denied} denied`}
          </span>
        </div>
        <div className="tile">
          <span className="tile-label">Approval calls, last 24 h</span>
          <span className="tile-value">
            {stats.calls_today}
            <span className="tile-of"> of {stats.max_calls_per_day}</span>
          </span>
          <Meter
            used={stats.calls_today}
            total={stats.max_calls_per_day}
            label="Share of the daily call limit used"
          />
          <span className="tile-note">Past the limit, requests are denied without a call.</span>
        </div>
      </div>
      {approvals.warning && (
        <div className="notice notice-warning" role="alert">
          <StatusMark tone="warning" label="Rubber-stamping" />
          <span>{approvals.warning}</span>
        </div>
      )}
    </section>
  );
}
