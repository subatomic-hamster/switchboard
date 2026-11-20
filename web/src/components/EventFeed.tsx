import { clock, describe } from "../format";
import type { SwitchboardEvent } from "../types";
import { StatusMark } from "./StatusMark";

const SHOWN = 60;

/** Newest first. Every line says what happened, why, and the reason code when there is one. */
export function EventFeed({ events }: { events: SwitchboardEvent[] }) {
  const recent = events.slice(-SHOWN).reverse();
  return (
    <section className="panel" aria-labelledby="feed-title">
      <h2 id="feed-title">Activity</h2>
      {recent.length === 0 ? (
        <p className="empty">
          Nothing yet. Type a sentence above, or run the mock demo to see a full day replayed.
        </p>
      ) : (
        <ol className="feed" aria-live="polite">
          {recent.map((event) => {
            const line = describe(event);
            return (
              <li key={event.id} className="feed-item">
                <div className="feed-head">
                  <StatusMark tone={line.tone} label={line.label} />
                  <time dateTime={event.ts}>{clock(event.ts)}</time>
                </div>
                <p className="feed-title">{line.title}</p>
                {(line.detail || line.code) && (
                  <p className="feed-detail">
                    {line.code && <code className="reason-code">{line.code}</code>}
                    {line.detail}
                  </p>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
