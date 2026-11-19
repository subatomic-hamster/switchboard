import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "./api";
import { ApprovalPanel } from "./components/ApprovalPanel";
import { CommandBar } from "./components/CommandBar";
import { EventFeed } from "./components/EventFeed";
import { HostileBox } from "./components/HostileBox";
import { MarketsPanel } from "./components/MarketsPanel";
import { MicButton } from "./components/MicButton";
import { RingView } from "./components/RingView";
import { RulesBox } from "./components/RulesBox";
import { SafetyPanel } from "./components/SafetyPanel";
import { StatsPanel } from "./components/StatsPanel";
import { useEventStream } from "./events";
import { canSpeak, speak, stopSpeaking } from "./speech";
import type { ApprovalView, IntentResponse, StatsResponse, Wallet } from "./types";

const STREAM_LABEL = {
  connecting: "Connecting…",
  live: "Live",
  reconnecting: "Reconnecting…",
} as const;

export function App() {
  const stream = useEventStream();
  const [text, setText] = useState("");
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [approvals, setApprovals] = useState<ApprovalView[]>([]);
  const [wallets, setWallets] = useState<Wallet[]>([]);
  const [readAloud, setReadAloud] = useState(true);
  const [marketSlug, setMarketSlug] = useState<string | null>(null);
  const [spokenRequest, setSpokenRequest] = useState<{ text: string; at: number } | null>(null);
  const readAloudRef = useRef(readAloud);
  readAloudRef.current = readAloud;

  const refresh = useCallback(() => {
    api.stats().then(setStats).catch(() => undefined);
    api.approvals().then(setApprovals).catch(() => undefined);
  }, []);

  useEffect(() => {
    api.wallets().then(setWallets).catch(() => undefined);
  }, []);

  // Every event can change the numbers and the pending list, so refetch on each one.
  const lastId = stream.events.at(-1)?.id ?? 0;
  useEffect(refresh, [refresh, lastId, stream.state]);

  const onQuote = useCallback((plan: IntentResponse) => {
    if (readAloudRef.current) speak(plan.spoken_quote);
  }, []);

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
        <div className="topbar-status">
          {stats?.using_p1_stubs && (
            <span className="badge" title="Chain and venue calls are answered from fixtures">
              Stubbed chain and venues
            </span>
          )}
          {stats && !stats.calls_live && (
            <span className="badge" title="No phone call is placed">
              Calls: dry run
            </span>
          )}
          {stats && !stats.trading_enabled && <span className="badge">Trading: off</span>}
          <span className={`stream stream-${stream.state}`} role="status">
            <span className="dot" aria-hidden="true" />
            {STREAM_LABEL[stream.state]}
          </span>
        </div>
      </header>
      <main className="layout">
        <div className="column">
          <CommandBar
            text={text}
            onTextChange={setText}
            onQuote={onQuote}
            onMarketMove={setMarketSlug}
            submit={spokenRequest}
            micSlot={
              <MicButton
                onText={setText}
                onFinal={(sentence) => {
                  setText(sentence);
                  setSpokenRequest({ text: sentence, at: Date.now() });
                }}
              />
            }
            footer={
              canSpeak() && (
                <label className="toggle">
                  <input
                    type="checkbox"
                    checked={readAloud}
                    onChange={(event) => {
                      setReadAloud(event.target.checked);
                      if (!event.target.checked) stopSpeaking();
                    }}
                  />
                  Read the quote back aloud
                </label>
              )
            }
          />
          <ApprovalPanel
            approvals={approvals}
            callsLive={stats?.calls_live ?? false}
            passkeyRegistered={stats?.passkey_registered ?? false}
            onChanged={refresh}
          />
          <RingView
            signals={stream.live}
            wallets={wallets}
            frozen={stats?.frozen ?? false}
            stale={stats?.stream_stale ?? false}
          />
          <MarketsPanel selectedSlug={marketSlug} refreshKey={lastId} />
        </div>
        <div className="column">
          <StatsPanel stats={stats} />
          <EventFeed events={stream.events} />
          <SafetyPanel stats={stats} onChanged={refresh}>
            <RulesBox
              callsLive={stats?.calls_live ?? false}
              refreshKey={lastId}
              onChanged={refresh}
            />
            <HostileBox onChanged={refresh} />
          </SafetyPanel>
        </div>
      </main>
    </div>
  );
}
