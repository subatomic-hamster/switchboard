import { useState } from "react";

import { CommandBar } from "./components/CommandBar";
import { EventFeed } from "./components/EventFeed";
import { useEventStream } from "./events";

const STREAM_LABEL = {
  connecting: "Connecting…",
  live: "Live",
  reconnecting: "Reconnecting…",
} as const;

export function App() {
  const stream = useEventStream();
  const [text, setText] = useState("");

  return (
    <div className="app">
      <header className="topbar">
        <div>
          <h1>Switchboard</h1>
          <p className="tagline">
            Say where the money should go. It moves inside limits a contract enforces, and phones
            you when something needs your say.
          </p>
        </div>
        <span className={`stream stream-${stream.state}`} role="status">
          <span className="dot" aria-hidden="true" />
          {STREAM_LABEL[stream.state]}
        </span>
      </header>
      <main className="layout">
        <div className="column">
          <CommandBar text={text} onTextChange={setText} />
        </div>
        <div className="column">
          <EventFeed events={stream.events} />
        </div>
      </main>
    </div>
  );
}
