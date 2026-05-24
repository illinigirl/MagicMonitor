"use client";

import { useEffect, useState } from "react";

/**
 * Small "Updated Xs ago" indicator showing how fresh the live park
 * data is. The server renders an absolute time string (e.g.
 * "5:42 PM") at request time; once JS hydrates, this component
 * swaps to a relative form ("47s ago") and recomputes every 10s so
 * the user can see freshness without refreshing the page.
 *
 * SSR + hydration safety: the initial render uses the
 * `initialAbsolute` prop the server already computed. The first
 * useEffect tick then upgrades to the relative form. Because the
 * initial state matches the server's HTML, there's no hydration
 * mismatch.
 *
 * If JS is disabled, the user still sees the absolute time —
 * graceful degradation.
 */
export function UpdatedIndicator({
  iso,
  initialAbsolute,
}: {
  iso: string;
  initialAbsolute: string;
}) {
  const [label, setLabel] = useState(initialAbsolute);

  useEffect(() => {
    // Compute the relative form immediately on mount (replaces the
    // server-rendered absolute time with the fresher relative form),
    // then refresh every 10s. 10s feels live without being a CPU
    // drain — a stale-by-9s display is fine for a 2-min-poll source.
    function tick() {
      setLabel(formatRelative(iso));
    }
    tick();
    const id = setInterval(tick, 10_000);
    return () => clearInterval(id);
  }, [iso]);

  return (
    <span title={`Last poll: ${iso}`}>Updated {label}</span>
  );
}

/**
 * Bucket the (now - iso) delta into a short human label. Coarsens
 * the unit as the delta grows so the indicator stays compact and
 * the unit conveys "is this fresh or stale" at a glance.
 */
function formatRelative(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const sec = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  return `${day}d ago`;
}
