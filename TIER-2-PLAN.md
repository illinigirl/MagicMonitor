# Tier 2 — In-park Plan Execution Surface

A `/plan` page in the Magic Monitor web app that lets a signed-in user
view their active plan, see live disruptions, mark rides done, and
manage Lightning Lane bookings from their phone.

This is the bridge between "screenshot the plan" (tier 1, today) and a
full conversational replan loop in MM (tier 3, future).

**Scope:** scope B — plan execution + manual LL booking/tracking. Does
NOT include earlier-time LL alerts (scope C, deferred — see Future
Phases) or full Disney-app LL purchase sync (out of scope per
PROJECT.md).

**Sequencing note:** MCP-on-Pi work comes BEFORE tier 2 in the build
queue. That unlocks the full conversational replan loop on mobile via
Tailscale (~2 hours of work), which informs how much of tier 2 is
actually needed and which slices to prioritize. See MCP-ON-PI.md (TBD).

## Why this exists

- MCP runs locally; not usable on a phone in the park.
- A screenshot of the plan goes stale the moment ride status changes.
- A live web view can join the saved plan (DDB) to current ride status
  and surface disruptions automatically.
- Plan execution (marking rides done/dropped) is a status-update task,
  not an NLP task — it doesn't need a conversation.
- After this ships, the next time Claude is opened (post-park), the
  plan state is already accurate. No reconciliation conversation.

## Goals

1. View today's active plan on mobile with live ride status overlay.
2. See plan disruptions (rides currently DOWN that are in the plan).
3. Mark rides as completed or dropped during the day.
4. Keep DDB as the single source of truth so MCP and web stay in sync.

## Non-goals (v1)

- Editing the plan itself (adding/reordering rides). Plan negotiation
  stays in Claude — this surface is for *execution*, not planning.
- Conversational replan. That's tier 3.
- Plans for dates other than today. Future flag.
- Outcome recording (the post-trip rating form). Possible v1.1.
- Showing show selections. v1 covers rides only; shows can be added
  once the ride flow is proven.

## Slices

### Slice 1 — Read-only plan view

Ship the page with no mutations. Immediate value: live data instead
of a screenshot.

**Files:**
- `web/src/app/plan/page.tsx` — Server Component, auth-gated like
  `/me`. Reads today's active plan, joins to live ride status.
- `web/src/lib/dynamodb-writes.ts` — add `getActivePlanForUser(sub)`
  helper. Returns:
  - The single plan row matching `USER#{sub}/PLAN#*` where
    `planned_for_date = today (ET)` AND `outcome_recorded = false`.
  - "Most recent" tiebreaker = `planned_at` descending. If multiple
    rows match (user created a revised plan mid-morning), the helper
    returns the latest plus a `multiplePlansToday: true` flag so the
    page can surface a heads-up banner. Silently picking one would
    confuse a user who thought they'd replaced their morning plan.

**Why stale plans can't surface:** the combined filter
(`planned_for_date = today` AND `outcome_recorded = false`) is
sufficient. Yesterday's unrecorded plan would have
`planned_for_date = yesterday` and be filtered out by date alone.
The 24h TTL on unrecorded plans is the belt-and-suspenders cleanup,
not the primary safeguard.

**Rendering:**
- Plan metadata: park, planned_at timestamp, notes
- Table of rides: position, name, predicted_wait_min, current_wait,
  status badge (operating / down / closed)
- Empty state: "No active plan for today. Open Claude to create one."

**Auth contract:** matches `/me` — page calls `auth()`, redirects
unauthenticated users to Cognito hosted UI with `/plan` as callback.

### Slice 2 — Disruption surfacing

Layer disruption highlights on top of slice 1's render.

**Logic:**
- Cross-reference `ride_sequence` against current status.
- Any ride with status `DOWN` (and not in `completed_rides` or
  `dropped_rides`) is flagged.
- Top banner: "N rides in your plan are currently down: X (position
  3 of 8), Y (position 7 of 8)" — includes position so the user has
  context for urgency without v1 needing to be smart about priority.
- Inline row styling: red border + DOWN badge on disrupted rides.
- Banner only renders when N > 0.

**v1 deliberately does NOT prioritize alerts** — a ride DOWN at
position 7 looks the same as position 3. Smart prioritization
(weighting by how soon the user will reach it) is a v1.1 refinement
once we see real in-park usage and know whether the noise is real.

**No backend work.** All data is already fetched in slice 1.

### Slice 3 — Tap-to-complete / tap-to-drop

Add the mutation path using Server Actions (matching the existing
`/me` pattern — not Route Handlers).

**Files:**
- `web/src/app/plan/actions.ts` — new file
  - `completeRide(rideId)` server action
  - `dropRide(rideId)` server action
- `web/src/lib/dynamodb-writes.ts` — add `updatePlanRideStatus(sub,
  planId, rideId, newState)` helper. Uses DDB conditional expression
  to prevent races.

**Auth contract:** server actions call `auth()`, use Cognito sub for
the partition key, never accept a user_id from the client.

**Race safety:** the helper does a read-modify-write with a
`ConditionExpression` requiring `outcome_recorded = false` and the
ride still being in `ride_sequence`. If the condition fails (e.g.,
the page was open in two tabs and one already moved the ride), the
action returns an error and the UI revalidates.

**UI:** small inline buttons next to each ride. After action,
`revalidatePath("/plan")` so the server re-fetches.

**Side benefit of revalidatePath:** the re-fetch includes fresh
live ride status, so tapping "complete" also gets the user updated
wait times for free. This is intentional, not accidental — it means
the page stays current as the user works through the plan without
needing a separate "refresh" button.

### Slice 4 — LL booking + mark-as-used

Add manual Lightning Lane tracking. Matches what the Pi version
already supports and what the user actually does in the park.

**Why a new row type instead of extending PLAN#:** LL bookings are
day-scoped and ride-scoped, but they exist independently of whether
a plan exists. You can have an LL without a plan, or rebook an LL
mid-day without revising the whole plan. Keeping LLs in their own
row type avoids overloading the plan schema.

**Data model (new DDB row type):**
```
PK: USER#{sub}
SK: LL_BOOKING#{booked_for_date}#{ride_id}
booked_for_date: "2026-05-19"
ride_id: "<themeparks ride id>"
ride_name: "TRON Lightcycle Run"
park_key: "mk"
return_start: "2026-05-19T14:30:00-04:00"  # ISO with offset
return_end: "2026-05-19T15:30:00-04:00"
lane_type: "multi_pass"  # multi_pass | single_pass | inclusion
booked_at: "2026-05-19T10:12:00-04:00"
used_at: null  # ISO when user marks as used
notes: null  # optional free text
ttl: now + 48h initially; extends after used_at recorded
```

**Files:**
- `web/src/app/plan/actions.ts` — add server actions:
  - `addLLBooking(rideId, returnStart, returnEnd, laneType)`
  - `markLLUsed(bookingId)`
  - `removeLLBooking(bookingId)`
- `web/src/lib/dynamodb-writes.ts` — helpers:
  - `getActiveLLBookings(sub, dateET)` — returns today's bookings
  - `putLLBooking(...)` / `updateLLBookingUsedAt(...)` /
    `deleteLLBooking(...)`
- `web/src/app/plan/page.tsx` — render LL section above the ride
  list, showing booked LLs with return window, ride name, and an
  "I used this" button per booking.

**UI:** small "Add LL" button at the top of the LL section opens a
modal/inline form. Pre-fills ride options from the active plan's
`ride_sequence`. Return time is a time picker; defaults to current
return window if a `current_ll_offer` exists for that ride in DDB
live data (nice ergonomics — usually the user is booking exactly
the window the app shows).

**Marking used:** sets `used_at` to now and visually moves the
booking into a "used today" collapsed section so it stays out of
the way without disappearing.

### Slice 5 — MCP LL tools

Expose LL bookings to Claude via new MCP tools so the conversational
planner is aware of LL state.

**New MCP tools (in `mcp/server.py`):**
- `get_user_ll_bookings(user_id, date=None)` — read tool, returns
  today's (or specified date's) LL bookings with usage state. Lets
  Claude answer "what LLs do I have today?" and "have I used my
  TRON LL yet?"
- `record_ll_booking(user_id, ride_name, return_start, return_end,
  lane_type)` — write tool, lets Claude record an LL when the user
  tells it conversationally ("I just booked TRON for 2pm"). Same
  DDB row shape as the web action writes.
- `mark_ll_used(user_id, booking_id_or_ride_name)` — write tool.

**Docstring updates** to other tools that should know about LLs:
- `get_planning_context` should include `active_ll_bookings` in its
  payload so Claude reasons about plans-with-LLs holistically.
- The LL recommendation section (5.4 in `get_planning_context`
  docstring) should note that already-booked LLs shouldn't be
  re-recommended.

## Decisions pinned for v1

| Decision | Choice | Rationale |
|---|---|---|
| Mutation pattern | Server Actions | Matches existing `/me` pattern. Works without JS, atomic feel. |
| Date scope | Today only | Smallest useful slice. Tomorrow's plans rare today. |
| Show selections in v1 | Out | Add once ride flow is proven. |
| Outcome recording | Out (v1.1) | Significant UI; not blocking in-park use. |
| Race safety | DDB ConditionExpression | Cheap, idiomatic; matches MCP's own write pattern. |
| Plan-to-poller interaction | No change | Poller already reads `ride_sequence`; once a ride moves to `completed_rides`, it stops triggering alerts. ✓ desired behavior. |

## Prerequisites

### Before slice 1
1. **IAM scope on Amplify SSR role:** verify the role can read
   `USER#*/PLAN#*` rows. If not, CDK change needed (likely tiny —
   the existing policy probably uses key-prefix `USER#*` which would
   already cover it). Do this first; if a CDK change is required it
   has a separate deploy cycle.

### Before slice 3
2. **MCP docstring update:** update the docstrings on `record_plan`,
   `add_ride_to_plan`, and `remove_ride_from_plan` in `mcp/server.py`
   to note that the web app is also a writer of `completed_rides` /
   `dropped_rides`. This is a prereq, not a follow-up — Claude needs
   to know about the peer mutator *before* slice 3 ships, so during
   testing we don't confuse ourselves about who wrote what when
   debugging.

## Effort estimate

- Slice 1 (read-only plan view): 1–2 evenings
- Slice 2 (disruption surfacing): 1 evening
- Slice 3 (tap-to-complete/drop): 1–2 evenings
- Slice 4 (LL booking + mark-as-used): 2 evenings
- Slice 5 (MCP LL tools): 1 evening
- **Total: ~6–8 evenings for slices 1–5 (scope B)**

Outcome-handoff slice (previously slice 4 in scope A): 1 evening,
deferred to v1.1 — not blocking in-park use.

## What this changes about the MM story

The "alerts prompt you to replan" loop goes from purely aspirational
(verified at home with screenshots) to ~80% closed:

- Real-time alerts: shipped
- Plan stored and live-rendered: tier 2 slice 1
- Disruptions surfaced in plan view: tier 2 slice 2
- Plan execution decoupled from Claude: tier 2 slice 3
- Conversational replan handoff: tier 3 (still roadmap)

Status, plainly: tier 2 means in-park use of MM is real, not
aspirational. The remaining gap is the conversational replan — a
fair "next milestone."

## Future phases (deferred from tier 2 scope)

These are explicitly deferred but worth capturing so the design intent
isn't lost. Each can become its own phase when prioritized.

### Scope C — Earlier-time LL alerts
The Pi version has `_check_earlier_ll()` in `disney/monitor.py:515-583`
which compares current LL offers against booked return times and
alerts the user when a better window opens up (more accurate label
than "drop pattern" — this is a real personalized alert based on
user state).

Implementation in AWS:
- New poller path that reads `USER#*/LL_BOOKING#*` rows where
  `used_at` is null
- For each booking, compare `current_ll_offer.return_start` (already
  in live data) against the booked `return_start`
- If current is earlier than booked by some threshold (e.g., > 20
  min), fire Pushover alert
- Per-(user, booking) cooldown to prevent repeat alerts within an
  hour of each other

Effort: ~2 evenings. Sits alongside the existing DOWN/UP/STILL_DOWN
alert paths in `infra/lambda/poller/index.py`.

### MagicBand NFC tap → complete-next-ride
- NFC Personal Automation on iPhone reads the band, triggers a
  Shortcut, Shortcut calls a Route Handler on MM web with a bearer
  token, handler marks the next-uncompleted-undropped ride done.
- Requires new auth surface: per-user MCP token row, generation UI
  on `/me`, validation in the Route Handler.
- Effort: ~1-2 evenings.
- Strong demo story but personal-only utility.

### Festival booth tracking
- For EPCOT festivals (Arts, Flower & Garden, Food & Wine, Holidays)
- New row types:
  - `FESTIVAL#{festival_id}/BOOTH#{booth_id}` — canonical booth list
  - `USER#{sub}/BOOTH#{festival_id}#{booth_id}` — per-user state
    (want_to_try, completed_at, rating, notes, per_item_notes)
- New page: `/festivals/[festival_id]` with three-state filter
- Booth list maintenance: ideally Claude-curated via new MCP tool
  `refresh_festival_booth_list(festival_name)` that uses web search
  to scrape Disney's current festival page (~5 min/festival, 4×/yr
  vs hand-maintaining JSON quarterly)
- Effort: ~3-4 evenings including the Claude-curation MCP tool

### Multi-trip schema
- Pi version uses a trip-centric model: one trip spans multiple
  days, each day has park + ll + priorities + dining
- AWS version uses snapshot-style: each `PLAN#{iso_ts}` is one plan
- Hybrid future: `USER#{sub}/TRIP#{trip_id}` parent row pointing at
  multiple `PLAN#*` children
- Significant schema redesign — only worth doing if multi-day
  planning becomes a real use case
- Effort: ~1 week including migration

### Outcome-handoff form (v1.1)
- `/plan` page gets a "Day done — log outcomes" button
- Opens a form: per-ride actual_wait_min, aggression_rating,
  timing_rating, free_text
- Submit calls a server action that writes the same fields the MCP's
  `record_plan_outcome()` already writes
- Effort: 1 evening
