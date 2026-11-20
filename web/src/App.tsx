import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "./api";
import { ApprovalPanel } from "./components/ApprovalPanel";
import { CommandBar } from "./components/CommandBar";
import { ControlsPage } from "./components/ControlsPage";
import { EventFeed } from "./components/EventFeed";
import { FlightRecorder, type ReplayFrame } from "./components/FlightRecorder";
import { FreezeButton } from "./components/FreezeButton";
import { MarketsPanel } from "./components/MarketsPanel";
import { MicButton } from "./components/MicButton";
import { RingView } from "./components/RingView";
import { StatsPanel } from "./components/StatsPanel";
import { useEventStream } from "./events";
import { speak } from "./speech";
import type { ApprovalView, IntentResponse, StatsResponse, Wallet } from "./types";

const TABS = [
  { id: "home", label: "Home", icon: "M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z" },
  { id: "markets", label: "Markets", icon: "M4 19V5M4 19h16M8 15l4-4 3 3 5-6" },
  { id: "controls", label: "Controls", icon: "M4 7h10M18 7h2M4 17h4M12 17h8M16 5v4M10 15v4" },
] as const;

type TabId = (typeof TABS)[number]["id"];

/** The tab lives in the URL hash, so a reload (or a shared link) keeps it. */
function tabFromHash(): TabId {
  const wanted = window.location.hash.slice(1);
  return TABS.find((tab) => tab.id === wanted)?.id ?? "home";
}

export function App() {
  const stream = useEventStream();
  const [tab, setTab] = useState<TabId>(tabFromHash);
  const [text, setText] = useState("");
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [approvals, setApprovals] = useState<ApprovalView[]>([]);
  const [wallets, setWallets] = useState<Wallet[]>([]);
  const [marketSlug, setMarketSlug] = useState<string | null>(null);
  const [replay, setReplay] = useState<ReplayFrame | null>(null);
  const [spokenRequest, setSpokenRequest] = useState<{ text: string; at: number } | null>(null);
  // A quote asked for by voice is read back; a typed one is read only on request.
  const viaVoice = useRef(false);

  useEffect(() => {
    const onHash = () => setTab(tabFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

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
    if (viaVoice.current) speak(plan.spoken_quote);
  }, []);

  const pending = approvals.filter((approval) => approval.status === "pending").length;
  const streamLabel = stats?.frozen
    ? "Frozen"
    : stream.state !== "live"
      ? stream.state === "connecting"
        ? "Connecting…"
        : "Reconnecting…"
      : stats?.stream_stale
        ? "Paused"
        : "Live";
  const streamTitle = stats?.stream_stale
    ? "No word from the chain for a while, so spending is paused until it is back."
    : undefined;
  const streamTone = stats?.frozen || stats?.stream_stale ? "bad" : stream.state;

  return (
    <div className="app">
      <header className={`topbar${stats?.frozen ? " topbar-frozen" : ""}`}>
        <div className="topbar-inner">
          <span className="brand">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
              <circle cx="6" cy="12" r="3" />
              <circle cx="18" cy="6" r="3" />
              <circle cx="18" cy="18" r="3" />
              <path d="M9 12h3l3-4.5M12 12l3 4.5" />
            </svg>
            Switchboard
          </span>
          <nav className="tabs" aria-label="Main">
            {TABS.map((item) => (
              <a
                key={item.id}
                href={`#${item.id}`}
                className="tab"
                aria-current={tab === item.id ? "page" : undefined}
                onClick={() => setTab(item.id)}
              >
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
                  <path d={item.icon} />
                </svg>
                {item.label}
              </a>
            ))}
          </nav>
          <span className="spacer" />
          <span className={`stream stream-${streamTone}`} role="status" title={streamTitle}>
            <span className="dot" aria-hidden="true" />
            <span className="stream-label">{streamLabel}</span>
          </span>
          <FreezeButton stats={stats} onChanged={refresh} />
        </div>
      </header>

      {/* Always present, so a screen reader announces a new request when it arrives. */}
      <p className="visually-hidden" role="status">
        {pending > 0 ? `${pending} request${pending === 1 ? "" : "s"} need${pending === 1 ? "s" : ""} your approval.` : ""}
      </p>

      <main className="page">
        <div hidden={tab !== "home"} className="home">
          <h1 className="visually-hidden">Home</h1>
          <CommandBar
            text={text}
            onTextChange={(next) => {
              viaVoice.current = false;
              setText(next);
            }}
            onQuote={onQuote}
            onMarketMove={setMarketSlug}
            onOpenMarkets={() => {
              window.location.hash = "markets";
              setTab("markets");
            }}
            submit={spokenRequest}
            micSlot={
              <MicButton
                onText={setText}
                onFinal={(sentence) => {
                  viaVoice.current = true;
                  setText(sentence);
                  setSpokenRequest({ text: sentence, at: Date.now() });
                }}
              />
            }
          />
          <StatsPanel stats={stats} />
          <ApprovalPanel
            approvals={approvals}
            callsLive={stats?.calls_live ?? false}
            passkeyRegistered={stats?.passkey_registered ?? false}
            onChanged={refresh}
          />
          <div className="home-grid">
            <FlightRecorder events={stream.events} onFrame={setReplay}>
              <EventFeed events={stream.events} />
            </FlightRecorder>
            <RingView
              signals={replay ? replay.signals : stream.live}
              wallets={wallets}
              frozen={stats?.frozen ?? false}
              stale={stats?.stream_stale ?? false}
              replayLabel={replay?.label}
              resetKey={replay?.resetKey ?? 0}
            />
          </div>
        </div>
        {tab === "markets" && <MarketsPanel selectedSlug={marketSlug} refreshKey={lastId} />}
        {tab === "controls" && <ControlsPage stats={stats} refreshKey={lastId} onChanged={refresh} />}
      </main>
    </div>
  );
}
