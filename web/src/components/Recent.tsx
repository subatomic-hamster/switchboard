import { useState } from "react";

import type { RecentAction, RecentKind } from "../demo-types";
import { ago, money } from "../format";
import { useNow } from "./Countdown";

const FEW = 6;

const ICON: Record<RecentKind, string> = {
  send: "M5 12h14M13 6l6 6-6 6",
  receive: "M19 12H5M11 6l-6 6 6 6",
  bet: "M4 19V5M4 19h16M8 15l4-4 3 3 5-6",
  move: "M4 8h13M13 4l4 4-4 4M20 16H7M11 12l-4 4 4 4",
  swap: "M7 4v16M7 4L3 8M7 4l4 4M17 20V4M17 20l-4-4M17 20l4-4",
  win: "M8 4h8v5a4 4 0 0 1-8 0zM8 6H4v1a3 3 0 0 0 4 3M16 6h4v1a3 3 0 0 1-4 3M12 13v4M8 20h8",
};

/** Recent money actions, newest first. */
export function Recent({ actions }: { actions: RecentAction[] }) {
  const [all, setAll] = useState(false);
  const now = useNow(30_000);
  if (actions.length === 0) return null;
  const shown = all ? actions : actions.slice(0, FEW);
  return (
    <section className="card" aria-labelledby="recent-title">
      <h2 id="recent-title">Recent</h2>
      <ul className="feed">
        {shown.map((action) => (
          <li key={`${action.ts}-${action.title}`} className="feed-item">
            <span className="feed-icon">
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d={ICON[action.kind]} />
              </svg>
            </span>
            <div className="feed-text">
              <span className="feed-title">
                <span className="visually-hidden">{action.kind} </span>
                {action.title}
              </span>
              {action.detail && <span className="feed-detail">{action.detail}</span>}
            </div>
            {action.amount_usd != null && (
              <strong>
                {money(action.amount_usd)}
                {action.asset ? ` ${action.asset}` : ""}
              </strong>
            )}
            <time dateTime={action.ts}>{ago(action.ts, now)}</time>
          </li>
        ))}
      </ul>
      {actions.length > FEW && (
        <button type="button" className="link-button" onClick={() => setAll((open) => !open)}>
          {all ? "Show fewer" : `Show more (${actions.length - FEW})`}
        </button>
      )}
    </section>
  );
}
