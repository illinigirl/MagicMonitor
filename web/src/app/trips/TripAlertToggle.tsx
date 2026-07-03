"use client";

/**
 * "Get alerts for this trip" toggle — the /trips self-serve opt-in.
 *
 * Renders the signed-in member's subscription state for one trip (any
 * un-recorded day subscribed = on) and flips it via the setTripAlerts
 * server action, which writes the atomic set ADD/DELETE. The page passes
 * `subscribed` from the freshly-read rows, so state survives reloads and
 * reflects MCP-side changes too.
 */

import { useState, useTransition } from "react";

import { setTripAlerts } from "./actions";

export default function TripAlertToggle({
  planIds,
  subscribed,
}: {
  planIds: string[];
  subscribed: boolean;
}) {
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  const flip = () => {
    setError(null);
    startTransition(async () => {
      const res = await setTripAlerts(planIds, !subscribed);
      if (!res.ok) setError(res.error ?? "Couldn't update.");
      // On success the action revalidates /trips; the re-render brings
      // the new `subscribed` down from the server rows.
    });
  };

  return (
    <div className="shrink-0 text-right">
      <button
        type="button"
        onClick={flip}
        disabled={pending || planIds.length === 0}
        className={
          "rounded-full px-3 py-1 text-xs font-medium border transition-colors disabled:opacity-50 " +
          (subscribed
            ? "border-ok/40 bg-ok/15 text-ok"
            : "border-line bg-bg-1 text-fg-2 hover:text-fg-1")
        }
        aria-pressed={subscribed}
      >
        {pending
          ? "Saving…"
          : subscribed
            ? "Getting alerts ✓"
            : "Get alerts for this trip"}
      </button>
      {error && <p className="mt-1 text-xs text-warn">{error}</p>}
    </div>
  );
}
