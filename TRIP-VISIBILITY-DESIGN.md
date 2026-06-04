# Design: trip visibility — dashboard glance + calendar projection

**Status:** proposed, from a design discussion with Megan 2026-06-03.
Local working doc — NOT committed (same convention as
`MULTIDAY-TRIP-PLANNER-DESIGN.md`, `ACTIVE-PLAN-GSI-DESIGN.md`,
`TIER-2-PLAN.md`).

**Candidate roadmap slot:** a follow-on to M5 (the multi-day trip
planner, now shipped). Call it "trip visibility" pending the PROJECT.md
roadmap reconciliation.

---

## Motivation

M5 put multi-day trips in the data plane (`TRIP#` + `PLAN#` rows under
`USER#megan`) and made them reachable by *asking Claude*
(`get_upcoming_trip`, `get_plan_for_day`). What's missing is a way to
**see** an upcoming trip without a conversation — at a glance, and on
the phone where the family already looks. Megan wants both a dashboard
view and a Google Calendar projection.

Nothing here needs new data plumbing: the trip data already exists in
DDB, the dashboard SSR already reads DDB, and the Google Calendar MCP is
already connected in Megan's Claude session. This is about *projecting*
existing data onto two surfaces.

## The two surfaces (complementary, not either/or)

- **Dashboard = the rich glance.** Plan + MM's live waits side by side —
  the one thing only MM can show.
- **Calendar = the ambient glance.** On the phone, with optional
  reminders, shareable to the non-Claude family members (sister,
  husband) via normal calendar sharing.

Decision: **build both, but the cheap halves first** — a read-only
dashboard page + a Claude-driven calendar projection. The heavy
server-side calendar sync (below) is held in reserve.

---

## Surface 1 — Dashboard "My Trips" page

**Shape:** a Next.js route (proposed `/trips`) that SSR-reads the shared
trip and renders it read-only.

- **Data:** new helper in `web/src/lib/dynamodb.ts` — query
  `PK = USER#megan`, `begins_with(SK, "TRIP#")` for headers with
  `end_date >= today`, plus the matching `PLAN#` rows for per-day detail.
  This mirrors what the MCP `get_upcoming_trip` already does.
  - **Pagination is mandatory** (project failure-mode discipline — see
    CLAUDE.md / the 2026-05-24 getParkRides regression): paginate via
    `LastEvaluatedKey`. It's one small partition today, but never ship a
    single-page read of a partition that grows.
- **Renders per trip:** name, date range, and per day: park, ride list,
  ride count, dormant/active status.
- **Trip-day superpower (v1 stretch or v2):** when a day is *today* /
  active, show its planned rides **with live wait + status** — reuse the
  same STATE-row live data the `/parks/<park>` pages already render
  (and the `park_key-SK-index` GSI). This is the dashboard's reason to
  exist over the calendar.
- **Auth + sharing:** gated behind the existing Cognito/NextAuth login.
  Trips are shared family data (`USER#megan`), so any allowlisted
  logged-in member sees the shared trip — intended for the family model.
- **Read-only in v1.** Editing stays in the MCP planner (it owns the
  shared-partition writes + `created_by` attribution). A dashboard edit
  path would duplicate that and is out of scope for v1.
- **Empty state:** "No upcoming trips — ask Claude to build one."

**Cost:** moderate, no new external integration or secrets — reuses
`dynamodb.ts` patterns + existing auth. Always-current by construction
(SSR reads DDB live; nothing to sync, nothing to drift).

---

## Surface 2 — Calendar projection via Claude (the connected MCP)

**Mechanism (v1):** when Claude builds/activates a trip — or on request
("put my trip on my calendar") — it reads the trip
(`get_upcoming_trip` / `get_plan_for_day`, MM MCP) and writes events via
the **Google Calendar MCP already in the session** (`create_event`,
`list_events`). This is client-side: Megan's Google auth, her Claude
client. No backend, no secrets.

**Event shape (this is where the reminders question resolves):**

The trip plan has two kinds of items, and reality-drift only affects one
of them — so reminders are *not* all-or-nothing:

- **The loose ride sequence → all-day blocks, NO reminders.** One
  all-day event per trip day, e.g. "🏰 Magic Kingdom — Disney trip",
  with the planned ride list + notes in the description. All-day = zero
  time-pressure, which sidesteps the "our reality never matches the
  schedule" problem entirely. This alone is the phone glance, and it's
  shareable. *The "your plan just broke" signal is NOT the calendar's
  job — that's already MM's Pushover disruption alerts (the poller).*
- **The hard, clock-pinned slots → timed events, reminders worth it.**
  Only for items that don't drift and cost you if missed:
    - dining reservations (fixed time, lose-it-if-late)
    - Lightning Lane / ILL return windows, virtual-queue boarding groups
      (hard ~1h windows)
    - show times you selected (fixed performances)
    - rope drop / park open
  Reminders here are **opt-in per event**, decided in the moment — the
  only place they earn their keep.
- **Never** per-ride reminders.

**Reminder policy in one line:** off on the day-blocks, optional only on
the clock-pinned slots; MM's existing alerts carry the divergence load.

**Idempotency (the fragile part of the Claude-driven approach).** If
Claude syncs twice (build → re-activate → "add to calendar" again) it
could duplicate events. v1 mitigations, in order of preference:
  1. Claude `list_events` for the trip date range and skip/update
     matches before creating.
  2. Tag MM-created events (title prefix like "[MM]" or a Calendar
     extended property) so a re-sync can find + update instead of
     duplicate.
This is best-effort under the client-driven model; the robust version is
the deferred server-side sync.

**Calendar choice:** propose a dedicated **"Disney Trips" calendar**
(shareable to the family, deletable, doesn't clutter the primary)
rather than writing to the primary calendar.

---

## What data exists vs needs structuring

- **Structured today** (usable immediately): `TRIP#` (name, dates,
  per-day park), `PLAN#` (`ride_sequence`, `show_selections` *with*
  `performance_start`, `plan_window`, `notes`, `active`). So **all-day
  day blocks + timed show events work right now, zero schema change.**
- **Free-text only:** dining reservations + LL/ILL windows live in
  `notes`. To make those reliable *timed* events, either Claude parses
  `notes` best-effort (v1), or add a small structured field later (e.g.
  `fixed_slots: [{kind, name, time}]` on the plan). v1 = shows-structured
  + notes-best-effort.

---

## Deferred — Surface 3: server-side calendar sync

A backend path (on `create_trip`/`activate_plan`, or scheduled) that
writes/updates Google Calendar via the API, independent of Claude.

- **Buys:** always-current mirror, no human/Claude trigger, full
  edit/delete reconciliation.
- **Costs (why deferred):** Google API credentials in the backend (stored
  OAuth refresh token + SSM secret, or a service account — awkward for a
  personal Gmail), a sync trigger, idempotency via a stored
  `PLAN#/TRIP# → eventId` map in DDB, and update/delete handling. Real
  integration + secret management.
- **Build only if** the Claude-driven version proves insufficient (trip
  edits not reaching the calendar; wanting auto-sync without asking).

---

## Sequencing

1. **Dashboard "My Trips" page** (read-only; live-waits-on-trip-day as a
   stretch). Highest fit, no new secrets, always-current.
2. **Calendar-via-Claude** — agentic behavior + the event shape above
   (all-day blocks + timed shows; opt-in reminders; idempotency tagging).
3. **(Deferred) server-side sync** — only if #2 isn't enough.

## Resolved decisions (2026-06-03, with Megan)

1. **Dashboard route** — standalone **`/trips`** page (linked from `/me`
   + nav), not a panel folded into `/me`. Room to grow.
2. **Live waits on the trip-day view** — **v2.** Ship the read-only plan
   view first (days + rides + dormant/active); add the plan-rides ×
   live-STATE join as a fast follow.
3. **Calendar** — a **dedicated "Disney Trips" calendar** (shareable to
   family, deletable), not the primary calendar.
4. **Idempotency** — **tag MM events from day one** (extended property /
   title marker) so a re-sync finds + updates instead of duplicating.
5. **Reminders** — **off** on the all-day day-blocks; **opt-in** only on
   clock-pinned slots: the day itself, dining reservations, LL/ILL return
   windows, virtual-queue boarding groups, selected show times, rope drop.
6. **Dining/LL data** — **notes-best-effort for v1.** Shows are already
   structured (`performance_start`); parse dining/LL from free-text
   `notes` for now; add a structured `fixed_slots` field only if needed.

## Build units (independent; either can go first)

- **A — Dashboard `/trips` (read-only, v2 scope).** New Next.js route +
  a `getUpcomingTrips`/trip-read helper in `web/src/lib/dynamodb.ts`
  (paginate; reads the shared `USER#megan` `TRIP#`/`PLAN#` rows; the day
  list derives from `PLAN#` rows, matching the MCP `get_upcoming_trip`
  (Y) model). Cognito-gated. No new secrets. Live-waits join is the
  follow-on.
- **B — Calendar projection via Claude.** Agentic behavior + event shape
  (all-day day-blocks tagged with an MM marker; timed events for shows
  now / dining+LL best-effort from notes; opt-in reminders on the
  clock-pinned ones; dedicated "Disney Trips" calendar). Rides on the
  Calendar MCP already in the session — no backend.

## Follow-on C — show fixed-time commitments WITH their times (added 2026-06-04)

`/trips` v1 (shipped) renders the ride sequence with **no clock times** —
correct, because a future ride order's timing depends on live conditions
(crowds, hours, start time, pace) you can't know ahead; times only become
real at day-of `activate_plan`. But the genuinely **clock-pinned**
commitments — show times, dining reservations, LL/ILL return windows,
virtual-queue boarding groups, rope drop — have real times regardless of
crowds, and *should* surface with times even on a future day. Currently
none are shown on `/trips` (it only renders ride names).

Design:
- **Distinguish two states.** Far-future trips usually have these as
  **intentions to book** ("plan a ~6pm dinner", "Fantasmic if running"),
  not committed times — dining isn't bookable until ~60 days out and
  showtimes aren't published ahead. Near-term/day-of, they become
  **confirmed** with actual times. Render accordingly: "to book" vs a
  real time. Don't fabricate reservations for far-out trips (plausible-
  but-wrong); the planner should record intentions, not fake bookings.
- **Shows first (cheap).** `show_selections` is already structured
  (`performance_start`) — display those with times on `/trips` now.
- **Dining / LL need structure.** They live in free-text `notes` today.
  To show them reliably as timed items, add a small `fixed_slots`
  field (`[{kind, name, time, status: intended|booked}]`) on the plan +
  write-tool support — this is the Q6 `fixed_slots` we deferred, now
  justified. Until then, notes-best-effort.
- These same fixed slots are exactly what become **timed calendar
  events** (Build unit B) and the **opt-in reminder** targets — one
  concept, three surfaces (dashboard / calendar / alerts).

Sequencing: display `show_selections` with times on `/trips` (small);
then the `fixed_slots` field for dining/LL when it's worth the schema add.
