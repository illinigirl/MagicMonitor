# Magic Monitor

A serverless live-status dashboard for Walt Disney World, built as a
sibling project to Watchtower (shared visual lineage + Cognito user
pool).

Live at: `magicmonitor.megillini.dev` (after M2-B deploy)

## What it does

Polls themeparks.wiki every 2 minutes for live wait times and ride
status across the four WDW theme parks. Detects status transitions
(operating → down, down → operating, still down >45 min) and fans
out Pushover notifications to subscribers who have opted in to that
park.

A web dashboard at `magicmonitor.megillini.dev` shows live ride status,
park hours (including Early Entry and Extended Evening Hours for
deluxe / DVC guests), and per-user trip planning. Subscribers manage
their own preferences through the dashboard: which parks to watch,
which rides matter, their Pushover key for alerts.

## Audience

Real users — the family uses this on actual Disney trips. Megan +
her husband + her sister all subscribe and get alerted when their
planned rides go down. The project is also public on GitHub, so
architecture, deployment, and code quality matter alongside the
working product.

## Tech stack (high level)

| Layer | Choice | One-line rationale |
|---|---|---|
| Compute | AWS Lambda (Python 3.12) | Cheap at low traffic; matches Watchtower's tier |
| Schedule | EventBridge | Native AWS cron, no separate service needed |
| Storage | DynamoDB single-table | Serverless, free at this scale, fits the project's narrow access patterns |
| Notifications | Pushover (HTTPS API from Lambda) | Family already uses it; ~$0/mo recurring |
| Frontend | Next.js 16 + Tailwind 4 + React 19 | Modern SSR stack; Server Components let us read DynamoDB directly without a separate API tier |
| Type stack | Fraunces + Inter + JetBrains Mono | Mirrors Watchtower; shared visual lineage |
| Color palette | Castle: deep navy + pink + gold (OKLCH) | Disney-feeling without using any actual Disney TM |
| Auth | Amazon Cognito + Google federation | Reuses Watchtower's user pool via second app client (no new Google OAuth setup) |
| Read API | None — Server Components query DynamoDB directly | Read path is render-on-navigation; APIGW + FastAPI would add a hop with no benefit at this scale |
| Write API (M3+) | Next.js Route Handlers in same app | Small data plane (toggles, favorites) — TS end-to-end, NextAuth session in-handler, one fewer Lambda than the FastAPI approach |
| IaC | AWS CDK (TypeScript) | Reuses Watchtower's Python Lambda bundling helper for the poller |
| Hosting | AWS Amplify | SSR Next.js with custom domain; SSR compute role gets scoped DynamoDB access |
| Observability | CloudWatch + X-Ray | Native AWS, native to Lambda + Amplify |

## Out of scope

- **Lightning Lane purchase tracking.** Requires manually syncing your
  actual LL purchases from the Disney app — no public API.
- **PDF trip-itinerary import.** Personal-use feature on the Pi version
  that doesn't generalize.
- **Food / dining / menus.** Hand-curated JSON in the Pi version; not
  generalizable, and adds maintenance burden.
- **SMS notifications.** Investigated and rejected: 10DLC compliance
  is $130/yr + 2-week vetting + EIN requirement, all for a feature
  that adds zero architectural depth over Pushover.
- **Telegram bot.** The Pi version has one but the family no longer
  uses it. Pushover is what people actually open on their phones.

## Non-goals

- **Not a Touring Plans clone.** No crowd predictions, no community
  ratings, no proprietary recommendations.
- **Not multi-region.** Single region (us-east-2). Latency on a
  2-minute poll cadence doesn't matter.
- **Not high-availability beyond AWS defaults.** Lambda cold-start
  storms or 5-minute outages are acceptable.

## Constraints

- **Budget**: <$5/mo recurring. DynamoDB + Lambda + Amplify all in
  or near free tier at this scale.
- **Time**: 5-10 hours/week, no fixed deadline.
- **Quality bar**: stands on its own as a working product — TLS,
  custom domain, Google sign-in, deploy hygiene, README with cost
  breakdown and architecture diagram. Shares visual lineage and the
  Cognito user pool with Watchtower so they reinforce each other if
  both ship, but Magic Monitor must be usable independently.

## Definition of done

Magic Monitor is "done" enough to put on a resume when:

1. Live URL anyone can sign up for (`magicmonitor.megillini.dev`),
   with TLS + Google sign-in.
2. Per-user park toggles + favorite-rides selection working
   end-to-end.
3. Pushover alerts firing reliably on real ride events, gated by
   park hours so no closing-time noise.
4. README with architecture diagram, deploy steps, cost breakdown,
   and screenshots.
5. CI/CD via GitHub Actions, mirroring Watchtower's setup.
6. At least 30 days of polling history visible in DynamoDB so the
   M6 analytics page has real data to render.

Each milestone ships something demo-able; even partial completion
(through M3) is showable.

## Roadmap

### Done

#### 2026-05-24 — Production pagination regression caught + three-layer defense documented

Caught a real production regression in `getParkRides` while
testing an unrelated feature against the live site. The single-page
DDB Scan with FilterExpression had silently been returning empty
arrays for ~7 days — the table had grown past one Scan page
(~1MB / ~1000 items) once M6-B Phase 1 (shipped 2026-05-17) started
accumulating WAIT# rows on every poll. The first scan page no
longer contained any STATE rows, so all four park pages rendered
"0 attractions" instead of live ride data.

**What shipped:**
- `web/src/lib/dynamodb.ts` `getParkRides()` — paginates via
  `ExclusiveStartKey` / `LastEvaluatedKey` until exhausted.
  Immediate fix that unblocked production (`b535a6c2`).
- New `CLAUDE.md` at repo root — project-level guidance for Claude.
  The repo had no CLAUDE.md before; future sessions now start
  with project orientation and the failure-mode rules loaded.
- `TESTING.md` new top section "Failure modes we explicitly watch
  for" — three categories: silent regressions from data growth
  (this case), plausible-but-wrong AI output (defended by
  mcp/evals/), multi-source alert dispatch picking wrong winner
  (defended by alert_routing.py).
- `.github/workflows/canary.yml` — hourly cron + on-PR-touching-
  web-DDB-paths. Curls each park, asserts non-zero ride count.
  Would have caught this within an hour instead of ~7 days.

**Design rationale.** The bug was triggered by data growth, not
a code change — review and unit tests don't catch this category
because nothing in the code changed. The defense is three layers
that catch different things: code-time review patterns (treat
data-shape assumptions as expiring contracts), test-time mocks
(simulate paginated responses), runtime canary (catches the
failure mode within the canary cadence even when nobody
anticipated the trigger). Each layer alone is insufficient;
together they cover the category.

**Follow-up queued (priority list):** GSI on `park_key` to
replace the paginated Scan with a Query (drops per-page-load
cost from ~$0.03 to ~$0.0001 and removes the implicit
"small table" assumption entirely). Web unit test scaffold
(first Vitest in `web/`) to add the test-time layer for this
read path.

#### 2026-05-24 — Plan-aware alert priority + alert routing module

Fixes a real-but-subtle dispatch bug in the poller: when a user
both favorited a ride AND had it in today's active plan, the
generic favoriter alert ("X is DOWN") was firing instead of the
more actionable plan-aware alert ("Plan disruption — X DOWN. It's
in your plan today. You may want to re-sequence the rest of the
day.").

Root cause: favoriter fanout ran first, then the plan-aware path
skipped any user already in the favoriter set — silently picking
whichever source ran first instead of whichever was most
actionable. The dispatch logic had zero test coverage, so the bug
was reachable only by reading the code.

**What shipped:**
- `infra/lambda/poller/alert_routing.py` (new) — a small pure
  resolver: takes a list of `AlertCandidate(user_id, priority,
  notifier_fn, kwargs)` and returns one alert per user (highest
  priority wins). `PRIORITY_PLAN > PRIORITY_FAVORITE` makes the
  ordering explicit in one place instead of scattered across
  `if user in other_set: continue` checks.
- `index.py` DOWN path converted to use the resolver. Same dedup
  guarantee (one alert per user per event), better routing.
- 10 new unit tests pin the regression (plan beats favorite for
  same user, both input orderings) plus the resolver's edge cases.
  Poller test suite: 25 → 35 tests, all green.

**Design rationale:** The bug is a *category* (priority across
overlapping alert sources), not a one-off. As more alert types
layer in — LOW_VS_FORECAST, show alerts, per-type toggles — the
same coordination problem would recur for every new source.
Centralizing priority in the resolver makes the category
structurally hard to recreate; adding a new source is now
"append candidates with the right priority," not "audit every
existing source's dedup logic."

**BACK_UP path converted** in `9175ebc6` (same 2026-05-24 session) —
near-mechanical mirror of the DOWN-path work. Both alert paths now
route through the same resolver. Weather alerts and the queued
LOW_VS_FORECAST work slot naturally into the resolver too; they're
not blocked, just deferred until the alert types themselves arrive.

#### 2026-05-24 — MCP eval coverage at 5 cases / 5 dimensions

Builds on the eval framework shipped 2026-05-22
(`08e50a15` — Anthropic Messages API tool-use loop, canned-response
routing, 7 assertion types, YAML-driven cases in `mcp/evals/`).
Three new cases land today, bringing the suite to 5 cases covering
5 distinct behavioral dimensions:

| Case | Dimension under test |
|---|---|
| `basic_mk_plan` | Happy path — history → context → record |
| `propose_without_recording` | Write-side guardrail (no record without consent) |
| `hot_day_indoor_preference` | Structured-context reading (weather) |
| `calibration_aware_planning` | Personalization from history (`calibration_summary`) |
| `cross_park_rejection` | Ambiguity resolution (MK day with EPCOT rides) |

Also added a `response_mentions_any` assertion type — needed
because behaviors like weather-aware reasoning surface in many
phrasings ("heat" / "hot" / "indoor" / "AC" / "shade"), so pinning
to a single word would be brittle.

**Three eval-surfaced findings worth recording:**
1. *First run of `basic_mk_plan` failed* because Claude correctly
   refused to record an unconfirmed plan. Kept that behavior as a
   regression test (`propose_without_recording`) instead of
   "fixing" the case to pass.
2. *Calibration case behavior was perfect* (Claude named the
   +18 min Seven Dwarfs bias, cut ride count, mentioned past
   plans) but the *test* failed — an over-strict literal "Magic
   Kingdom" match when the user said "MK" in the prompt and
   Claude correctly mirrored that register.
3. *Cross-park case revealed a docstring ambiguity*: Claude
   correctly short-circuited and asked for clarification with
   zero tool calls, surfacing that the
   `get_user_plan_history` docstring's "at the start of every
   planning session" doesn't cover pre-clarification turns.

Total verification cost across the session: ~$0.30.

#### 2026-05-17 — M6-B Phase 1: raw wait collection in AWS

Starts the data-plane migration from Pi-fed analytics snapshot to
MM-native collection. **Only the data collection ships today** — the
aggregator script + consumer cutover are deferred until ~3-4 weeks
of MM-native data has accumulated (mid-June 2026).

**What shipped:**
- New `db.record_wait_observation()` helper writes a row per
  (operating ride, poll) into `RIDE#<id>/WAIT#<iso_ts>` with
  1-year TTL.
- Wired into the poller's per-ride loop, defensively wrapped so a
  write failure can never break the alert path (same pattern as the
  Phase A2 forecast-write).
- Mirrors the Pi's SQLite collection pattern in DDB so the
  aggregator can eventually swap its source without changing the
  data shape it consumes.
- 112 WAIT# rows landed on the first poll cycle; row shape
  verified via `aws dynamodb scan` in production.
- 2 new tests in `tests/test_db.py` — poller suite now at 25
  tests; CI green.

**Cost:** ~$3/mo additional (~67K writes/day at $1.25/M + storage
trending to ~5 GB after a year). Within the <$5/mo budget.

**What's intentionally deferred (M6-B Phase 2+):**
- `tools/aggregate-analytics.py` modification to read both Pi
  SQLite + DDB WAIT# rows and merge into the snapshot
- Pi → DDB backfill so the Pi dependency can be retired
- Consumer-side cutover (web app keeps importing the same
  snapshot file; only the snapshot's data source changes)

**Design rationale:** Data plane is hybrid right now — Pi for
history, MM-native collecting in DDB since 2026-05-17, merging at
the aggregator script when there's enough data. The architectural
cutover happens at the script (single source of truth for the
analytics snapshot shape); consumer interface stays put. The full
Pi-retirement backfill is the eventual cleanup.

#### 2026-05-17 — Test scaffolding + CI (47 tests, GitHub Actions)

Added pytest test suites for both Python codebases plus a CI workflow
running on every push and PR to main:

- `infra/lambda/poller/tests/` — 25 tests covering storm-shift
  detection, cooldown helpers (DOWN/BACK_UP/LOW_WAIT/weather + the
  M6-B Phase 1 wait observations), weather snapshot round-trip.
  Uses a stub DDB table — no real AWS calls in tests.
- `mcp/tests/` — 24 tests covering the two pre-computation
  functions the agentic planner trusts without re-deriving:
  `_compute_calibration_summary` (cross-session feedback loop
  aggregation across both calibration paths + confidence-label
  boundaries) and `_compute_load_vs_forecast` (wait-weighted
  ratio math, exclusions, confidence labeling).
- `.github/workflows/test.yml` — runs both suites on Ubuntu w/
  Python 3.12. Green badge in README.
- `TESTING.md` — strategy doc: what's tested, what's deliberately
  not, design philosophy ("data plane does the math, LLM narrates
  — those pre-computation functions are the most consequential
  code and the easiest to test rigorously").

**The bar:** every pre-computation function the LLM trusts without
re-deriving needs a test that pins its math. Alert-side helpers
that gate user-visible Pushover pings are tested for the same
reason — false negative = missed plan-disruption alert, false
positive = 3am phantom Pushover.

Closes the "no formal testing" gap.

#### 2026-05-16 — Living wisdom + preferences architecture

Section 0c added to `get_planning_context` docstring: planner now
fetches two living Google Docs from the user's Drive at plan time
(via the Drive MCP tools already loaded in Claude Desktop):

- **"Disney Wisdom"** — global operational tactics (LL strategy,
  burner-ride trick, SLL-doesn't-unlock-tiers gotcha, scan-in
  windows, annual-passholder workarounds). Editable by the user's
  non-technical sister directly in Google Docs.
- **"Disney Planner Preferences"** — per-person sections
  (`## Megan`, `## Mark`, `## Karen`). Planner reads the section
  matching the implied user.

Also codifies a 5-tier precedence hierarchy for conflict resolution:
park reality (wisdom facts) > current prompt > preferences > wisdom
tactics > planner framework. Park reality is the only non-negotiable
tier; the others are intent at different timescales.

Validated working in Claude Desktop — planner now fires the Drive
`search_files` + `read_file_content` tool calls before laying out
plans.

#### 2026-05-12 — Weather-shift alerts

Completes the "system noticed something that invalidates your plan"
loop along a second axis. The 2026-05-11 deploy added plan-aware
DOWN/UP alerts (per-ride disruption); this deploy adds plan-aware
weather alerts (park-wide disruption). Both fire to the same active-
plan set from the same scan, deduped per user.

**What it does:**
- On every 2-min poll where active plans exist for today, fetch
  Open-Meteo's 6-hour forecast for WDW.
- Compare against the previously persisted snapshot
  (`WEATHER#WDW/SNAPSHOT` row, 2-day TTL). If the new forecast
  contains a thunderstorm code (95/96/99) in next_6h AND the prior
  snapshot did not → fire a plan-weather-shift Pushover.
- Per-(user, plan) cooldown (`USER#<id>/COOLDOWN#WEATHER#<plan_id>`,
  60-min TTL) prevents re-pinging while the storm stays in the
  window. A distinct second storm window later in the day can still
  re-alert after cooldown.
- Cost gate: zero weather HTTP calls on days when no one has an
  active plan.

**Why narrow (storm only, not precip-jump):**
Florida summer rain shifts up/down all day and would generate noise.
Storm = lightning hold = actual Disney behavior (outdoor rides
pause) = real replan trigger. v1 trades coverage for signal quality.

**Design pattern:**
Mirrors the existing per-ride cooldown shape (DOWN/BACK UP/STILL DOWN/
LOW WAIT) and the active-plan scan from 2026-05-11. One scan yields
both views (per-ride index for DOWN/UP fanout, per-plan summary for
weather fanout). New `weather.py` module duplicates a trimmed copy of
the MCP server's `_fetch_weather_forecast` — different runtime, small
enough to copy, deliberate.

**Files:**
- `infra/lambda/poller/weather.py` (new) — fetch + storm-shift detector
- `infra/lambda/poller/db.py` — snapshot + per-plan cooldown helpers
- `infra/lambda/poller/notifier.py` — `alert_plan_weather_shift`
- `infra/lambda/poller/index.py` — integration after plan_ride_index

#### 2026-05-11 — Public repo, real-world validation, calendar awareness

Repo went **public** on GitHub today; MIT LICENSE added; final
pre-public sweep confirmed no secrets in tracked files (`.env.local`
is gitignored and was never committed). Live in-park-style alert
testing surfaced and fixed two production bugs (BACK_UP flap cooldown,
ride-completion semantics gap), and the planner gained calendar
awareness for after-hours parties.

**Production fixes (from real-world alert testing):**
- **BACK UP cooldown.** Pre-existing bug: DOWN alerts had a 15-min
  cooldown to suppress flap spam but BACK UP alerts had none.
  Result: a flapping ride generated 1 DOWN alert + N BACK UP alerts.
  New `COOLDOWN#BACK_UP` row pattern (15-min TTL) mirrors the DOWN
  cooldown; same gate covers both favoriter and plan-aware UP
  fanouts. Deployed via cdk deploy.
- **Completion vs. abandonment semantics.** `remove_ride_from_plan`
  was the only mid-trip exit, lumping "I rode it" with "I'm skipping
  it" into one signal — calibration loop undercounted completions
  and lost actuals. Schema gained `completed_rides` + `dropped_rides`
  arrays. New tool `mark_ride_complete(plan_id, ride_id, ride_name,
  actual_wait_min?, notes?)` captures actuals at the strongest signal
  point (mid-trip, not end-of-day recall). `remove_ride_from_plan`
  modified to MOVE to `dropped_rides` (preserves entry + adds
  optional reason) instead of deleting. Calibration loop extended
  to read prediction-vs-actual from completed_rides FIRST, with
  per_item_feedback as legacy fallback. **22 tools total now.**

**Calendar intelligence (M8 scaffolding):**
- New `mcp/data/party_calendar.json` carries MNSSHP / MVMCP /
  Jollywood Nights schedules with crowd_effects + non-party-ticket
  implications + dates_status (verified vs estimated).
- New `get_party_calendar(date?, days_ahead=14)` MCP tool.
- Planner docstring section 0b: tells Claude to call this for any
  MK or HS plan, surface the 6pm-closure constraint for non-party
  guests BEFORE sequencing, apply ~0.80-0.85 load_ratio adjustment
  for party-day daytime crowds.
- MNSSHP 2026 dates verified from disneyworld.com (38 nights);
  MVMCP estimated pending Disney's Christmas announcement;
  Jollywood Nights pending Disney's HS announcement.

**Portfolio infrastructure:**
- **MIT LICENSE.** Repo is now public-ready and explicit on usage
  rights.
- **`docs/aws-setup-brief.md`** — self-contained briefing document
  for spinning up sibling projects under the same AWS account.
  Covers identity/auth, sibling project context (Watchtower, MM),
  reuse-vs-fresh decisions, the five M2-B lessons, CDK conventions,
  Python 3.12 default rationale, Megan's working preferences. Drop-in
  prompt for fresh agents on new projects.

#### MCP suite — agentic trip planner (✅ shipped 2026-05-10)

Magic Monitor exposed as 17 MCP tools that any MCP client (Claude
Desktop, agentic frameworks) can call conversationally. **This is
the project's headline capability** — agentic trip-planner answers natural-
language route questions in Claude Desktop using one consolidated
`get_planning_context` call, then learns from outcomes across
sessions via a feedback loop with server-side calibration.

**Tools, by capability:**

- *Sanity:* `hello_magic_monitor`
- *Analytics (offline JSON snapshot, 8.8M historical rows):*
  `get_park_heatmap`, `get_ride_analytics`, `get_ride_dow_pattern`,
  `get_ride_down_clusters`, `get_short_wait_baseline`,
  `get_ride_ll_drops`, `find_rides_matching`
- *Live DDB reads:* `get_ride_forecast`, `get_live_ride_status`,
  `get_park_live_status`, `get_ride_downtime_today`
- *Live external (themeparks.wiki):* `get_park_showtimes`
- *The agentic planner:* `get_planning_context` — one-shot per-ride
  live status + forecast + DOWN history + lat/lon + park hours +
  weather + today-vs-forecast correction + park-wide DOWN list +
  LL drop patterns + headliner showtimes
- *Plan feedback loop:* `record_plan`, `record_plan_outcome`,
  `get_user_plan_history` (returns server-computed
  `calibration_summary` — aggression averages, timing distribution,
  per-ride / per-show prediction bias with sample sizes + confidence
  labels + ready-made interpretation strings)

**Other shipped pieces under this umbrella:**

- `attraction-locations.json` — lat/lon for all 88 WDW attractions
  (fetched from themeparks.wiki entity endpoints) so the planner
  can do haversine-distance proximity grouping.
- LL drop analytics from sibling Pi project's `ll_history` (159K
  events / 5 weeks / 46 rides), summarized as per-ride drop hours
  + drops-per-active-day + typical-shift-minutes for the planner
  to suggest LL refresh windows.
- Verbatim Python port of the showtimes classifier from
  `web/src/lib/showtimes.ts` with cross-file "keep in sync" comments
  on both sides. Six-bucket categorization with named-act overrides
  (Indy Epic Stunt Spectacular as stage not spectacular; festival
  concert series as music; Candlelight Processional as stage).
- boto3 default-profile resilience — Claude Desktop strips the env
  block from MCP config on quit/launch, so the server defaults to
  `AWS_PROFILE=watchtower` internally. Survives Claude Desktop
  restarts without re-editing config.
- ~30K-character `get_planning_context` docstring carrying the full
  planner rulebook: hard-constraints discovery (dining, LL, virtual
  queue, shows), cost-of-delay reasoning, today-vs-forecast scaling
  with confidence thresholds, DOWN-state diagnosis (mechanical vs
  weather-caused with concurrent-outdoor-rides signal), proximity
  grouping, feasibility check with 2-3 alternate full plans on
  overcommit, meal/break windows, per-park parade routes + viewing
  spots, crowd-scaled show arrival times, water-rides hot-day
  exception, and the cross-session feedback loop instructions.
- Server-side calibration aggregation pattern (mirrors the live
  `today_vs_forecast` design): pre-computed numbers + confidence
  labels + interpretation strings in the read tool, narration in
  the LLM.

#### Phase A2 — Forecast capture in poller (✅ shipped 2026-05-10)
- Poller extracts the `forecast` array from each `/live` response and
  writes one `RIDE#<id>/FORECAST#<polled_at>` row per poll, TTL'd
  after 7 days. No additional API calls — forecast was already in
  the same payload.
- New STATE attribute `last_forecast_at` tracks forecast presence
  cheaply, so we don't store 5K+ empty rows/day for the ~23% of
  attractions that never have a forecast (DOWN rides, walk-ups,
  transportation, shows).
- Underpins the live-data half of `get_planning_context` — without
  this Phase, the agentic planner couldn't reason about future
  waits, only current.

#### 2026-05-06 — Web app analytics + showtimes + onboarding wave
Single-day wave that landed the most demo-visible web features:

**M6 — Analytics (the impressive layer)**
- Per-park hour × day-of-week heatmap.
- Per-ride downtime ranking with 3 sort modes
  (`?sort=down|wait|name`).
- Drawn from 8.8M historical poll rows in sibling Pi project's
  SQLite (analytics-snapshot.json, ~230 KB, regenerated by
  `tools/aggregate-analytics.py`).
- Pre-aggregated rather than streams→Athena pipeline (rationale
  in README: poller writes only status changes, dataset is 1.5GB
  not 10GB+, freshness-vs-cost calculus skews against Athena).
- Heatmap renders in park-day order (4am ET boundary), not
  calendar-clock — matches how the analytics aggregator buckets.

**M4 — Showtimes web app**
- `/parks/<park>/today` page with chronological showtimes.
- Six-bucket name-based classifier (spectacular / parade / stage /
  music / atmosphere / character_meet) with named-act overrides
  for shows the regex misclassifies.
- Category-pill filters + live search.
- "Next up" callout with soonest unstarted performance across the
  park.
- Headliner / atmosphere split.
- "Today's shows →" link added to landing-page park cards.

**M3 Phase 3 — New-user onboarding gate**
- First sign-in (no `USER#<sub>/PROFILE` row) → redirect to /me
  onboarding flow before any other page.
- Default zero rides → zero alerts, no welcome-spam.

**SHORT_WAIT alerts (M7+ surface, shipped early)**
- Poller imports `baselines.json` (per-(ride, hour-of-day) wait
  thresholds, generated alongside analytics-snapshot).
- For each operating ride: if `current_wait ≤ min(30, 0.5 ×
  typical)` and 90-min cooldown isn't active, low-wait Pushover
  fires.
- Only 38 of 88 tracked rides have baselines — for rides with
  short typical waits, alerting "this is short" is meaningless.

#### M3 — Per-user dashboard pages, Phase 1 + Phase 2 (✅ shipped 2026-05-05)
- **Phase 1:** `/me` page with profile + Pushover key + park
  toggles. Route handlers under `web/src/app/api/me/*` use
  NextAuth session to scope writes to `USER#<sub>`. CDK extended
  the SSR compute role with scoped `Update`/`Put`/`Delete`
  permissions.
- **Phase 2:** favorite-rides grid per park + per-favorite ∩
  per-park-subscription alert intersection in the poller. Schema:
  `USER#<sub>/FAV_RIDE#<ride_id>` with denormalized `park_key`.
  Poller fans out only to users who both subscribe to the park
  AND have favorited the ride that changed status.
- Auth fix: anchor session sub to Cognito ID-token sub
  (the access-token sub differs from ID-token sub for federated
  Google sign-ins).
- Defensive trust-policy override on the SSR role (RUNBOOK
  Lesson 5 round 2 — alpha CDK construct re-introduces SourceArn
  conditions on cdk deploys that touch the App resource).

#### M2-B — Auth + production deploy (✅ shipped 2026-05-05)
- Live at https://magicmonitor.megillini.dev with TLS, Google sign-in
  via Cognito, and live ride data rendered server-side from DynamoDB
- CDK adds Amplify SSR app + custom domain + Cognito 2nd app client +
  GitHub OIDC role for future CI deploys, all in `disney-stack.ts`
- NextAuth wired in `web/` (auth.ts, route handler, Sign In/Sign Out
  buttons in header). M1 poller untouched.
- Real journey took ~7 hours (vs ~3 estimated) due to a stack of
  AWS-side changes that landed within days of MM's setup. See
  RUNBOOK.md "M2-B journey" for the 5 specific lessons learned —
  required reading before touching Amplify Hosting in CDK again.

#### M1 — Backend (✅ deployed 2026-05-04)
- CDK stack: DynamoDB single table + Poller Lambda + EventBridge schedule
- Python Lambda ports `wait_times.py` + `monitor.py` diff logic
- Pushover notifier with multi-user fanout (PARK#x/USER#y subscription model)
- Schema supports per-user preferences from day 1, no future migration

#### M1.5 — Park-hours alert filter (✅ deployed 2026-05-04)
- Lambda fetches each park's schedule from themeparks.wiki on every poll
- Alerts only fire if `open ≤ now ≤ close - 30min`
- Outside that window: data still flows to DynamoDB, just no Pushover ping
- Fail-open if schedule API breaks (better noise than missed alerts)

#### M2-A — Public web dashboard (✅ local 2026-05-04)
- Next.js 16 + Tailwind 4 + React 19 scaffold, mirrors Watchtower
- Castle palette (deep navy / castle pink / castle gold) in OKLCH
- Landing page with park selector cards, each showing live status
- Per-park page with Down rides surfaced first, then Open, then Closed
- Park hours display: Early Entry, regular hours, Extended Evening Hours
  (with deluxe / DVC label) — refreshes every 10 min
- Closed-state notice replaces stale ride list when park is shut

### Next

#### Update MVMCP + Jollywood Nights dates when Disney announces (~10 min, manual)
- MNSSHP 2026 verified on 2026-05-11; MVMCP and Jollywood still
  pending. When Disney publishes (typically May-June for the same
  year), replace the estimated/empty `dates` arrays in
  `mcp/data/party_calendar.json` and flip `dates_status` to
  `verified_from_disney_calendar`. The planner docstring already
  tells Claude to hedge confidence based on this status.

#### Capture Claude Desktop screenshots (~30-45 min, mostly manual)
- Brief at `docs/screenshot-brief.md`. Two required + one optional
  (the optional one now genuinely demoable thanks to the
  mark_ride_complete tool + the feedback loop).
- Once PNGs land in `docs/screenshots/`, agent can wire them into
  the README demo grid in ~2 minutes.

#### M9 Phase 1 — HTTPS MCP transport for Claude mobile (~3-4 hr)

Broken out from the full M9 because the mobile use case is
product-load-bearing: the family uses MM in the parks, and the
planner needs to be reachable from a phone. The Claude mobile app
only supports remote MCP servers (HTTPS), not local stdio like the
current setup — so this work unlocks mobile-from-the-park usage
without shipping the full M9 web-chat UI.

**Designed 2026-05-16 with a risk-managed shape:**

- **Duplicate-first, not refactor-first.** Don't touch `mcp/server.py`.
  Create `mcp/server_http.py` from scratch with verbatim copies of
  the tool definitions. Extract-to-shared-impl-module is a future
  cleanup once HTTP is proven working. This keeps the Claude Desktop
  stdio demo 100% unaffected — worst case is "mobile doesn't work
  yet, but stdio works as before."
- **Net-new AWS resources only.** New Lambda + API Gateway + SSM
  bearer-secret param. Doesn't touch the Amplify App (avoiding
  RUNBOOK Lesson 5-2 territory) or the existing poller stack.
- **Rollback is `cdk destroy` of just the new constructs** —
  ~10 min worst case if anything misbehaves.

**Auth-model spike (FIRST step, before any code):**
The single uncertainty is whether Claude mobile's MCP integration
supports bearer-token auth or requires full OAuth 2.1 with PKCE
(which is in the MCP spec for remote servers). Spend 15-30 min
upfront reading Anthropic's mobile MCP docs OR opening the Claude
mobile app's "Add MCP Server" screen to see what fields it asks for.

- If bearer-token works → proceed with the 3-4 hr build below
- If OAuth 2.1 required → scope balloons to ~6-8 hr (Cognito as
  OAuth provider, dynamic-client-registration). Present the tradeoff
  before writing code; decide whether to ship now or defer.

**The 3-4 hr build (after spike confirms bearer-token path):**

1. **HTTP transport** (~1 hr) — `mcp/server_http.py` using the
   MCP SDK's HTTP transport. Verbatim tool definitions copied from
   `server.py` for now. Includes the same wisdom/preferences fetch
   docstring section 0c.
2. **Lambda handler wrapper** (~30 min) — Lambda-style entrypoint
   that wraps the HTTP transport for the API Gateway invocation
   pattern. MCP SDK has Lambda examples to reference.
3. **CDK Lambda + API Gateway + SSM secret** (~45 min) — new
   constructs in `disney-stack.ts`. Bearer-token auth via APIGW
   authorizer Lambda or direct header validation in the handler.
   DDB read/write permissions matching what `server.py` uses.
4. **Smoke test via curl** (~15 min) — list tools endpoint, call
   one tool with auth header, verify response shape. DO NOT
   configure mobile until curl tests pass.
5. **Configure Claude mobile** (~10 min) — mobile Settings →
   Connectors / MCP Servers → Add → paste HTTPS URL + bearer
   token. Verify `magic-monitor.*` tools appear in mobile's tools
   menu.
6. **Real-world test on phone** (~30 min) — run a planning query
   from the phone. Test screenshot upload + tool integration
   (Disney app screenshot → Claude reads → planner sees current LL
   bookings). Verify the Drive MCP for wisdom/preferences also
   works on mobile (needs Drive integration enabled in mobile
   Claude account separately, same Google account).

**Design rationale:** Dual-transport architecture — same tool
implementations served over both stdio (Claude Desktop) and HTTPS
(Claude mobile in the park). Stdio shipped first to validate the
agentic-coding workflow before investing in HTTPS infrastructure;
ported once usage proved the planning loop was valuable.

**Auth upgrade path (post-mobile-bootstrap):** Bearer secret is
v1 — sufficient behind a non-discoverable URL for single-family
use. Upgrade target is Cognito JWT validation on each request,
reusing the user pool the web app already uses. Slot in alongside
full M9 (Phases 2-6) later.

**Cost impact:** ~$1-2/mo additional (API Gateway @ $1/mo +
Lambda free tier covers usage). Within budget.

#### LOW_VS_FORECAST alert — crowd-adjusted opportunity detection (~2-3 hr)

Second baseline for the low-wait opportunity alert path. Where the
existing LOW_WAIT compares current wait to a STATIC historical
baseline from `baselines.json` (Pi-fed, regenerated when
`aggregate-analytics.py` runs), this new alert compares against
themeparks.wiki's DYNAMIC per-hour forecast for the same ride today.

**Why two baselines instead of one.** The signals catch different
classes of opportunity:
- LOW_WAIT (historical): "*This ride is anomalously low for this
  hour, all-time.*" Catches end-of-day Pirates, fireworks-time
  Carousel of Progress — moments rare across history.
- LOW_VS_FORECAST (today-aware): "*This ride is beating today's
  specific expectation.*" Catches the heavy-crowd day where Big
  Thunder hits 40 min when today's forecast said 65 — an opportunity
  LOW_WAIT misses because absolute wait is still above its all-time
  half-typical threshold.

**Killer case.** On a heavy-crowd day (today_vs_forecast > 1.15),
the park is running hotter than the historical baselines expect.
LOW_WAIT will essentially never fire. LOW_VS_FORECAST is what catches
genuinely-better-than-today's-load moments on those days.

**Park-wide normalization (the design wrinkle).** A naive
`current_wait < forecast` rule over-fires on "crowds light today"
days when *everything* is below forecast. The alert needs to fire
only when this specific ride is doing *meaningfully better* than the
park-wide load this hour, not just "below its own forecast":

```python
ride_ratio = current_wait / forecast_for_this_hour
park_ratio = today_vs_forecast.ratio   # already computed server-side
# Fire when this ride is ≥25% ahead of park average AND the gap is
# meaningful in absolute minutes.
fire = ride_ratio <= 0.75 * park_ratio  AND  current_wait <= forecast - 15
```

This mirrors the `today_vs_forecast` pattern already used by the
MCP planner — same data plane signal, applied to alerting.

**Threshold tuning caveat.** As of 2026-05-12 we have ~2 days of
FORECAST# data. The feature will *function* on minimal data, but the
threshold values above are first-pass guesswork. Plan to re-tune from
observation after ~30 days of data. All thresholds env-var
configurable (mirror the LOW_WAIT pattern) so tuning is a config
change, not a redeploy.

**Dedup with existing LOW_WAIT.** A single ride could in principle
satisfy both conditions at the same moment. Options:
- *Single combined alert* — "Big Thunder: 35 min, way under both
  typical and today's forecast." Strongest signal, single push.
- *Separate cooldowns, separate alerts* — two pushes for the same
  ride at the same moment is noisy; reject this.
- *Shared cooldown row* (`COOLDOWN#LOW`) covering both alert types
  — at most one low-wait push per ride per cooldown window, body
  text picks the strongest applicable signal.

Recommendation: shared cooldown, body text adapts. Cleanest UX.

**Files affected:**
- `infra/lambda/poller/db.py` — helper to fetch latest FORECAST# row
  per ride for the current hour; potentially share LOW_WAIT cooldown
  row.
- `infra/lambda/poller/forecast_signal.py` (new) — per-ride
  ratio-vs-park computation, threshold check.
- `infra/lambda/poller/index.py` — after the existing LOW_WAIT check,
  evaluate LOW_VS_FORECAST; pick body text based on which signal is
  strongest.
- `infra/lambda/poller/notifier.py` — extend `alert_low_wait` (or
  add `alert_low_vs_forecast`) — decide based on the dedup design.

**Today_vs_forecast aggregation in the poller.** The MCP server
computes this on demand inside `get_planning_context`. The poller
would need its own implementation (Lambda runtime, no MCP deps) — a
small function that scans current STATE rows for the park, joins
against the latest FORECAST# row per ride, returns the per-park
ratio. ~30 lines. Same logic, second copy — same trade-off as the
showtimes classifier dual-impl.

**Design rationale.** Started with a historical baseline and
realized it blinded the alert path on heavy-crowd days. Added a
second baseline using the live forecast the planner already
consumed, normalized against park-wide load so the signal stays
clean on quiet days too. Same data plane, two complementary alerts —
one catches all-time anomalies, the other catches today-specific
opportunities.

**Acceptance criteria:**
1. Active rides where current_wait is ≥25% ahead of park-wide
   today_vs_forecast AND ≥15 min under forecast trigger an alert.
2. Quiet days (park_ratio < 0.9) don't generate per-ride spam.
3. Cooldown shared with LOW_WAIT — one low-wait-class push per ride
   per cooldown window.
4. Body text indicates which baseline triggered ("vs typical" /
   "vs today's forecast" / "both").
5. All thresholds env-var configurable.

#### M6-B Phases 2+ — Aggregator cutover + Pi retirement (~4-8 hr)

**Phase 1 shipped 2026-05-17** (see Done section above). Raw wait
collection in DDB is now running. The remaining phases handle the
consumer-side cutover once 3-4 weeks of MM-native data has
accumulated (target: mid-June 2026).

**Phase 2 — Augment the aggregator (~2-3 hr):**
- Modify `tools/aggregate-analytics.py` to read both Pi SQLite AND
  DDB WAIT# rows (via boto3). Merge into the same snapshot shape
  the script already emits.
- Pi data covers everything through ~2026-05-10; DDB data covers
  everything from 2026-05-17 onward. ~1 week gap during cutover
  is statistical noise at the heatmap aggregation grain — document
  in TESTING.md or a script comment.
- Web app consumer interface unchanged; same `analytics-snapshot.ts`
  imported the same way.
- Run the script manually; analytics page updates after each push.

**Phase 3 — Automate the regen (~1-2 hr, optional):**
- Cron on laptop, GitHub Action on a schedule, or AWS Lambda + EventBridge
- Replaces the manual `python tools/aggregate-analytics.py` step

**Phase 4 — Retire the Pi (~3-4 hr):**
- One-time backfill: read Pi SQLite history, write equivalent
  WAIT# rows to DDB so the Pi snapshot can be removed.
- Modify the aggregator to read only DDB.
- Delete `.scratch/disney-pi-snapshot.db` and the Pi-reader code
  paths from the script.
- The repo no longer ships any Pi-fed data; the Pi can be unplugged.

**Design rationale:** Shipped the analytics with a Pi-fed snapshot
to get something demoable fast, then evolved the data plane to
MM-native collection without changing the consumer interface.
Backfilled the historical data so the analytics page never noticed
the cutover.

Unblocks M8 — once M6-B has been collecting MM-native data alongside
the Pi history for ~3 months, the union covers the seasonal mix M8
needs.

#### M5 — Trip planning (~1 week)
- Trip CRUD: dates + parks per day + party size
- Auto-toggle subscriptions based on current trip dates
- Per-trip-day showtime view (uses M4 data)
- Optional: household linking so spouse + sister get the same alerts
- "Mark as ridden today" — TTL'd row (`USER#<sub>/RIDDEN#<ride_id>#<YYYY-MM-DD>`,
  ~24h TTL) that suppresses alerts for rides you've already done.
  Captured here from M3 Phase 2 discussion: a real day-of-trip need
  (one alert per ride per day cap) but only useful in the context of
  an active trip, so it lives here rather than in M3.
- *Demo-able:* "I have a trip June 15-20, here's the calendar — alerts
  auto-enable on those dates and I see what shows are running each day"

### Future

#### M7+ — Polish (grab bag)
- Per-type alert toggles on `/me` — currently every alert recipient
  gets DOWN, BACK UP, STILL DOWN, and LOW WAIT. Needs `down_up` /
  `short_wait` per-user toggles. Lightning-lane alerts remain out of
  scope (no public LL purchase API).
- Show alerts: opt-in per show, fires N min before start time. Needs
  a daily showtime poll (stable through the day, no per-2-min churn)
  plus a per-user `SHOW_ALERT#<show_id>` row mirroring the FAV_RIDE
  shape. Deferred scope.
- Per-ride alerts (not just per-park)
- Email digest summary at end of trip
- Public read-only stats page (no sign-in needed)
- Mobile push notifications via Web Push (alternative to Pushover)
- Live LL transition capture in the MM poller (currently LL drop
  analytics come from the Pi snapshot; capturing live keeps the data
  fresh past the snapshot date — a smaller cousin of the M6-B move).
- Weather-history persistence in DDB. Would let the planner answer
  "what was the weather when this ride went DOWN" with data instead
  of inference. Discussed in Q&A 2026-05-10 and explicitly deferred
  — the live `today_vs_forecast` signal is what drives planning;
  weather history is mostly explanatory, not actionable. Build only
  if explainability becomes a UI feature.

#### M8 — Calendar Intelligence (~1 week, scoped post-M6-B)

Extend the analytics dimensional model with calendar context so
"Sunday at 2pm" stops being a 57-day-window average and becomes
filterable by month, season, US holidays, school breaks, and
Disney-specific events (EPCOT festivals, Halloween/Christmas
parties). Real-world wait patterns vary dramatically with these
factors — a Sunday-2pm in March is structurally different from a
Sunday-2pm in July or in mid-December.

Schema additions:
- Calendar dimension: `(date, dow, week_of_year, month, season,
  is_weekend)` — pure derivation.
- US holiday flags via Python's `holidays` package.
- School-cycle flags: spring break (state-by-state hardcoded
  ranges), summer break, winter break, Jersey Week.
- Disney-event ranges: Festival of the Arts, Flower & Garden, Food
  & Wine, Festival of the Holidays, MNSSHP, MVMCP. ~12 rows/year,
  manual but stable.
- Optional: Touring Plans crowd-calendar score (external dataset).
- Optional: NWS historical weather (free).

Aggregator extension: per-(ride, dow, hour, calendar-cohort) cells
*when sample size justifies*, falling back to coarser cells when
not. The data-engineering judgment call here — over-segmenting
thin data produces confident-looking but unreliable answers — is
worth being deliberate about.

MCP tool: `get_ride_pattern(ride_name, cohort_filter)` accepting
cohort predicates like `{"month": "july"}` or
`{"festival": "food_and_wine"}` or `{"holiday_week": true}`.

Date-segmented heatmap UI: toggle "all data" / "summer only" /
"non-holiday weekends" / etc.

**Blocked on dataset depth.** With <1 year of data, fine-grained
cohort filtering produces statistically thin cells. M8 is shippable
when the union of Pi-historical + MM-native data covers ≥12 months
across the right mix of seasons / holidays / events. M6-B (live
data plane) is what gets MM-native data accumulating; M8 follows
naturally a few months later.

#### M9 — Embedded agentic chat (~2-3 days)

Bring the MCP planner experience into the web app itself so users
who don't run Claude Desktop (e.g., Megan's sister, husband, anyone
who wants to try MM from a phone in the park) can talk to the
agentic planner the same way Claude Desktop users can today.

**Architecture decision: Option C — shared tool implementations.**
Refactor `mcp/server.py` so each tool's pure data-fetching logic
moves into a shared module (`mcp/_tool_impls.py`); both
`mcp/server.py` (stdio MCP for Claude Desktop) and a new HTTP
transport (`mcp/server_http.py`, deployed as a Lambda) wrap the
same impl functions. Single source of truth for tool logic AND
docstrings; both delivery paths work; cleanest architecture story
("the same tool layer powers both stdio MCP and the in-app chat").

Alternatives ruled out: (A) HTTP-MCP-only adds cold-start latency
and discards the stdio demo; (B) duplicating tool definitions in TS
splits the source of truth and means every classifier/docstring
change touches two files.

**Phases (each a clean stopping point):**

1. **Tool refactor + HTTP transport (~3-4 hr).** Extract tool bodies
   to `_tool_impls.py`; expose via FastAPI or MCP SDK HTTP transport
   as a new Lambda; exercise via curl. Claude Desktop demo unchanged.
2. **Chat backend Route Handler (~4-6 hr).**
   `web/src/app/api/chat/route.ts`. Validates NextAuth session
   against an allowlist of Cognito subs; calls Anthropic Messages
   API with tool definitions; tool-use loop; SSE streaming; **prompt
   caching** on the docstrings + tool schemas (huge cost win — those
   are stable + massive).
3. **Chat UI page (~3-4 hr).**
   `web/src/app/chat/page.tsx`. Streaming-chat component, tool-use
   indicators ("Calling get_planning_context…"), mobile-friendly.
   Reuses existing castle palette + typography.
4. **Allowlist + per-user token tracking (~2-3 hr).** Allowed
   Cognito subs in env var (`ALLOWED_CHAT_SUBS=sub1,sub2,sub3`).
   Per-user monthly token totals to DDB
   (`USER#<sub>/CHAT_USAGE#<yyyy-mm>`); soft-cap at a budget;
   friendly error when exceeded. Per-conversation max-turn cap
   prevents runaway tool-use loops.
5. **CDK + secrets (~30 min).** SSM param
   `/disney/anthropic/api_key`; grant SSR Lambda read access; add
   the env var. Make sure the new HTTP-MCP Lambda is granted the
   same DDB perms the stdio MCP relies on (read STATE / FORECAST /
   USER#PLAN# rows).
6. **Polish + testing (~2-3 hr).** Tool-use loop edge cases, rate
   limiting, error boundaries. Mobile test on phone.

**Cost reality check.** With prompt caching (90% off cached reads)
on the ~30K-char docstrings + tool schemas, a typical planning
conversation is ~$0.05-0.10. Three active users × 5 conversations/
week = ~$3-5/month additional, well under the project's <$5/mo
budget after MM's other costs.

**Why deferred:**
- Streaming + tool-use + auth interactions are notoriously fiddly.
  Half-finished UI is worse than no UI when the existing Claude
  Desktop path already works.
- Today's Claude Desktop MCP setup is the agentic-coding headline.
  Risk-averse to disrupt it before the in-app chat path is fully
  validated.
- M9 will showcase production agentic-coding patterns when shipped:
  allowlist auth, prompt caching, per-user cost controls, graceful
  tool-use failure handling.

**Dependencies (all met):**
- Cognito auth (M2-B ✓)
- DDB Route Handler write pattern (M3 ✓)
- Stable MCP tool layer (✓ as of 2026-05-11 — 22 tools incl.
  full plan-feedback loop with calibration_summary,
  mark_ride_complete, get_party_calendar)

**Stretch follow-on (call it M9.1):** Pushover-driven proactive
feedback collection. When `record_plan` writes a plan, schedule a
Pushover ping for park-close + 30 min: "How'd today go? GREAT /
FINE / RAN-OVER" with three URL buttons that hit a route handler
on the web app and auto-record the coarse outcome. Closes the
feedback loop without requiring the user to remember to chat.
~3-4 hr. Build only if real usage of the cue-based + next-session
triggers proves they don't capture enough feedback on their own.

## Recommended ordering rationale

- **M2-B before M3:** M3 (per-user toggles) requires auth. M2-B
  is "make auth exist." Hard to do M3 without it.
- **M4 (showtimes) before M5 (trip planning):** Trip planning
  surfaces *what's happening on each day of your trip*, and showtimes
  are a big part of that. Building showtimes first means trip planning
  has more to render.
- **M6 (analytics) is the differentiator:** It's the most distinctive
  part of the whole project — months of polling history → heatmaps and
  downtime stats no one else has. The "why this and not Touring Plans"
  answer.

## Priority order

Original ordering (as of project start):

1. **M2-B** — gets a live URL at a real domain. Single most
   user-visible milestone.
2. **M3** — multi-user signup, lets anyone try the app.
3. **M6** — distinctive analytics not available elsewhere.
4. **M4 + M5** — personal-use polish (real trip value, smaller
   visibility lift).

Status as of 2026-05-11: 1, 2, 3, and the M4 half of 4 all shipped.
The MCP suite (added mid-roadmap, not in original order) became the
new headline — agentic trip-planner using real WDW data is the
single most distinctive piece of the project. Repo is public.

**Refreshed priority (reordered 2026-05-16):**

**Path chosen: protect what's shipped + ship small bounded additions
when bandwidth allows.** Balancing full-time job + family means
limited weekly hours. Recent shipping items: test scaffolding + CI
(2026-05-17), M6-B Phase 1 raw collection (2026-05-17), MCP eval
framework + 5 cases (2026-05-22 / 2026-05-24), alert routing module
+ plan-aware DOWN alert fix (2026-05-24). Data collection clock is
now ticking — by ~mid-June the aggregator can swap source from Pi
to DDB.

1. **M6-B Phase 4 — Pi-to-DDB backfill + aggregator cutover**
   (~2-3 hr, single session, splittable). Backfill script tested
   end-to-end 2026-05-24 (`3266bcc2`); ready to run the full ~5M-row
   write (~$6.30) followed by the aggregator cutover to read DDB
   instead of SQLite. Decision pending on HIST# / downtime handling
   (extend retention vs. change poller to write per-poll). After this
   ships: M6-B is closed (Phase 2 conservative-merge skipped in
   favor of going directly to the endpoint), Pi can be unplugged,
   analytics is 100% AWS-native.
2. **GSI on `park_key`** (~1 hr + small CDK deploy). Replaces the
   current paginated Scan in `getParkRides` with a Query against a
   sparse index. Drops per-page-load cost from ~$0.03 to ~$0.0001
   and removes the implicit "table fits in one Scan page" assumption
   that caused the 2026-05-24 silent regression. CDK change adds the
   GSI; minor schema migration (existing items get indexed
   automatically via the partition key); web reader switches from
   ScanCommand to QueryCommand. Pair with the Web unit test
   scaffold below.
3. **Web unit test scaffold** (~1-2 hr). First Vitest setup in
   `web/`. Add a unit test for `getParkRides` with a mocked DDB
   client returning paginated responses (asserts the function
   accumulates across pages). Test-time layer of the three-layer
   defense documented in TESTING.md. Unblocks future web-side
   regression tests for any new SSR data path.
4. **LOW_VS_FORECAST alert** (~2-3 hr) — second baseline on the
   low-wait alert path. Catches heavy-crowd-day opportunities the
   historical baseline blinds you to. Single-session work, additive
   to the poller. Designed 2026-05-12. Slots into `alert_routing`
   as a new priority tier. Ship if a 2-3 hour window opens.
5. **Capture Claude Desktop screenshots** — `docs/screenshot-brief.md`
   has the three target queries. Manual work at a bigger monitor
   when convenient. No session commitment needed.
6. **Update MVMCP + Jollywood dates** when Disney publishes them
   (~10 min, manual). Gated on Disney announcing.
7. **Blog at megillini.dev** — first post showcases Magic Monitor.
   Separate project queued at `.planning/blog/`. Not blocking.
8. **M6-B Phase 3 — Aggregator regen automation** (~1-2 hr).
   After Phase 4 cuts over, the aggregator still runs manually.
   Cron / GitHub Action / Lambda + EventBridge — pick whichever
   fits the workflow. Lower leverage than Phase 4; do after.
9. **M9 Phase 1 (mobile HTTPS MCP)** — **Deferred.** Design captured
   in the Next section above. OAuth 2.1 with PKCE required (confirmed
   empirically — Claude mobile UI only offers OAuth on "Add MCP
   Server"). Real estimate is ~6-10 hr with Cognito as OAuth provider
   + DCR proxy gap. Worth shipping properly when there's bandwidth;
   not worth rushing.
10. **M9 Phases 2-6 (custom web chat UI)** — deferred.
11. **M5 (trip planning)** — personal-use polish, can slip.

**Sequencing rationale for what's not shipped:** The mobile gap
and the M6-B-not-yet-cut-over are real, but they're sequenced
deliberately. Stdio-first MCP setup validated the agentic-coding
workflow before investing in HTTPS infrastructure. The Pi-fed
analytics snapshot was intentional: shipped what was usable fast,
evolved toward MM-native later, consumer interface stays put.
