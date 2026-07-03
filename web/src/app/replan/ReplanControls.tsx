"use client";

/**
 * Approve/Dismiss controls for one disrupted ride on /replan. Drop moves
 * it out of the poller's watch set (atomic); Keep un-drops. State comes
 * down from the server (dropped_ride_ids), so it survives reloads and
 * reflects MCP-side changes.
 */

import { useState, useTransition } from "react";

import { applyDrop, type ReplanResult } from "./actions";

export default function ReplanControls({
  planId,
  rideId,
  rideName,
  initiallyDropped,
}: {
  planId: string;
  rideId: string;
  rideName: string;
  initiallyDropped: boolean;
}) {
  const [pending, startTransition] = useTransition();
  const [dropped, setDropped] = useState(initiallyDropped);
  const [error, setError] = useState<string | null>(null);

  const run = (next: boolean) => {
    setError(null);
    startTransition(async () => {
      const res: ReplanResult = await applyDrop(planId, rideId, next);
      if (res.ok) setDropped(next);
      else setError(res.error ?? "Couldn't update.");
    });
  };

  if (dropped) {
    return (
      <div className="flex items-center gap-3">
        <span className="rounded-full bg-bad/15 px-3 py-1 text-xs font-medium text-bad">
          Dropped from today
        </span>
        <button
          type="button"
          onClick={() => run(false)}
          disabled={pending}
          className="text-xs text-fg-3 underline hover:text-fg-1 disabled:opacity-50"
        >
          {pending ? "…" : "Undo"}
        </button>
        {error && <span className="text-xs text-warn">{error}</span>}
      </div>
    );
  }

  return (
    <div className="flex items-center gap-2">
      <button
        type="button"
        onClick={() => run(true)}
        disabled={pending}
        className="rounded-md border border-bad/40 bg-bad/10 px-3 py-1.5 text-sm font-medium text-bad hover:bg-bad/20 disabled:opacity-50"
      >
        {pending ? "…" : `Drop ${rideName}`}
      </button>
      <span className="text-xs text-fg-3">or leave it — it may come back up</span>
      {error && <span className="text-xs text-warn">{error}</span>}
    </div>
  );
}
