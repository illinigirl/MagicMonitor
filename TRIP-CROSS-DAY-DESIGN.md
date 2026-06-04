# Design: cross-day awareness — treat a trip as one entity

**Status:** proposed (Megan's idea, 2026-06-04). Local working doc — NOT
committed (same convention as the other design docs). A planner-behavior
enhancement; distinct from trip visibility (`TRIP-VISIBILITY-DESIGN.md`)
and trip alerts (`TRIP-ALERTS-DESIGN.md`).

## Idea

When a trip visits a park more than once (e.g. two MK days), the planner
should reason across those days as a **set**, not plan each in isolation.
So a second MK day *complements* the first — it doesn't re-suggest the
same headliners you already did/planned — unless the user explicitly
wants to re-ride.

## Two modes (the data differs)

- **Building ahead (both days still dormant):** only *intended* ride
  lists exist. **Distribute** the park's must-dos across the repeat days
  rather than duplicating — TRON + Space day 1, Seven Dwarfs + Splash
  day 2. A spread, not a copy.
- **Day-of, the later day:** the earlier same-park day has happened, so
  its `completed_rides` is ground truth. Plan the later day around
  **what's left + what got missed** (the earlier day's `dropped_rides` /
  not-completed), not what was already ridden.
- **Repeat only on request:** re-riding a favorite is valid — "let's do
  TRON again on day 2" overrides the dedup. Default = don't repeat.
- **Repeat-park only:** an MK day and an EPCOT day share no rides, so
  this only fires when ≥2 days are the same park within a trip.

## What it takes — mostly agentic, not data

- **Data already supports it.** Days are grouped by `trip_id`; each
  `PLAN#` carries `ride_sequence` + `completed_rides` + `dropped_rides` +
  `park_key`. "What did the trip's other MK day cover" is fully derivable.
- **The gap is instruction.** Teach the planner (a new bullet in
  `get_planning_context` §0d) to, when planning a day: look up the trip's
  *other same-park days* and treat their rides as already-covered — skip
  by default; when building ahead, spread the must-dos; day-of, account
  for actuals; honor explicit "ride X again."
- **Give Claude the cross-day ride lists.** Today `get_upcoming_trip`
  returns per-day ride *counts*, not the lists, so Claude would call
  `get_plan_for_day` for the sibling day(s). Optional cleaner path: a
  derived **`get_trip_ride_coverage(trip_id)`** tool → per-park set of
  rides planned/completed across the whole trip, so Claude gets a clean
  "already covered" set in one call instead of N per-day reads.
- **Eval case.** Two MK days, day-1 plan = [TRON, Space, Seven Dwarfs];
  planning day 2 → assert day-2 `ride_sequence` does NOT just duplicate
  those (covers different/remaining rides), AND a variant where the
  prompt says "ride TRON again" → TRON IS allowed back. Locks the
  behavior + the override.

## Open questions
1. Derived `get_trip_ride_coverage` tool vs. Claude composing it from
   per-day `get_plan_for_day` reads? (Tool = reliable + one call;
   per-day = no new surface but more agentic legwork.)
2. Build-ahead "spread": does the planner auto-distribute, or propose a
   split and let the user adjust? (Lean: propose, since which day gets
   which headliner is preference-driven.)
3. Interaction with calibration: completed_rides already feeds
   calibration; cross-day dedup just *reads* it — no conflict, but worth
   confirming the day-of flow pulls the sibling day's outcomes.
