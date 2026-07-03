"use client";

/**
 * "I have an LL at ⏱" control per ride on the schedule. Records a held
 * Lightning Lane return time (self-serve, no Claude needed) so the poller
 * stops alerting about earlier LLs that are still later than what you
 * hold. Shows the current hold with a clear/edit affordance.
 */

import { useState, useTransition } from "react";

import { applyHeldLl } from "./actions";

/** ISO ("…T15:30:00-04:00") → "3:30 PM" for display. */
function fmt(iso: string): string {
  try {
    const t = iso.slice(11, 16); // HH:MM
    const [h, m] = t.split(":").map(Number);
    const ap = h >= 12 ? "PM" : "AM";
    const h12 = h % 12 === 0 ? 12 : h % 12;
    return `${h12}:${String(m).padStart(2, "0")} ${ap}`;
  } catch {
    return iso;
  }
}

export default function HeldLlInput({
  planId,
  rideId,
  dateIso,
  heldIso,
}: {
  planId: string;
  rideId: string;
  dateIso: string;
  heldIso: string | null;
}) {
  const [pending, start] = useTransition();
  const [held, setHeld] = useState<string | null>(heldIso);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = (time: string) => {
    setError(null);
    start(async () => {
      const res = await applyHeldLl(planId, rideId, dateIso, time);
      if (res.ok) {
        setHeld(time ? `${dateIso}T${time.padStart(5, "0")}:00` : null);
        setEditing(false);
      } else setError(res.error ?? "Couldn't save.");
    });
  };

  if (held && !editing) {
    return (
      <span className="inline-flex items-center gap-1.5 text-xs text-gold">
        🎟 LL {fmt(held)}
        <button
          type="button"
          onClick={() => setEditing(true)}
          disabled={pending}
          className="text-fg-3 underline hover:text-fg-1"
        >
          edit
        </button>
        <button
          type="button"
          onClick={() => save("")}
          disabled={pending}
          className="text-fg-3 underline hover:text-fg-1"
        >
          clear
        </button>
      </span>
    );
  }

  if (editing || !held) {
    return (
      <span className="inline-flex items-center gap-1.5 text-xs">
        <label className="text-fg-3">🎟 LL at</label>
        <input
          type="time"
          defaultValue={held ? held.slice(11, 16) : ""}
          disabled={pending}
          onBlur={(e) => e.target.value && save(e.target.value)}
          className="rounded border border-line bg-bg-0 px-1 py-0.5 text-fg-0"
        />
        {editing && (
          <button
            type="button"
            onClick={() => setEditing(false)}
            className="text-fg-3 underline"
          >
            cancel
          </button>
        )}
        {error && <span className="text-warn">{error}</span>}
      </span>
    );
  }
  return null;
}
