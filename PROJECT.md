# Magic Monitor

A serverless live-status dashboard for Walt Disney World, built as the
sibling project to Watchtower for the same FDE portfolio.

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

- **Primary**: hiring managers reviewing portfolios for FDE / cloud
  engineering roles. Architecture, deployment, and code quality matter
  alongside the working product.
- **Secondary**: real users — the family uses this on actual Disney
  trips. Megan + her husband + her sister all subscribe and get
  alerted when their planned rides go down.

## Tech stack (high level)

| Layer | Choice | One-line rationale |
|---|---|---|
| Compute | AWS Lambda (Python 3.12) | Cheap at low traffic; matches Watchtower's tier |
| Schedule | EventBridge | Native AWS cron, no separate service needed |
| Storage | DynamoDB single-table | Serverless, free at this scale, interview-popular |
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
- **Quality bar**: stands on its own as a portfolio demo — TLS,
  custom domain, Google sign-in, deploy hygiene, README with cost
  breakdown and architecture diagram. Shares visual lineage and the
  Cognito user pool with Watchtower so they reinforce each other if
  both ship, but Magic Monitor must be demoable independently.

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

#### M3 — Per-user dashboard pages (~1 week)
- Profile page: name + Pushover user key entry (writes USER#<sub>/PROFILE)
- Park toggles: which parks alert me right now (writes PARK#<key>/USER#<sub>)
- Favorite-rides grid: checkbox grid per park, alerts only fire for
  rides in your favorites (writes USER#<sub>/FAV_RIDE#<id>)
- New-user gating: show "Pick which rides to watch" first-run flow
  before any alerts fire (default: zero rides → zero alerts)
- Implementation: Next.js Route Handlers under `web/src/app/api/me/`
  (NOT a separate FastAPI service). Each handler calls `auth()` to
  get the Cognito sub, then writes through `@aws-sdk/lib-dynamodb`.
  CDK adds scoped `UpdateItem`/`PutItem` permissions to the Amplify
  SSR compute role — same role that reads in M2-B, just broader
  conditions. Revisit this if MM ever needs a public/mobile API
  that warrants the APIGW boundary.
- *Demo-able:* sign up as a new user, paste a Pushover key, pick
  favorites, get alerted within 2 min when one of them changes status

### Future

#### M4 — Showtimes (~2-3 days)
- Same themeparks.wiki API returns SHOW entities alongside attractions
- Per-park "Today at the park" section: parades, fireworks, stage shows
  with start times for the day
- Examples: Festival of Fantasy Parade, Happily Ever After, Fantasmic,
  Beauty and the Beast Live on Stage
- No alerts (shows don't break) — pure read-side UI
- *Demo-able:* "What time is Fantasmic tonight?" answered in one glance

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

#### M6 — Analytics (~1 week, the impressive layer)
- Port `_api_analytics_rides` from disney_dashboard.py:
  - Per-ride downtime % over last 30 days
  - Hourly average wait pattern per ride
  - Day-of-week pattern
  - Park-wide hour × day-of-week heatmap
- Decision deferred until M5 ships: stay on DynamoDB with on-the-fly
  aggregation, or migrate analytics to Aurora Serverless v2 / S3+Athena
- *Demo-able:* "Here's three months of data showing when Test Track
  is most likely to be down — useful for trip planning"

#### M7+ — Polish (grab bag)
- Low-wait alerts (needs ~1 week of history to compute baselines).
  When this ships, also add per-type toggles to `/me`
  (`down_up` / `short_wait`) — until short-wait exists there's only
  one alert type, so type-pickers are moot. Lightning-lane alerts
  remain out of scope pre-demo (no public LL purchase API).
- Show alerts: opt-in per show, fires N min before start time. Needs
  a daily showtime poll (stable through the day, no per-2-min churn)
  plus a per-user `SHOW_ALERT#<show_id>` row mirroring the FAV_RIDE
  shape. Past-demo scope.
- Per-ride alerts (not just per-park)
- Email digest summary at end of trip
- Public read-only stats page (no sign-in needed)
- Mobile push notifications via Web Push (alternative to Pushover)

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
worth surfacing in interviews.

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

## Recommended ordering rationale

- **M2-B before M3:** M3 (per-user toggles) requires auth. M2-B
  is "make auth exist." Hard to do M3 without it.
- **M4 (showtimes) before M5 (trip planning):** Trip planning
  surfaces *what's happening on each day of your trip*, and showtimes
  are a big part of that. Building showtimes first means trip planning
  has more to render.
- **M6 (analytics) is interview-essential:** It's the most distinctive
  part of the whole project — months of polling history → heatmaps and
  downtime stats no one else has. The "why this and not Touring Plans"
  answer.

## Demo-prep priority order

If interview prep is the constraint, ship in this order:

1. **M2-B** — gets a live URL on a portfolio. Single most
   demo-valuable milestone.
2. **M3** — multi-user signup, lets interviewer touch the demo.
3. **M6** — impressive analytics that other tools don't have.
4. **M4 + M5** — personal-use polish (real trip value, smaller
   demo lift).
