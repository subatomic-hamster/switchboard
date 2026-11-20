import { useEffect, useState } from "react";

import { api } from "./api";
import { EVENT_TYPES, type SwitchboardEvent } from "./types";

export const MAX_EVENTS = 2000;
const RETRY_MS = 2000;
const MAX_LIVE = 300;

/** Add events to the log: ordered by id, no duplicates, capped at MAX_EVENTS. */
export function mergeEvents(
  current: SwitchboardEvent[],
  incoming: SwitchboardEvent[],
): SwitchboardEvent[] {
  if (incoming.length === 0) return current;
  const seen = new Set(current.map((event) => event.id));
  const fresh = incoming.filter((event) => !seen.has(event.id));
  if (fresh.length === 0) return current;
  const merged = [...current, ...fresh].sort((a, b) => a.id - b.id);
  return merged.length > MAX_EVENTS ? merged.slice(merged.length - MAX_EVENTS) : merged;
}

export type StreamState = "connecting" | "live" | "reconnecting";

export interface EventStream {
  events: SwitchboardEvent[];
  state: StreamState;
  /** Events received live (not from history), oldest first, for animations and tones. */
  live: SwitchboardEvent[];
}

/**
 * The event log: stored history first, then the live `/events` stream.
 *
 * EventSource retries by itself after a dropped connection and sends Last-Event-ID. It gives up
 * for good if the first response is an error (for example the server is still starting), so
 * that case is retried here, and history is fetched again on every (re)connect.
 */
export function useEventStream(): EventStream {
  const [events, setEvents] = useState<SwitchboardEvent[]>([]);
  const [state, setState] = useState<StreamState>("connecting");
  const [live, setLive] = useState<SwitchboardEvent[]>([]);

  useEffect(() => {
    let source: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;

    const loadHistory = () => {
      api
        .history()
        .then((history) => {
          if (!stopped) setEvents((current) => mergeEvents(current, history));
        })
        .catch(() => {
          // The stream state already tells the user the server is unreachable.
        });
    };

    const onMessage = (message: MessageEvent<string>) => {
      const event = JSON.parse(message.data) as SwitchboardEvent;
      setEvents((current) => mergeEvents(current, [event]));
      setLive((current) => [...current.slice(-MAX_LIVE + 1), event]);
    };

    const connect = () => {
      if (stopped || typeof EventSource === "undefined") return;
      source = new EventSource("/events");
      source.onopen = () => {
        setState("live");
        loadHistory();
      };
      source.onerror = () => {
        setState("reconnecting");
        if (source?.readyState === EventSource.CLOSED) {
          source.close();
          retry = setTimeout(connect, RETRY_MS);
        }
      };
      for (const type of EVENT_TYPES) source.addEventListener(type, onMessage);
    };

    loadHistory();
    connect();

    return () => {
      stopped = true;
      clearTimeout(retry);
      source?.close();
    };
  }, []);

  return { events, state, live };
}
