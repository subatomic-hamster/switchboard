import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "./api";
import type { Portfolio, RecentAction, Suggestion } from "./demo-types";

export interface DemoData {
  /** null until the first answer; true when /api/demo/suggestions works, false when it is a 404. */
  on: boolean | null;
  suggestions: Suggestion[];
  portfolio: Portfolio | null;
  recent: RecentAction[];
}

const PORTFOLIO_MS = 3000;
const SUGGESTIONS_MS = 5000;

/**
 * Demo-mode data. Detection is "the suggestions route answers". While `visible` (Home is showing
 * and the document is not hidden) the portfolio refreshes every 3 s and suggestions every 5 s;
 * every new event (`refreshKey`) refetches all of it.
 */
export function useDemo(refreshKey: number, visible: boolean): DemoData {
  const [on, setOn] = useState<boolean | null>(null);
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [recent, setRecent] = useState<RecentAction[]>([]);

  const loadSuggestions = useCallback(
    () =>
      api
        .demoSuggestions()
        .then((list) => {
          setOn(true);
          setSuggestions(list);
        })
        .catch((caught) => {
          if (caught instanceof ApiError && caught.status === 404) setOn(false);
        }),
    [],
  );
  const loadPortfolio = useCallback(
    () => api.demoPortfolio().then(setPortfolio).catch(() => undefined),
    [],
  );
  const loadRecent = useCallback(
    () => api.demoRecent().then(setRecent).catch(() => undefined),
    [],
  );

  // Everything again on a new event (and once on start, which is also the detection).
  useEffect(() => {
    void loadSuggestions();
    if (on) {
      void loadPortfolio();
      void loadRecent();
    }
  }, [refreshKey, on, loadSuggestions, loadPortfolio, loadRecent]);

  useEffect(() => {
    if (!visible || on === false) return;
    const tick = (load: () => unknown) => () => {
      if (!document.hidden) void load();
    };
    const timers = [setInterval(tick(loadSuggestions), SUGGESTIONS_MS)];
    if (on) timers.push(setInterval(tick(loadPortfolio), PORTFOLIO_MS));
    const onVisible = () => {
      if (!document.hidden && on) {
        void loadPortfolio();
        void loadSuggestions();
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      timers.forEach(clearInterval);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [visible, on, loadSuggestions, loadPortfolio]);

  return { on, suggestions, portfolio, recent };
}
