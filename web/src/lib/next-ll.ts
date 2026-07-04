/**
 * "Next Lightning Lane worth grabbing" — the web twin of the poller's
 * nudge.pick_ll_candidate (infra/lambda/poller/nudge.py — KEEP THE RULE
 * IN SYNC): among the plan's REMAINING rides, skip anything the party
 * already holds an LL for, then pick the earliest usable return window
 * still in the future. Deterministic rules decide what to surface; the
 * full re-plan brain stays behind the Ask-Claude tap.
 *
 * Used at the mark-done moment (/done confirmation, /replan controls) —
 * the instant the family actually asks "what should we book next?".
 */

export interface NextLlSuggestion {
  ride_id: string;
  ride_name: string;
  /** Raw return ISO (ET offset) — format with formatEtTime for display. */
  return_start: string;
  price: string | null;
  /** Current standby, for the "beats Xm standby" framing. */
  standby_mins: number | null;
}

/** Don't suggest an LL for a ride whose standby is at/under this — a
 *  near-walk-on's slot is always available and always early, so it
 *  would win "earliest return" forever (Spaceship Earth got suggested
 *  and BOOKED on 2026-07-04). Mirrors the poller's LL_MIN_STANDBY_MINS. */
export const LL_SUGGEST_MIN_STANDBY_MINS = 25;

export function pickNextLl(opts: {
  /** Remaining plan rides (not done, not dropped, not the just-done one). */
  rides: { ride_id: string; ride_name: string }[];
  /** ride_id → held LL return ISO. */
  holds: Record<string, string>;
  /** Live park state (getParkRides) — carries each ride's current offer. */
  live: {
    ride_id: string;
    wait_mins: number | null;
    ll: { price?: string; return_start?: string } | null;
  }[];
  now: Date;
}): NextLlSuggestion | null {
  const liveById = new Map(opts.live.map((r) => [r.ride_id, r]));
  let best: NextLlSuggestion | null = null;
  let bestAt = Infinity;
  for (const r of opts.rides) {
    if (opts.holds[r.ride_id]) continue;
    const state = liveById.get(r.ride_id);
    const ret = state?.ll?.return_start;
    if (!ret) continue;
    // Walk-on-short standby → the LL isn't worth a booking slot.
    if (
      state.wait_mins != null &&
      state.wait_mins <= LL_SUGGEST_MIN_STANDBY_MINS
    )
      continue;
    const at = Date.parse(ret);
    if (!Number.isFinite(at) || at < opts.now.getTime()) continue;
    if (at < bestAt) {
      bestAt = at;
      best = {
        ride_id: r.ride_id,
        ride_name: r.ride_name,
        return_start: ret,
        price: state?.ll?.price ?? null,
        standby_mins: state?.wait_mins ?? null,
      };
    }
  }
  return best;
}
