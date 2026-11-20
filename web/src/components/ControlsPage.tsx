import { useState } from "react";

import { money } from "../format";
import type { StatsResponse } from "../types";
import { ConnectorsPanel } from "./ConnectorsPanel";
import { HealthPanel } from "./HealthPanel";
import { HostileBox } from "./HostileBox";
import { RulesBox } from "./RulesBox";

export interface ControlsPageProps {
  stats: StatsResponse | null;
  /** Bumps when an event arrives, so lists refresh. */
  refreshKey: number;
  onChanged: () => void;
}

/** Limits and the rule box, assistants, health, and the fake-attack test. */
export function ControlsPage({ stats, refreshKey, onChanged }: ControlsPageProps) {
  const [seed, setSeed] = useState<{ text: string; at: number } | null>(null);
  const change = (text: string) => setSeed({ text, at: Date.now() });

  return (
    <div className="controls">
      <h1 className="visually-hidden">Controls</h1>
      <div className="controls-main">
        <section className="card" aria-labelledby="limits-title">
          <h2 id="limits-title">Limits</h2>
          {stats ? (
            <ul className="lines">
              <li className="line">
                <span className="grow">Send without asking, up to</span>
                <strong>{money(stats.policy.transfer_cap_usd)}</strong>
                <button
                  type="button"
                  className="btn"
                  aria-label="Change the no-ask limit"
                  onClick={() => change("Set my transfer limit to ")}
                >
                  Change
                </button>
              </li>
              <li className="line">
                <span className="grow">Spend per day, up to</span>
                <strong>{money(stats.policy.daily_budget_usd)}</strong>
                <button
                  type="button"
                  className="btn"
                  aria-label="Change the daily limit"
                  onClick={() => change("Set my daily budget to ")}
                >
                  Change
                </button>
              </li>
              <li className="line">
                <span className="grow" title="Set on the server">
                  Approval calls per day
                </span>
                <strong>{stats.max_calls_per_day}</strong>
              </li>
            </ul>
          ) : (
            <p className="empty">Waiting for the server…</p>
          )}
          <RulesBox
            callsLive={stats?.agent_tools_live ?? false}
            refreshKey={refreshKey}
            onChanged={onChanged}
            seed={seed}
          />
        </section>
        <ConnectorsPanel refreshKey={refreshKey} />
      </div>
      <div className="controls-side">
        <HealthPanel stats={stats} onChanged={onChanged} />
        <details className="card fold-card">
          <summary>Test it with a fake attack</summary>
          <HostileBox onChanged={onChanged} />
        </details>
      </div>
    </div>
  );
}
