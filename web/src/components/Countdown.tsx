import { useEffect, useState } from "react";

import { countdown } from "../format";
import type { DemoOffer } from "../demo-types";

/** The current time, re-read every second. */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

/** "ends in 4:05"; nothing once the time has passed. Not a live region, so it never chatters. */
export function Countdown({ until, before = "", after = "" }: { until: string; before?: string; after?: string }) {
  const left = countdown(Date.parse(until) - useNow());
  if (!left) return null;
  return (
    <span className="countdown">
      {before}
      {left}
      {after}
    </span>
  );
}

/** A slim line above the command box while an offer is running. */
export function OfferBanner({ offer }: { offer: DemoOffer | null }) {
  const now = useNow();
  if (!offer || Date.parse(offer.expires_at) <= now) return null;
  return (
    <p className="offer-banner" role="status">
      <strong>{offer.headline}</strong>
      <Countdown until={offer.expires_at} after=" left" />
    </p>
  );
}
