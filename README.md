# Magic Monitor

A serverless live ride-status dashboard, showtimes view, and
Pushover alerter for Walt Disney World — running on AWS for under
a dollar a month.

**Live:** https://magicmonitor.megillini.dev
[![tests](https://github.com/illinigirl/MagicMonitor/actions/workflows/test.yml/badge.svg)](https://github.com/illinigirl/MagicMonitor/actions/workflows/test.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

![Magic Kingdom analytics — hour×day-of-week heatmap and ride downtime ranking](docs/screenshots/analytics-park.png)

---

## What it does

Polls themeparks.wiki every two minutes for live wait times and
ride status across the four WDW theme parks. When a ride in your
favorites list goes down, comes back up, or its current wait drops
below what's typical for that hour-of-day, a Pushover notification
fires to every user who's both subscribed to that park and marked
that ride as a favorite.

A web dashboard at `magicmonitor.megillini.dev` shows live ride
status, park hours (including Early Entry and Extended Evening
Hours for deluxe / DVC guests), today's entertainment lineup, and
wait-time analytics — hour × day-of-week heatmaps and per-ride
downtime rankings drawn from millions of historical poll rows.

## Stack at a glance

| Layer | Choice | Why |
|---|---|---|
| Compute | AWS Lambda (Python 3.12) for the poller, Amplify SSR Lambda (Node 22) for the web app | Free tier covers both at 2-min poll cadence |
| Schedule | EventBridge cron | Native, no extra service |
| Storage | DynamoDB single table (`DisneyData`) | Serverless, free at this scale, schema documented in CDK |
| Read API | None — Next.js Server Components query DynamoDB directly | One fewer hop. Render-on-navigation is the right default for cheap reads |
| Write API | Next.js Route Handlers under `/api/me/*` | Same Amplify Lambda, scoped IAM grants on USER#/PARK# partitions |
| Auth | Amazon Cognito + Google IdP | Imports a pre-existing Cognito user pool by ID; MM adds its own app client |
| Notifications | Pushover HTTPS API | Family already uses it; ~$0/mo recurring |
| Agentic / MCP | stdio MCP (Claude Desktop) + HTTPS MCP (API Gateway → Lambda, FastMCP + Mangum) | Same tool surface over two transports; remote one lets Claude mobile hit the live data plane |
| MCP auth | Cognito OAuth (access-token JWT, JWKS verify) + custom RFC 7591 DCR proxy + sub allowlist | Standards-based remote-MCP auth without standing secrets; reuses the same imported pool |
| Analytics delivery | Nightly GitHub Action regenerates the snapshot from DDB → commits (web) + uploads to S3 (MCP Lambda) | Data refresh decoupled from code deploys; Lambda fetches fresh on cold start |
| Frontend | Next.js 16 + Tailwind 4 + React 19 (Turbopack) | Modern SSR; Server Components let us drop the API tier |
| IaC | AWS CDK (TypeScript) | One stack, custom domain, GitHub OIDC role, Cognito client, Amplify app |
| Hosting | AWS Amplify Hosting | SSR Next.js with custom domain; CloudFront-fronted |
| DNS | Cloudflare | CNAME to Amplify-managed cert |
| Observability | CloudWatch + X-Ray | Native to Lambda + Amplify |

## Architecture

```mermaid
flowchart TD
    Browser([Browser]) -->|TLS| CF[CloudFront / Amplify edge<br/>us-east-1 cert]
    CF --> SSR[Amplify SSR Lambda<br/>Node 22, us-east-2]
    SSR -.->|reads| DDB[(DynamoDB<br/>DisneyData<br/>single table)]
    SSR -.->|fetches| TPW[themeparks.wiki]
    Cog[Cognito User Pool<br/>imported by ID] -.->|OAuth via Cognito hosted UI| SSR

    EB[EventBridge ⏰<br/>every 2 min] --> Poll[Poller Lambda<br/>Python 3.12]
    Poll -->|fetches| TPW
    Poll -->|read + write| DDB
    Poll -->|fanout alerts| PO[Pushover API]

    Claude([Claude mobile /<br/>Desktop Connector]) -->|MCP over HTTPS + OAuth| APIGW[API Gateway<br/>HTTP API]
    APIGW --> MCP[MCP Lambda<br/>Python 3.12<br/>FastMCP + Mangum]
    MCP -.->|reads| DDB
    MCP -.->|fetch snapshot<br/>on cold start| S3[(S3<br/>analytics snapshot<br/>+ baselines)]
    Cog -.->|access-token JWT verify via JWKS<br/>+ RFC 7591 DCR proxy| MCP

    SSR -.reads.-> Snapshot[analytics-snapshot.ts<br/>shipped in repo<br/>millions of historical rows]
    Poll -.reads.-> Baselines[baselines.json<br/>per-ride-hour wait thresholds]
    Agg[Nightly aggregator<br/>GitHub Action] -.->|regen from DDB| Snapshot
    Agg -.->|upload| S3

    classDef aws fill:#232f3e,stroke:#ff9900,color:#fff
    classDef ext fill:#444,stroke:#888,color:#fff
    classDef data fill:#1a3a52,stroke:#4a8fc7,color:#fff
    class CF,SSR,EB,Poll,Cog,DDB,APIGW,MCP aws
    class TPW,PO,Claude ext
    class Snapshot,Baselines,S3,Agg data
```

The **stdio MCP server** (`mcp/server.py`) runs locally as a Claude
Desktop subprocess; the **HTTPS MCP server** (`mcp/server_http.py`,
deployed as the MCP Lambda above) is the same data plane reached
remotely by Claude mobile and the Desktop Custom Connector, gated by
Cognito OAuth. See [the HTTPS MCP transport](#https-mcp-transport-oauth-on-a-remote-data-plane) below.

**Single-tier read pattern** is deliberate. Next.js Server Components
talk to DynamoDB through the SSR compute role. Writes (per-user
profile, park subscriptions, favorite rides) are Next.js Route
Handlers in the same app, not a separate API Gateway / FastAPI tier.
The trade-off: the SSR compute role grows broader. The win: one
fewer Lambda, end-to-end TypeScript, and direct access to the auth
session without re-validating JWT cookies at an API edge.

## Demo

| | |
|---|---|
| ![Landing](docs/screenshots/landing.png) | ![Live park](docs/screenshots/park-live.png) |
| **Landing** — pick a park; live status shows current open hours and the per-park entry into both rides and showtimes | **Live ride status** — down rides surface above operating; favorites get a star; closed-park empty state suppresses stale wait times |
| ![Today at the park](docs/screenshots/park-today.png) | ![Analytics heatmap](docs/screenshots/analytics-park.png) |
| **Today at the park** — chronological showtimes with category-pill filters (parade, fireworks, stage, music, character meet) and live search | **Analytics** — hour × day-of-week heatmap, ride downtime ranking with three sort modes, drawn from 8.8M historical poll rows |

## Engineering judgment moments

### Single-table DynamoDB, deliberately

```
PK              | SK                    | Purpose
RIDE#<id>       | STATE                 | current ride state (overwrites)
RIDE#<id>       | HIST#<iso_ts>         | status-change history (90d TTL)
RIDE#<id>       | FORECAST#<polled_at>  | per-poll wait forecast (7d TTL)
RIDE#<id>       | DOWN_SINCE            | when this ride went down
RIDE#<id>       | COOLDOWN#DOWN         | 15-min alert dedup (TTL)
RIDE#<id>       | COOLDOWN#BACK_UP      | 15-min alert dedup (TTL)
RIDE#<id>       | COOLDOWN#STILL_DOWN   | 45-min follow-up alert dedup (TTL)
RIDE#<id>       | COOLDOWN#LOW_WAIT     | 90-min low-wait alert dedup (TTL)
USER#<sub>      | PROFILE               | name + Pushover user key
USER#<sub>      | FAV_RIDE#<id>         | favorite (denormalized park_key)
USER#<sub>      | PLAN#<iso_ts>         | accepted plan + outcome. TTL keyed to the trip day (a couple days past planned_for_date while pending; 1y once an outcome is recorded). Body carries ride_sequence (still-planned), completed_rides (rode, with actual_wait_min), dropped_rides (skipped, with reason), plus the multi-day fields planned_for_date / trip_id / active / activated_at / plan_window / created_by
USER#<sub>      | TRIP#<trip_id>        | multi-day trip header (name, start/end date, per-day park list) — groups its PLAN# days
PARK#<key>      | USER#<sub>            | park subscription (fanout target)
```

Most access patterns resolve to a Query or GetItem on
`PK = <prefix>#<id>` plus an SK predicate — the poller's per-park
fanout is one Query per park, per-user write paths PutItem /
UpdateItem on a known PK, and TTL handles cooldown expiry without a
sweeper job. Two GSIs exist, and both were added for the same reason:
a single-table Scan that was fine at first silently broke as the table
grew, and the fix was to Query an index instead.

- **`park_key-SK-index`** (PK `park_key`, SK `SK`) — powers the web
  reader's per-park ride list. Replaced a single-page Scan that began
  returning `[]` once WAIT# rows pushed the table past one 1 MB Scan
  page (a regression that silently rendered "0 attractions" for ~7
  days before it was caught).
- **`planned_for_date-index`** (PK `planned_for_date`, SK `SK`) — a
  *sparse* index: only PLAN# rows carry `planned_for_date`, so it
  indexes plans and nothing else. The poller Queries it to find today's
  active plans. Replaced a full-table Scan whose 50-page safety cap
  covered only ~8% of the table once it reached ~630 MB — so an
  activated plan's row usually sat past the cap and never fired alerts.

### Per-favorite alert intersection

A user gets a Pushover alert for "Big Thunder is DOWN" only if they
*both* (a) subscribed to Magic Kingdom *and* (b) marked Big Thunder
as a favorite. New users default to zero rides → zero alerts: no
welcome-spam, no surprise pings.

This was an explicit Phase 2 schema change. The poller's fanout
filter does:

```python
park_subscribers = db.get_park_subscribers(park_key)        # Query 1
favoriters = [s for s in park_subscribers
              if ride_id in db.get_user_favorites(s, park_key)]  # Query per user
```

At the current user count this is a handful of Queries per status
event. If user count grows past hundreds, the per-user favorites
move to a GSI on `FAV_RIDE#<ride_id>` and the fanout filter becomes
"for this ride, who has it favorited?" instead.

### Pre-aggregated analytics, not streams→Athena

The "millions of polling rows" behind the analytics page live as
a ~230 KB TypeScript module checked into the repo, not behind a
DynamoDB Streams → Firehose → S3 → Athena pipeline. Two reasons.

First, MM's poller writes only status *changes* to DDB (HIST# rows
on transition), not every poll. A streams pipeline would carry
the wrong-shaped data — transitions, not the granular hour-bucketed
wait values the analytics page needs.

Second, Athena earns its complexity at 10 GB+. The dataset here
is 1.5 GB; the Glue / IAM / partitioning surface, the per-query
cost, and the freshness-vs-cost calculus all skew against it at
this scale.

The data originated in a sibling Raspberry Pi project that's been
recording every-2-min wait snapshots into SQLite for a couple of
months. `tools/aggregate-analytics.py` reads that file, buckets
by (ride, hour-of-day) and (park, hour × day-of-week), and emits
the static module. Server Components import it directly. Zero AWS
data plane, zero per-request cost, instant render.

When freshness becomes load-bearing, the documented upgrade path
([PROJECT.md M6](PROJECT.md)) is hourly-bucketed wait writes into
a `RIDE#<id>/AGG#<hour>` DDB partition with a nightly re-aggregation
Lambda — about 1-2 days of work, no streams or Athena required.

### Short-wait alerts use the analytics layer

The most interesting feedback loop in the system: the historical
data that powers the analytics page also powers an alert type.

`tools/aggregate-analytics.py` runs once per data refresh. It
reads `.scratch/disney-pi-snapshot.db` (a sibling Pi project's
SQLite of every-2-min poll rows), buckets by ride and hour-of-day,
and writes two outputs:

- `web/src/data/analytics-snapshot.ts` — Server Components import this
- `infra/lambda/poller/baselines.json` — the poller imports this at
  cold start

The poller checks each operating ride's current wait against its
hour-bucket baseline. If `current ≤ min(30, 0.5 × typical)` and a
90-min cooldown isn't active, a low-wait Pushover fires. Only 38 of
the 88 tracked rides have baselines — for rides whose typical wait
is already short, alerting "this is a short wait" is meaningless.

### Agentic planner with cross-session feedback loop

MM is exposed as a set of MCP tools over two transports — a stdio
server for Claude Desktop and a deployed HTTPS server (see
[below](#https-mcp-transport-oauth-on-a-remote-data-plane)) that Claude
mobile and the Desktop Custom Connector reach over OAuth. The
interesting one is `get_planning_context` — a single tool call that
returns
per-ride live status + forecast + DOWN history + lat/lon + park
hours + weather + today-vs-forecast correction + park-wide DOWN
list + LL drop patterns + headliner showtimes for an arbitrary
list of rides. Ground truth in one round trip, the LLM reasons
over it.

A calibration feedback loop then closes across sessions:

- `record_plan(ride_sequence, show_selections, context, ...)` —
  eager-write at plan-acceptance time (TTL keyed to the trip day so
  unrecorded plans auto-clean). Captures Claude's predicted waits +
  arrival times alongside the plan itself.
- `record_plan_outcome(plan_id, aggression_rating, timing_rating,
  per_item_feedback, ...)` — lazy-update when the user reports
  outcomes ("we ran out of time before Mansion", "Big Thunder was
  60 not 40"). Bumps TTL to 1 year.
- `get_user_plan_history(...)` — returns recent plans **plus a
  server-computed `calibration_summary`**: aggression averages,
  timing distributions, per-ride / per-show prediction bias with
  sample sizes, confidence labels (`high` ≥5, `medium` 3-4, `low`
  <3), and ready-made interpretation strings the LLM paraphrases.

**The design pattern that matters:** the data plane does the math
(server-side aggregation, confidence thresholds, neutral-zone
filtering of small deltas), the LLM narrates the lesson. Mirrors
the live `today_vs_forecast` design from `get_planning_context` —
pre-computed numbers + interpretation strings in the response,
conversational application by the model. Server-side aggregation
keeps calibration version-controlled and rigorous; LLM-side
narration keeps the conversational feel of the agentic loop.

What the loop produces in practice — Claude opens a new planning
session, calls `get_user_plan_history`, and reads back something
like:

> *"User finishes with extra time on 3/5 recent plans (averaging
> ~45 min spare on those days) — pack today's plan more
> aggressively."*
>
> *"Big Thunder Mountain Railroad tends to wait LONGER than
> predicted (+17 min avg, high confidence on n=5). Scale this
> ride's prediction up by ~17 min for this user."*

Then it weaves that into today's plan ("your last 3 plans
finished with extra time, so I'm being more aggressive today")
without restating the calibration per ride. Over the stdio transport
this runs single-user for the family-use case (`user_id` defaults to
`"megan"`); over the HTTPS transport the calling user's identity now
arrives as the verified Cognito `sub` (see below), so writes can be
attributed per family member.

### Multi-day trip planner

The same write layer backs a future-dated, shared trip planner. You
build a trip ahead of time — `create_trip(days=[...])` mints a `TRIP#`
header plus one **dormant** `PLAN#` per day — and those days sit silent
until you arrive. On a trip day you pull the plan up
(`get_plan_for_day`), Claude re-evaluates it against live conditions
(`get_planning_context` — what's DOWN now, today's real forecast +
hours), and once you accept, `activate_plan` flips it ACTIVE — which is
what turns on that day's plan-disruption alerts. A dormant plan fires
nothing; activation is the gate, and it deliberately happens *after*
the live re-evaluation, not before. `record_plan` with a
`planned_for_date` builds a single future day the same way, and
`get_upcoming_trip` surfaces a trip in progress at session start.
Mid-day, `mark_ride_complete` / `remove_ride_from_plan` /
`add_ride_to_plan` keep the active plan honest so monitoring tracks
what's actually left. The trip space is shared across the family (one
`USER#megan` partition); each write is stamped with `created_by` from
the verified login — never a client-supplied id.

### HTTPS MCP transport: OAuth on a remote data plane

The stdio MCP server only reaches clients that can launch a local
subprocess (Claude Desktop). To bring the same tools to Claude mobile —
which only speaks to *remote* MCP servers — MM runs a second,
deployed copy of the server behind API Gateway, as a separable CDK
stack (`DisneyMcpStack`) that reads `DisneyData` by name and owns
nothing the main stack owns (rollback is `cdk destroy DisneyMcpStack`).

The auth is the interesting part. There's no standing secret; it's
Cognito OAuth end to end:

- **Access-token JWTs, verified per request** against the user pool's
  JWKS (RS256), checking issuer + expiry + `token_use`, then a
  hard-coded `sub` allowlist as the real gate — even a validly-signed
  token from a non-family user is rejected.
- **A custom RFC 7591 Dynamic Client Registration proxy.** MCP clients
  expect to self-register; Cognito doesn't expose DCR. A `/register`
  endpoint translates the RFC 7591 call into a scoped Cognito
  `CreateUserPoolClient` on the shared pool, and the RFC 8414 / 9728
  discovery endpoints advertise where to go. So a fresh client walks
  discovery → registration → authorize → token → `/mcp` with no manual
  setup.
- **Stateless by necessity.** Lambda doesn't preserve session state
  across invocations, so the streamable-HTTP transport runs in
  stateless mode — each request is self-contained.

The Lambda reads across DynamoDB but has *scoped* write access —
`Put`/`Update`/`Delete` constrained by an IAM `LeadingKeys` condition
to the `USER#*` / `PARK#*` partitions (plans, trips, subscriptions),
mirroring the web SSR role — so the trip-planner write tools persist
plans without being able to touch ride-state rows. It pulls the 1.2 MB
analytics snapshot from **S3 on cold start** (rather than bundling it
into the deploy artifact) so the nightly regeneration reaches it
without a redeploy — data freshness decoupled from code-deploy cadence.

### Plan-aware alerts on two axes

The "system noticed something that invalidates your plan" loop fires
along two independent axes, both reading from the same active-plan
lookup the poller runs at the top of every invocation (a Query against
the `planned_for_date-index` GSI — formerly a full-table Scan).

**Axis 1 — per-ride disruption.** When a ride in TODAY's plan
transitions DOWN or BACK UP, every user with that ride in an active
plan gets a Pushover — regardless of whether they favorited it.
Being in today's plan is a stronger "I care right now" signal than
generic favoriting. Deduped against the favoriter fanout so one event
never produces two pings to the same user.

**Axis 2 — park-wide weather shift.** When Open-Meteo's 6-hour
forecast newly contains a thunderstorm code (95/96/99) that wasn't
there on the previous poll, every user with an active plan today
gets a Pushover. Storm = lightning hold = Disney pauses outdoor rides,
so the alert IS the replan trigger. Per-(user, plan) cooldown
prevents re-firing while the storm stays in the window; a distinct
second storm window later in the day can re-alert.

```python
# index.py — one GSI query yields both views, both fanouts dedup per user
plan_ride_index, active_plans = db.build_active_plan_ride_index(today)
# Axis 1: ride goes DOWN → look up plan targets by ride identifier
plan_targets = db.lookup_plan_targets(plan_ride_index, ride_id, name)
# Axis 2: storm enters forecast → fan out to every active plan today
if active_plans:
    new_weather = weather.fetch_forecast()
    if shift := weather.detect_storm_shift(prior_weather, new_weather):
        for plan in active_plans:  # dedup by user_id below
            ...
```

Pattern choice — narrow over noisy: precipitation_chance jumps were
considered and rejected for v1. Florida afternoon rain shifts up and
down all day, so a precip-jump trigger would generate noise. Storm
codes are an actual operational event; precip-jumps mostly aren't.
The trigger can broaden later if usage shows real plan-disruption
moments slipping through.

Cost gate: weather is fetched only when at least one active plan
exists for today. Off-season days with no plans skip the HTTP call
entirely.

### Trust-policy override in CDK

Amplify Hosting's L2 alpha generated a service-role trust policy
with `aws:SourceArn` and `aws:SourceAccount` conditions on it (the
"AWS best practice" pattern). On a later `cdk deploy` that touched
the role, Amplify's runtime began assuming the role through an
internal service-role chain whose source ARN no longer matched —
builds silently failed with "Unable to assume specified IAM Role"
and no console banner. `disney-stack.ts` now reaches into the L2's
construct tree and overrides the role's `AssumeRolePolicyDocument`
back to a no-conditions form. Defensive no-op today; if a future
alpha re-introduces the conditions, the override re-applies on the
next deploy.

Full debug log under [RUNBOOK.md → "Lesson 5 — round 2"](RUNBOOK.md#lesson-5--round-2-trust-policy-conditions-can-come-back-on-deploy).

### AWS SDK bundled inline

Next.js 16 + Turbopack + pnpm + `serverExternalPackages` listing
the AWS SDK produced a hash-suffixed module name at runtime that
pnpm's nested store didn't expose. Result: `/parks/[park]` returned
bare 500s with no CloudWatch trail (Amplify-managed SSR Lambda
logs aren't customer-accessible). Diagnostic technique that
worked: a temporary `/api/debug/ddb` route handler with a try/catch
around a dynamic `import()` of the SDK, returning the actual error
as JSON.

Fix: drop the AWS SDK from `serverExternalPackages` — Turbopack
bundles it inline. Costs ~600KB extra in the SSR chunk; meaningless
at our scale. RUNBOOK Lesson 3 has the long form.

## Cost

| Item | Monthly |
|---|---|
| Lambda invocations (~22k poller + a few hundred SSR) | $0 (free tier) |
| DynamoDB on-demand (~50k req, ~50MB storage) | <$0.10 |
| EventBridge | $0 |
| Amplify Hosting (SSR + bandwidth at family traffic) | <$0.20 |
| CloudFront / data transfer | <$0.05 |
| Cognito (imported pool, own app client) | $0 |
| Pushover | $5 one-time (already paid) |
| **Total recurring** | **~$0.30/mo** |

Headroom for adding hourly-bucketed wait recording (M7+) at the
most aggressive cadence is ~$3/mo — well under the project's
&lt;$5/mo budget. PROJECT.md M7+ section documents the trade-offs.

## Local setup

The interesting parts are deployed; this section is the ops
escape hatch.

### Prerequisites

- AWS account with CDK bootstrapped in `us-east-2`
- Node.js 20+ and `pnpm`
- Python 3.12 on PATH
- Pushover account + an "application" registered for the alerts
  (https://pushover.net/apps/build) — note the **App Token**
- Pushover **User Key** for each subscriber

### One-time

```bash
# Install CDK dependencies
cd infra && npm install

# Seed Pushover credentials in SSM
aws ssm put-parameter --profile <yours> --region us-east-2 \
  --name /disney/pushover/app_token --type SecureString \
  --value '<your-disney-app-token>'

# Deploy the stack
npx cdk deploy --profile <yours>

# Outputs include the table name and Lambda function name —
# copy them for the verification steps below.
```

`web/.env.local.example` shows the env shape needed for `pnpm dev`
locally (Cognito client secret, NextAuth URL, etc.).

### Verification

```bash
# Trigger a manual poll
aws lambda invoke --profile <yours> --region us-east-2 \
  --function-name <PollerFunctionName-from-cdk-output> \
  --cli-binary-format raw-in-base64-out --payload '{}' \
  /tmp/disney-poll.json && cat /tmp/disney-poll.json

# Tail logs live
aws logs tail /aws/lambda/<PollerFunctionName> --follow --profile <yours>

# Smoke-test the live URL
for path in / /parks/magic_kingdom /parks/epcot /parks/hollywood_studios \
            /parks/animal_kingdom /api/auth/providers; do
  curl -s -o /dev/null -w "  $path → %{http_code}\n" \
    -L https://<your-domain>$path
done
```

`RUNBOOK.md` has the full operational playbook — lessons learned,
deploy hygiene, common debug commands, and known follow-ups.

## What's left

Captured in [`PROJECT.md` → Roadmap](PROJECT.md) and
[`RUNBOOK.md` → Known follow-ups](RUNBOOK.md#known-follow-ups-low-priority):

- **Per-type alert toggles on `/me`** — currently every alert
  recipient gets DOWN, BACK UP, STILL DOWN, and LOW WAIT.
  PROJECT.md M7+ has the natural opt-in shape.
- **M9 Phase 2: embedded agentic chat (~2-3 days)** — Phase 1 (the
  remote OAuth MCP server above) is live; Phase 2 brings the planner
  *into the web app* for users who don't run Claude Desktop, calling a
  shared tool layer via Anthropic API tool-use with prompt caching and
  per-user token budgets.

Recently shipped: **M5** (the multi-day shared trip planner above —
build → activate → re-evaluate, live across both MCP transports with
the poller activation gate and a sparse GSI backing active-plan
lookup), **M6-B** (analytics now regenerate nightly from DynamoDB
rather than a frozen Pi snapshot), and **M9 Phase 1** (the
[HTTPS MCP transport with Cognito OAuth](#https-mcp-transport-oauth-on-a-remote-data-plane),
live on Claude mobile + the Desktop Custom Connector).

## Deliberately skipped

- **Lightning Lane purchase tracking** — no public API exists for
  personal LL inventory; would require manually syncing from the
  Disney app, which doesn't generalize past the original user.
- **SMS notifications** — 10DLC compliance is $130/yr + a two-week
  vetting period + EIN paperwork for zero architectural depth over
  Pushover. Pushover ships in a day; SMS would burn three weeks of
  calendar time on regulatory work.

## License

[MIT](LICENSE). Copy, fork, deploy your own — attribution appreciated
but not required.
