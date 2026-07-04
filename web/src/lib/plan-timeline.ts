/**
 * One time-ordered day timeline: the plan's rides (whose ORDER is
 * authoritative — plan_order / sequence position, not their suggested
 * times) with meals and shows slotted in between by time.
 *
 * Merge rule: each meal/show is inserted before the first ride whose
 * target_time is LATER than it; rides without a target_time can't be
 * compared and never attract insertions. Extras that fit nowhere (all
 * rides earlier or un-timed) go at the end, still time-sorted. This
 * keeps the ride backbone exactly as planned while the 12:30 lunch
 * lands between the 11:25 ride and the 12:45 one — what a human means
 * by "the day's schedule."
 */

export interface TimelineRide {
  ride_name: string;
  ride_id?: string;
  target_time?: string | null;
  done?: boolean;
  held_ll?: string | null;
}

export type TimelineEntry<R extends TimelineRide> =
  | { kind: "ride"; ride: R }
  | {
      kind: "meal" | "show";
      name: string;
      time: string;
      /** Booked commitment (reservation / show time) vs a suggested
       *  quick-service stop the plan merely recommends. */
      booked: boolean;
    };

/** Suggested-not-booked meal types (record_plan `type` values). */
const SUGGESTED_MEAL_TYPES = new Set(["quick-service", "qs", "suggested"]);

export function buildDayTimeline<R extends TimelineRide>(
  rides: R[],
  reservations: { name: string; time: string; type?: string }[],
  shows: { name: string; start: string }[],
): TimelineEntry<R>[] {
  const out: TimelineEntry<R>[] = rides.map((ride) => ({
    kind: "ride" as const,
    ride,
  }));
  const extras: Extract<TimelineEntry<R>, { kind: "meal" | "show" }>[] = [
    ...reservations.map((r) => ({
      kind: "meal" as const,
      name: r.name,
      time: r.time,
      booked: !SUGGESTED_MEAL_TYPES.has((r.type ?? "").toLowerCase()),
    })),
    ...shows.map((s) => ({
      kind: "show" as const,
      name: s.name,
      time: s.start,
      booked: true,
    })),
  ].sort((a, b) => (a.time < b.time ? -1 : 1));

  // Ascending-time insertion keeps same-slot extras in time order: an
  // earlier extra sits before the ride entry the next one splices at.
  for (const extra of extras) {
    const idx = out.findIndex(
      (e) =>
        e.kind === "ride" &&
        e.ride.target_time != null &&
        e.ride.target_time > extra.time,
    );
    if (idx === -1) out.push(extra);
    else out.splice(idx, 0, extra);
  }
  return out;
}
