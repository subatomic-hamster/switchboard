import { useState } from "react";

import { clock, collapseBlips, describe, eventExplorerLink } from "../format";
import type { SwitchboardEvent } from "../types";
import { ToneIcon } from "./StatusMark";

const FEW = 6;
const MANY = 60;

/** Newest first: icon, one-line title, one-line subtitle, time. */
export function EventFeed({ events }: { events: SwitchboardEvent[] }) {
  const [all, setAll] = useState(false);
  const shown = collapseBlips(events);
  const recent = shown.slice(-(all ? MANY : FEW)).reverse();
  if (shown.length === 0) return <p className="empty">Nothing yet.</p>;
  return (
    <>
      <ol className="feed" aria-live="polite">
        {recent.map((event) => {
          const line = describe(event);
          const link = eventExplorerLink(event);
          return (
            <li key={event.id} className="feed-item">
              <span className={`feed-icon tone-${line.tone}`}>
                <ToneIcon tone={line.tone} size={18} />
              </span>
              <div className="feed-text">
                <span className="feed-title">
                  <span className="visually-hidden">{line.label} </span>
                  {line.title}
                </span>
                {(line.detail || line.code) && (
                  <span className="feed-detail">
                    {line.code && <code className="reason-code">{line.code}</code>}
                    {line.detail}
                  </span>
                )}
              </div>
              {link && (
                <a
                  className="feed-explorer"
                  href={link.url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  View on {link.name}
                  <span className="visually-hidden"> (opens in a new tab)</span>
                </a>
              )}
              <time dateTime={event.ts}>{clock(event.ts)}</time>
            </li>
          );
        })}
      </ol>
      {shown.length > FEW && (
        <button type="button" className="link-button" onClick={() => setAll((open) => !open)}>
          {all ? "Show fewer" : `Show more (${Math.min(shown.length, MANY) - FEW})`}
        </button>
      )}
    </>
  );
}
