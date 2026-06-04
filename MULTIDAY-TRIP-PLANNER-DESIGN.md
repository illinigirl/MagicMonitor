# Multi-Day Shared Trip Planner — Design Doc (DRAFT for review)

**Status:** draft for Megan's sign-off, 2026-06-01. No code written yet.
**Local working doc — not committed.** (Same convention as `TIER-2-PLAN.md`.)

This is the spec for the feature we designed in conversation: a
multi-day, shared, future-dated trip planner that supersedes the
original "port the same-day write tools" plan. React to this; once
it's signed off I build against it.

---

## 1. Goal

Let the family build a whole trip ahead of time (June 23 = MK 10am–close
+ ride list, June 24 = EPCOT, …), then on each day pull up that day's
plan, have Claude **re-evaluate it against live conditions**, accept the
adjusted version, and get live disruption alerts for the rest of the day.
One shared trip everyone can see and edit.

Non-goal (for v1): concurrent-edit conflict resolution, multiple
simultaneous *separate* trips per person (shared-by-default; personal
trips are a possible later tag).

## 2. Current state (why we can't just port)

- `record_plan` hardcodes `planned_for_date = today`; no future-date param.
- Pending plans have a **24h TTL** → a future plan auto-deletes before the trip.
- The poller fires disruption alerts on
  `planned_for_date = today AND outcome_recorded = false AND begins_with(SK,"PLAN#")`.
  The model is "one active plan, for the day you're in the park."
- **No** TRIP#/itinerary/multi-day persistence exists anywhere (verified
  across MCP, poller, web).

The good news: because the poller already filters on `planned_for_date =
today`, future-dated rows are **naturally ignored** until their day. So
multi-day storage doesn't break the poller — we only add an activation
gate (§6).

## 3. Data model

**Recommendation: extend the existing `PLAN#` rows, don't invent a new
container.** Keeps back-compat and reuses the calibration machinery.

### 3.1 Day-plan row (the unit of work)

```
PK = USER#megan                      # shared partition (see §7)
SK = PLAN#<recorded_iso_ts>          # UNCHANGED from today
```

- **SK stays `PLAN#<recorded_ts>` (refined 2026-06-01 during build).**
  The day a plan is *for* lives in the `planned_for_date` field, not the
  SK. A user has only a handful of plans, so `get_plan_for_day` is a
  single-partition Query (`PK=USER#megan, begins_with(SK,"PLAN#")`) +
  in-code filter on `planned_for_date` — just as cheap at this scale, and
  it means zero SK migration / full back-compat. The earlier
  `PLAN#<date>#<ts>` idea was a micro-optimization with no payoff here.

New/changed attributes:

| Field | Meaning |
|---|---|
| `planned_for_date` | **Now settable** (the trip day), not forced to today. |
| `trip_id` | Groups day-plans into one trip. e.g. `2026-06-trip` or a minted id. |
| `active` | **bool.** Gates poller alerts (§6). Dormant=false, activated=true. |
| `activated_at` | iso_ts when the user activated (audit / "live since"). |
| `created_by` | friendly id from the Cognito sub (`megan`/`jim`/`michele`) — attribution. |
| `plan_window` | optional `{open, close}` for the day (10am–close). |
| `ttl` | **Now date-based** (§5), not always now+24h. |
| *(unchanged)* | `ride_sequence`, `completed_rides`, `dropped_rides`, `show_selections`, `context`, `notes`, `outcome_recorded`. |

### 3.2 Trip header row (DECIDED: included)

```
PK = USER#megan
SK = TRIP#<trip_id>
{ name, start_date, end_date, days: [ {date, park}, ... ], created_by, ttl }
```

The "trip overview" object — powers `get_upcoming_trip()` and gives the
trip a name. TTL = `end_date + a few days`.

## 4. Tool surface

**Changed:**
- `record_plan` → gains `planned_for_date` (defaults today), `trip_id`
  (optional). Same-day record sets `active=true` (today's flow unchanged);
  future record sets `active=false` (dormant). **Drops the client
  `user_id` param** — HTTP derives identity from the token (§7).
- `mark_ride_complete` / `add_ride_to_plan` / `remove_ride_from_plan` /
  `record_plan_outcome` → select the target day-plan by `planned_for_date`
  (default = today's active plan) instead of "newest PLAN# row."

**New:**
- `create_trip(name, days=[{date, park}])` → **(DECIDED: dedicated tool)**
  mints `trip_id`, writes the TRIP# header + a dormant day-plan per day in
  one call. The ride lists per day can be filled in by subsequent
  `record_plan(planned_for_date=…, trip_id=…)` calls as Claude builds each
  day, or passed up front. Internally it's `record_plan` per day sharing
  the `trip_id`.
- `get_trip(trip_id?)` / `get_upcoming_trip()` → the trip overview + its
  day-plans (dormant ones included).
- `get_plan_for_day(date)` → pull one day's plan.
- `activate_plan(date=today)` → the activation + re-eval entry point (§6).

**Unchanged:** `get_user_plan_history` (history + calibration), now
returns multi-day rows too.

## 5. Lifecycle & TTL

States: **dormant → active → recorded.**

- **dormant** (future, `active=false`, `outcome_recorded=false`):
  TTL = `planned_for_date + 2 days` (survives until just after the trip
  day; auto-cleans if never activated/outcomed).
- **active** (`active=true`): set on the day via `activate_plan` (or
  auto on same-day `record_plan`). Poller watches it. TTL bumped to keep
  it alive through the day.
- **recorded** (`outcome_recorded=true`): TTL = 365d (calibration history).

## 6. Activation + live re-evaluation (the core UX)

```
You:    "What's my plan today?"
Claude: get_plan_for_day(today)  +  get_planning_context(park)   ← read-only, NO alerts yet
        "Here's your MK plan reconciled with live conditions:
         Space Mountain's down (~40min typical recovery) so I moved it
         to mid-morning; today's ~15% above forecast so I trimmed Pirates.
         Lock it in and start watching for disruptions?"
You:    "Yes"
Claude: activate_plan(today)  → writes the adjusted ride_sequence,
        sets active=true, activated_at=now  → ALERTS BEGIN.
...during the day: mark_ride_complete, live disruption pings on remaining rides...
You:    "We're done, that worked great"
Claude: record_plan_outcome  → active=false, outcome_recorded=true → calibration.
```

**Hard dependency:** the re-evaluation IS `get_planning_context`, which is
**not yet on HTTP/mobile** (it was "session 4"). So this feature requires
porting `get_planning_context` to `server_http.py` too. The deliverable
bundles them (≈ the M5 trip-planner).

**Activation = 2 prompts (decided):** view+re-eval is read-only; "yes"
accepts the adjusted plan and flips alerts on. Avoids pinging you when you
check the plan from the hotel at 7am.

**Plan-window-respecting alerts (DECIDED).** Disruption alerts fire only
while "now" (ET) is inside the day's `plan_window`. At activation, the
re-eval resolves the window to concrete times (e.g. "10am–close" → the
park's actual close from `get_planning_context`'s hours) and stores
`{open, close}` on the plan. The poller's alert fanout checks now ∈
[open, close] before firing — so no pings before you arrive or after you
leave. (This is a runtime check in the alert path, not the DDB filter.)

## 7. Identity & sharing

- **Shared partition:** all writes → `USER#megan` (reuse; zero migration;
  stdio already defaults there). Everyone sees/edits one trip.
- **Identity = attribution, not routing:** middleware stores the verified
  Cognito `sub` in a `ContextVar` (confirmed to propagate to tools); tools
  stamp `created_by = friendly_id(sub)`. Map sub→friendly id (megan/jim/
  michele); unmapped-but-allowlisted sub → fail loud.
- **`user_id` is never a client-supplied tool param** (security: a crafted
  call mustn't write as someone else). Deliberate divergence from stdio.
- **Write IAM:** mirror the web SSR exactly — `PutItem/UpdateItem/DeleteItem`
  with `ForAllValues:StringLike` on `LeadingKeys: ["USER#*","PARK#*"]`
  (defense-in-depth; per-user isolation is enforced in code, not IAM).

## 8. Back-compat with legacy today-plan rows

- Legacy rows: `SK = PLAN#<recorded_ts>`, no `active`/`trip_id` fields.
- New rows: `SK = PLAN#<date>#<recorded_ts>`, with `active`.
- **Poller filter becomes:**
  `planned_for_date = today AND outcome_recorded = false
   AND begins_with(SK,"PLAN#") AND begins_with(PK,"USER#")
   AND (attribute_not_exists(active) OR active = true)`
  → legacy rows (no `active` attr) still fire alerts; new dormant rows
  (`active=false`) don't; activated rows do. The `attribute_not_exists`
  clause is transitional (legacy rows TTL out).
- Read tools that match plans by `planned_for_date` handle both SK shapes
  (they read the field, not the SK).

## 9. stdio vs HTTP

- Build in **both** `server.py` (stdio = source of truth) and
  `server_http.py` (mobile). Duplicate-first still holds.
- Port `get_planning_context` to HTTP (§6 dependency).
- Touches `server.py` planner docstrings → **re-run the eval suite**
  (CLAUDE.md guardrail).

## 10. Eval cases (new behavioral coverage)

- Build a multi-day trip → correct dormant rows per day, no alerts.
- "What's my plan today?" on the trip day → re-eval pulls live context,
  proposes adjustments, does NOT activate without confirmation.
- Activation → ride_sequence reflects the accepted adjustments, active=true.
- A ride DOWN at activation → re-eval reorders rather than ignoring it.
- Outcome recording → deactivates + feeds calibration.

## 11. Resolved decisions (Megan, 2026-06-01)

1. **Trip header row — INCLUDE** the `TRIP#<trip_id>` overview object (§3.2).
2. **Trip creation — DEDICATED** `create_trip(days=[…])` tool (§4).
3. **Plan window — RESPECT** it: alerts only fire inside the day's
   `plan_window`, resolved to concrete times at activation (§6).
4. **Same-day — AUTO-ACTIVATE** on record (today's UX unchanged); future
   plans are dormant until `activate_plan` (§4, §5).

## 12. Phasing & effort (~1.5–3 focused days)

1. **Schema + write tools** (date-aware `record_plan`, trip grouping,
   `created_by`, drop client `user_id`) — ~½–1 day.
2. **Activation + `get_planning_context` HTTP port** (the re-eval engine)
   — ~½–1 day. *Biggest single piece.*
3. **Poller `active` gate + TTL change** — ~1–2 hrs.
4. **Agentic instructions + eval cases** — ~½ day. *Least predictable.*
5. **Tests, deploy (`DisneyMcpStack` + poller), mobile verify** — ~2–3 hrs.

No new AWS infra; same write-IAM shape; poller change is small. The cost
is design care (schema back-compat) + getting the agentic build→persist→
resume→re-eval flow reliable.
```
