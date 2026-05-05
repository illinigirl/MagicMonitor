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
| Frontend | Next.js 16 + Tailwind 4 + React 19 | Same stack as Watchtower for portfolio consistency |
| Type stack | Fraunces + Inter + JetBrains Mono | Mirrors Watchtower; shared visual lineage |
| Color palette | Castle: deep navy + pink + gold (OKLCH) | Disney-feeling without using any actual Disney TM |
| Auth | Amazon Cognito + Google federation | Reuses Watchtower's user pool via second app client (no new Google OAuth setup) |
| IaC | AWS CDK (TypeScript) | Same as Watchtower; reuses Python Lambda bundling helper |
| Hosting | AWS Amplify | Same as Watchtower; SSR Next.js with custom domain |
| Observability | CloudWatch + X-Ray | Native AWS, matches Watchtower |

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
- **Quality bar**: must look like the Watchtower demo — same visual
  identity treatment, same auth flow, same deployment hygiene. The
  two projects reinforce each other in the portfolio.

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

#### M2-B — Auth + production deploy (~2-3 hrs work + cert wait)
- Add Amplify app + custom domain (`magicmonitor.megillini.dev`) +
  ACM cert in CDK stack
- Add Cognito 2nd app client on Watchtower's existing user pool
  (no new Google OAuth setup — reuses Watchtower's federation)
- Cloudflare CNAME for `magicmonitor.megillini.dev` (manual)
- NextAuth wiring for Cognito provider
- Sign-in button + protected routes
- *Demo-able:* sign in with Google at the live URL, see the dashboard

### Future

#### M3 — Per-user dashboard pages (~1 week)
- Profile page: name + Pushover user key entry (writes USER#<sub>/PROFILE)
- Park toggles: which parks alert me right now (writes PARK#<key>/USER#<sub>)
- Favorite-rides grid: checkbox grid per park, alerts only fire for
  rides in your favorites (writes USER#<sub>/FAV_RIDE#<id>)
- New-user gating: show "Pick which rides to watch" first-run flow
  before any alerts fire (default: zero rides → zero alerts)
- *Demo-able:* sign up as a new user, paste a Pushover key, pick
  favorites, get alerted within 2 min when one of them changes status

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
- Low-wait alerts (needs ~1 week of history to compute baselines)
- Per-ride alerts (not just per-park)
- Email digest summary at end of trip
- Public read-only stats page (no sign-in needed)
- Mobile push notifications via Web Push (alternative to Pushover)

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
