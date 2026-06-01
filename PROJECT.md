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

#### 2026-05-31 — M9 Phase 1 session 2B: Cognito OAuth wired + bearer removed

Closes M9 Phase 1. The HTTPS MCP transport from session 1 now
authenticates via Cognito OAuth (access tokens RS256-verified
against the user pool's JWKS, with a hard sub allowlist on top)
instead of the placeholder shared bearer secret. Claude Desktop's
Custom Connector and Claude mobile both verified end-to-end
against the live API — mobile call worked off home WiFi, which
was the original driver for M9 Phase 1.

**What shipped:**
- `mcp/server_http.py` — hard-replaced `_BearerAuthMiddleware`
  with `_CognitoJwtMiddleware` (verifier injectable for tests).
  Added 3 public routes handled inside the middleware:
  `/.well-known/oauth-protected-resource` (RFC 9728),
  `/.well-known/oauth-authorization-server` (RFC 8414),
  `POST /register` (RFC 7591 DCR via `dcr_proxy.register_client`).
  OPTIONS bypass preserved.
- `mcp/lambda_handler.py` — deleted `_bootstrap_bearer_secret`.
  Cognito config rides plain Lambda env vars (pool ID, region,
  domain URL, allowlist, public base URL — none are secrets).
- `infra/lib/disney-mcp-stack.ts` — swapped SSM bearer IAM grant
  for scoped `cognito-idp:CreateUserPoolClient` on the one user
  pool ARN. Added 5 env vars; pulls allowlist from CDK context.
- `infra/cdk.json` — `mcp_allowed_subs` = Megan's + Jim's
  Cognito subs (public identifiers, fine in source).
- `mcp/tests/test_server_http_oauth.py` (new) — 20 tests:
  metadata route shapes, DCR happy + invalid-payload + non-JSON
  paths, middleware bypass list (well-known + register + OPTIONS),
  auth gate (missing/non-bearer/verifier-rejects/verifier-throws/
  valid), exact-path matching (no misroute on POST to well-known
  or GET to /register).
- **SSM `/disney/mcp/bearer_secret` deleted post-deploy.** No code
  references remain.
- **OAuth metadata design (pragmatic DCR-proxy quirk):**
  `issuer` in the AS metadata is our API URL, but `authorization_
  endpoint` / `token_endpoint` point at Cognito's hosted UI.
  Clients follow `jwks_uri` (Cognito's) for signature verification,
  not strict issuer matching. Locked decision: keep until a
  real-world client breaks on it.

**Verified live (curl smoke):**
- `GET /.well-known/oauth-protected-resource` → RFC 9728 shape ✓
- `GET /.well-known/oauth-authorization-server` → RFC 8414 shape ✓
- `POST /register` (no auth) → 201 + real Cognito client_id ✓
- Browser `/authorize` → Google → Cognito redirect with code ✓
- `POST /token` (PKCE) → access_token with sub = Megan's UUID ✓
- `POST /mcp` with valid token → 200 + `tools/call
  hello_magic_monitor` returns greeting ✓
- `POST /mcp` no header → 401 `missing or malformed Authorization` ✓
- `POST /mcp` bad bearer → 401 `invalid token` (generic, doesn't
  leak which check failed) ✓
- Allowlist count from CDK output = 2 (Megan + Jim) ✓

**Verified live (Claude clients):**
- Claude Desktop Custom Connector — added by URL, auto-discovered
  metadata + DCR + Cognito login, all 3 tools available ✓
- Claude mobile off home WiFi — same flow, tool call works ✓

**Test status:** 70 mcp tests green (26 baseline + 20 new HTTP
OAuth + 24 calibration/forecast). No eval re-run needed (stdio
`server.py` unchanged).

**Architectural note — Lambda env var write-after-Lambda-construct.**
`MCP_PUBLIC_BASE_URL` (the API URL the OAuth metadata advertises
as `resource` + `issuer`) is added via `mcpFn.addEnvironment(...)`
*after* the HTTP API is created, since the Lambda is constructed
before the API exists. Clean enough; alternative would be moving
the API construction up.

**No regression:** stdio `server.py` + Claude Desktop's existing
`magic-monitor` server unaffected (different module, different
process). The new Custom Connector "Magic Monitor (Remote)" lives
alongside it.

**Cost impact:** unchanged. No new always-on resources;
allowlist + DCR are pure compute.

**Deferred follow-ups:**
- Cognito app-client cleanup (smoketest clients accumulate — not
  a real risk at the 1000-client limit / 3 users).
- DCR rate-limit on `/register` (allowlist on JWT verify is the
  real gate).
- HTTPS port of write tools + analytics tools + planning context
  (session 3+ scope; needs write IAM grants).

#### 2026-05-26 — M9 Phase 1 session 1: HTTPS MCP transport on AWS (v1, bearer-token)

First half of the mobile-MCP arc. Ships an end-to-end HTTPS MCP
transport on AWS so Claude mobile (and any remote MCP client) can
eventually hit the same data plane that Claude Desktop hits via
stdio. Session 1 lands the transport + IAM + read tools; OAuth +
mobile config is session 2.

**Duplicate-first.** The stdio MCP server (`mcp/server.py`)
shipped 2026-05-10 + 22 tools + production traffic — is left
bit-for-bit untouched (apart from one perf-follow-up comment).
`mcp/server_http.py` is a verbatim copy of the v1 tool subset
with three additions: FastMCP `stateless_http=True`, disabled
DNS rebinding protection (API Gateway hostnames don't match the
default allowlist), and a bearer-token starlette middleware.
The refactor to a shared `_tool_impls.py` is later cleanup once
the HTTPS path is validated on mobile.

**v1 tool subset (read-only):**
- `hello_magic_monitor` — sanity ping
- `get_live_ride_status` — single-ride lookup with substring match
- `get_park_live_status` — paginated DDB Scan + filter, sorted DOWN-first

The full plan-feedback loop and analytics-snapshot-bundled tools
are deferred to session 3+ (after OAuth lands and write-side IAM
is added).

**What shipped:**
- `mcp/server_http.py` (new) — duplicated v1 tools + bearer-token
  middleware. ~280 LOC.
- `mcp/lambda_handler.py` (new) — Lambda entry, fetches bearer
  secret from SSM at cold-start, wraps each ASGI invocation in
  `session_manager.run()` with a `_has_started=False` reset (see
  "Architectural notes" below).
- `mcp/requirements.txt` — adds `mangum==0.18.0` for ASGI→Lambda
  adapter.
- `infra/lib/disney-mcp-stack.ts` (new) — net-new CDK stack:
  Lambda + API Gateway HTTP API + IAM (DDB read-only on
  `DisneyData`, SSM read on the bearer-secret param). Completely
  separable from DisneyStack — `cdk destroy DisneyMcpStack`
  removes everything net-new without touching the Amplify app,
  poller, or web Cognito client.
- `infra/bin/disney.ts` — registers the new stack alongside
  DisneyStack.
- `mcp/server.py` — one-liner perf-follow-up comment noting that
  the paginated Scan in `get_park_live_status` could later move
  to the GSI that web/ already uses (commit `4fd17bc3`,
  2026-05-25).

**Bootstrap (one-time):** SSM SecureString param
`/disney/mcp/bearer_secret` created manually with
`openssl rand -base64 32`. Never lands in CDK / CloudFormation /
git — the Lambda role grants `ssm:GetParameter` on that exact
name and the handler fetches at cold start.

**Verified live (curl smoke):**
- Auth gate: no token → 401, wrong token → 401, right token → 200
- MCP `initialize`: returns Magic Monitor (HTTP) v1.27.1
- `tools/list`: returns all 3 tools with full schemas
- `tools/call hello_magic_monitor`: returns greeting
- `tools/call get_live_ride_status ride_name=big thunder`: returns
  live STATE row (50 min wait, last_seen ~2 min ago)
- `tools/call get_park_live_status park=MK`: returns 35 rides
  sorted DOWN-first
- Four consecutive warm-invocation calls — proved the per-request
  session-manager reset works across the same Lambda container

**Zero regression on stdio path:** all 5 eval cases in
`mcp/evals/` pass (~$0.30, ~8.6 min). Claude Desktop demo is
unaffected.

**Architectural notes (the gnarly bit, documented for future-me):**

The MCP SDK's `StreamableHTTPSessionManager.run()` is designed
for long-running servers (uvicorn-style): one lifespan startup
opens an anyio task group that lives forever. The SDK enforces
this with a `_has_started` flag and refuses second entry.

Lambda + Mangum doesn't fit that model — Mangum invokes the
lifespan cycle per-invocation (not once-per-cold-start). So:
- `lifespan="off"` → task group never created → every request
  500s on `"Task group is not initialized"`
- `lifespan="on"` → second invocation 500s on
  `"can only be called once per instance"`
- Wrap-per-request with manual `session_manager.run()` → first
  request works, second fails (same single-use guard)

Fix landed in `lambda_handler.py`: Mangum `lifespan="off"` +
wrap each request in `session_manager.run()` + reset
`_has_started=False` after exit. Small intrusion into SDK
internals; cleaner alternatives (moving to AWS Lambda Web
Adapter, re-constructing FastMCP per request) were more rework
than session 1 warranted.

The full debug arc is documented in the `lambda_handler.py`
module docstring so the next person seeing the same errors
doesn't repeat it.

**Open follow-up: Cognito vs Clerk vs Auth0 for session 2 OAuth.**
The original session 2 estimate (6-10 hr) assumed Cognito as the
OAuth provider + a DCR proxy in front (Cognito doesn't support
Dynamic Client Registration natively). After looking at how
`schuettc/mixcraft-app` handled the same problem — they use
Clerk, which supports DCR out of the box and made their OAuth
flow significantly simpler. Worth a 15-min decision spike before
session 2 starts: stick with Cognito + write the DCR proxy, or
adopt Clerk (paid, adds a 2nd auth system unless web app migrates
too) or Auth0 (similar). Recommendation TBD — defer to a fresh
decision moment.

**Cost impact:** ~$1-2/mo (API Gateway HTTP API @ $1/M requests
+ Lambda free tier covers usage at the family scale).

#### 2026-05-25 — LOW_VS_FORECAST alert: today-aware low-wait baseline

Adds a second baseline on the low-wait alert path. LOW_WAIT
(historical) catches all-time anomalies; LOW_VS_FORECAST
(today-aware) catches heavy-crowd-day opportunities the
historical baseline blinds you to — e.g., Big Thunder at 40 min
when today's forecast said 65. Both signals share a single
cooldown row so a ride gets one low-wait-class push per window
regardless of which baseline triggered, with body text that
adapts to whichever fired.

**What shipped:**

- `infra/lambda/poller/forecast_signal.py` (new) — three pure
  functions: `find_forecast_for_hour` extracts today's predicted
  wait for the current ET hour from a ride's in-band forecast
  array; `compute_park_load_ratio` aggregates a wait-weighted
  `sum(actual)/sum(predicted)` across qualifying operating
  rides (same shape as MCP's `_compute_load_vs_forecast`);
  `should_fire_low_vs_forecast` applies the four-gate threshold
  test (sample size, park-quiet, absolute-gap, ride-vs-park
  ratio). All thresholds env-var configurable.
- `infra/lambda/poller/index.py` — computes `park_load_ratio`
  once per park from the in-memory upstream payload (no extra
  DDB read — simpler than the PROJECT.md design assumed since
  the same payload `record_forecast` writes is already
  available). Restructures the LOW_WAIT branch into a shared
  evaluation that runs both baselines under one
  `COOLDOWN#LOW_WAIT` gate.
- `infra/lambda/poller/notifier.py` — `alert_low_wait` signature
  extended with optional `typical_wait_mins` /
  `forecast_wait_mins`. Body text composes adaptively:
  - both → "Typical for this hour: ~70 min. Today's forecast: 65 min."
  - typical only → existing wording (no behavioral change)
  - forecast only → "Today's forecast: 70 min." (no historical
    comparison)
- `infra/lambda/poller/tests/test_forecast_signal.py` (new) —
  18 tests pin the threshold math and aggregation. Five gates
  tested independently + the killer case (heavy day + ride
  beating park-wide load by ≥25% with ≥15 min gap). Plus 5
  aggregation tests covering operating-only filtering, noise
  floor, missing forecast, empty park.

**Tunable thresholds (env-var defaults):**
- `LOW_VS_FORECAST_MIN_PARK_RATIO` = 0.9 (quiet-day suppression)
- `LOW_VS_FORECAST_RIDE_RATIO_MULT` = 0.75 (ride beats park by
  ≥25%)
- `LOW_VS_FORECAST_MIN_ABS_GAP` = 15 min (meaningful gap)
- `LOW_VS_FORECAST_MIN_RIDES_SAMPLED` = 5 (sample-size floor —
  added during design, not in original spec, to prevent early-
  morning noise when only 3-4 rides are operating)
- `LOW_VS_FORECAST_MIN_PREDICTED_WAIT` = 10 min (per-ride noise
  floor matching MCP's `_compute_load_vs_forecast`)

**Design refinements during build:**
- *In-memory data plane, no DDB read.* PROJECT.md design
  assumed the poller would Query DDB for the latest FORECAST#
  row per ride. But the poller already has each ride's
  forecast in memory — it's literally the same payload it
  passes to `record_forecast`. Operating directly on
  `attractions[i]["forecast"]` cuts a per-poll DDB read and
  removes a failure mode.
- *Added sample-size floor.* The original spec didn't include
  a minimum n; with only 3-4 rides sampled (early morning), the
  park_ratio is too noisy to drive an alert. Mirrors the MCP
  planner's same noise-floor guard. Default 5.
- *Cooldown SK kept as `LOW_WAIT`.* Could have renamed to
  `LOW` for accuracy — the row now gates two signals. Kept
  the existing key because the alternative would mean live
  rows existing under both keys during the 90-min TTL window
  for no functional benefit.

**Known limit (worth tuning later):** ~13 days of FORECAST#
data as of 2026-05-25 (Phase A2 shipped 2026-05-10). Threshold
defaults are first-pass values, not bench-derived. Plan to
revisit after ~30 days of accumulated observation; env vars
above let tuning be a config change, not a redeploy.

#### 2026-05-25 — M6-B Phase 3: nightly aggregator regen via GitHub Actions

Closes M6-B fully. Replaces the manual run + commit + push loop
with a scheduled workflow that runs the aggregator against the
live DDB table every night and commits the snapshot diff back
to main if it changed. Amplify auto-deploys the new snapshot
~3-5 min later. The M6-B milestone (analytics now AWS-native,
end-to-end, including its own regeneration) is closed.

**What shipped:**
- `tools/aggregate-analytics.py` — default `--source` flipped
  `sqlite` → `ddb`. Bare runs now use the live table; the
  sqlite path remains for historical diffing while the Pi
  runs in parallel as a backup data source. Snapshot regenerated
  from DDB as part of the flip (`d7c63f62`).
- `tools/aggregate-analytics.py` — `_ddb_table()` now picks
  the credential source from the env: default credential chain
  in CI (where `AWS_ACCESS_KEY_ID` is populated by
  aws-actions/configure-aws-credentials), `watchtower` SSO
  profile locally. Same script works in both contexts without
  flags (`1531995c`).
- `.github/workflows/aggregate.yml` (new) — cron at 08:00 UTC
  (04:00 EDT / 03:00 EST) + `workflow_dispatch`. Assumes the
  existing `MagicMonitorGithubDeploy` OIDC role (no CDK change
  needed — `AdministratorAccess` already covers DDB Scan +
  Query), runs the aggregator, commits the diff as
  `github-actions[bot]` if anything changed, pushes back to
  main with `git pull --rebase` to close the human-push race
  window (`1531995c`).
- Workflow guardrails (`7712477f`):
  - `dry_run` `workflow_dispatch` input — manual runs can
    exercise the full path (OIDC + DDB + aggregator + diff
    detection) without committing a noise snapshot. Used to
    smoke-test the workflow before relying on the cron.
  - `$GITHUB_STEP_SUMMARY` step — surfaces outcome
    (`unchanged` / `dry-run-diff` / `committed` / `failed`)
    plus the last 25 lines of aggregator output on the run
    page, so a passing nightly is also a visible heartbeat.
  - Concurrency group prevents overlapping manual + cron runs.

**Verification:** dry-run smoke test passed end-to-end in 1m17s
(GitHub Actions run `26412418734`). OIDC assume → DDB read →
aggregator (65.3s, faster than local same-region) → diff
detection → step summary populated → no commit, no push.

**Design rationale:** Manual regen was acceptable during M6-B
Phase 4 cutover verification. Persistent staleness post-cutover
isn't — the live `analytics-snapshot.json` should track the
data the poller is collecting, not freeze on whatever day
someone last remembered to regen by hand. Nightly is overkill
for the data's actual rate of change (week/month patterns) but
is the lowest-friction automation pattern: no CDK change, no
new AWS resource, uses the OIDC role that already exists, and
the workflow file itself is the documentation of "how the
analytics snapshot gets fresh." Lambda + EventBridge was the
AWS-native alternative; passed on it because the GitHub Action
gets the commit on-tree (Amplify auto-deploys from main) with
zero additional infrastructure.

**Follow-up surfaced during the work:** GitHub flagged that
`aws-actions/configure-aws-credentials@v4` runs on Node.js 20,
which gets force-deprecated June 2026. Non-blocking; bump to
`@v5` when it ships.

#### 2026-05-25 — M6-B Phase 4: Pi-to-DDB data plane cutover + duration-based analytics

Closed the M6-B milestone (analytics now AWS-native end-to-end).
Two coupled data-plane changes and a structural shift in how the
analytics aggregator measures downtime.

**What shipped:**
- `tools/backfill-pi-to-ddb.py` — added `--mode {wait,hist,both}`.
  HIST# mode walks the Pi snapshot per-ride and emits a DDB
  HIST# row for every status transition, matching the live
  poller's `record_status_change()` shape. Stamped 5-year TTL so
  backfilled rows survive past the live poller's old 90-day
  default (`6f3e96c7`). Production backfill: 2.4M WAIT# rows
  (~$3) + 13K HIST# transitions (~$0.02).
- `infra/lib/disney-stack.ts` — bumped `HISTORY_RETENTION_DAYS`
  90 → 1825 days, so live HIST# rows written post-cutover match
  the backfill TTL. Deployed CDK before running HIST# backfill
  to avoid a ~3-month gap window (`6f3e96c7`).
- `tools/aggregate-analytics.py` — dual-source rewrite with
  `--source {sqlite,ddb}` flag. DDB mode prefetches WAIT# + HIST#
  via an 8-way thread pool (~25s for 2.5M items), then runs the
  same four passes the SQLite path runs. Default stays `sqlite`
  for now; flip to `ddb` once verified in the running site
  (`b9c1b870`).
- `tools/aggregate-analytics.py` — same commit also switched all
  ride_active / ride_down / rh_* / rdh_* / pdh_* accumulators
  from poll counts to wall-clock minutes. Surfaced + fixed a
  long-standing measurement bug (see TESTING.md "Cadence-
  dependent ratio metrics"). Both paths now produce cadence-
  independent downtime metrics.
- Regenerated `web/src/data/analytics-snapshot.json` from DDB
  source covering 2026-03-10 → 2026-05-25. 20 walkthrough/
  gallery rides (Tree of Life, Wilderness Explorers, Main
  Street Vehicles, etc.) dropped from output — they have no
  HIST# transitions and no WAIT# rows in DDB, since their
  status never changes and they have no wait_mins. Web UI
  rendered no useful values for these rides; clean drop.
- `web/src/app/analytics/page.tsx` — refreshed footer copy. No
  longer claims DDB-backed aggregates are "deferred for scope"
  (they shipped). Minimal honest version: date + source
  (`e361c921`).

**The cadence-dependent ratio bug (worth its own story):**

Before the cutover, the SQLite-from-Pi aggregator computed
`downtime_pct = ride_down_polls / ride_active_polls`. This had
been silently wrong for the whole life of the metric — but
approximately right by accident.

Pi `wait_history` turned out to have multiple concurrent poller
processes writing to it (two distinct 2-min cadences offset by
~21 seconds, plus irregular extras — most likely a leftover from
earlier iteration where a systemd service AND a cron job both
ended up running on the Pi). Pi data was 2-4× denser than the
single-stream cadence everyone assumed.

In SQLite mode, both numerator and denominator carried this
inflation symmetrically, so the ratio came out approximately
right. In DDB mode during cutover (WAIT# inheriting Pi-multi-
stream inflation but synthesized DOWN polls at single-stream
2-min cadence from HIST#), the asymmetry produced ~50% of truth
for most rides and ~14× truth for walkthrough-adjacent rides
where the denominator collapsed (Journey of Water reported
25.4% downtime against a true 1.8%).

Fix: switched accumulators from poll counts to wall-clock
minutes. SQLite path uses gap-to-next-poll (which auto-corrects
for cadence variance); DDB path uses HIST# transition pairs
(which encode duration directly). Both produce cadence-
independent totals. Apples-to-apples diff on the same data
window: avg_wait matches exactly (0.00 mean delta), max_wait
matches exactly, downtime_pct within 0.5pp mean delta.

**Design rationale:** Saw the metric was wrong before the
cutover went live. Two paths: ship the cutover with a known-
inaccurate metric and fix in follow-up, or pause and redesign
the metric first. Chose to redesign because (a) the wrong
numbers would have been visible on production analytics page
immediately, (b) the fix was contained to one file, and (c)
the test artifact (diff against Pi-derived ground truth) only
exists during the dual-source window — once Pi data is gone,
re-verifying the metric is much harder.

**What this enables:** Pi can run in parallel as a backup data
source (zero ongoing cost, free belt-and-suspenders) or be
retired at user discretion. Analytics page is now sourced from
the live DDB table. Manual regen is still required per cycle;
nightly automation queued (M6-B Phase 3).

**Follow-up queued:** flip aggregator default `sqlite` → `ddb`
in a follow-up commit; consolidate the Pi's parallel poller
processes (fix the multi-stream root cause); build the nightly
regen automation (M6-B Phase 3).

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

1. **Consolidate Pi poller processes** (~15-30 min, SSH-only).
   Discovered 2026-05-25: the Pi has multiple concurrent processes
   writing to `wait_history` (two distinct cadences offset by ~21s
   plus irregular extras). Likely a systemd service + cron + maybe
   `@reboot` all running simultaneously. Cleanup is low-impact
   (data is no longer authoritative — DDB is) but the multi-stream
   waste burns Pi cycles + SD card writes. Find via `systemctl`
   and `crontab -l` on the Pi; kill the duplicates.
2. **Bump `aws-actions/configure-aws-credentials` to `@v5`** when
   it ships — `@v4` runs on Node.js 20, which GitHub force-
   deprecates June 2026. One-line workflow change. Non-blocking
   until then.
3. **Capture Claude Desktop screenshots** — `docs/screenshot-brief.md`
   has the three target queries. Manual work at a bigger monitor
   when convenient. No session commitment needed.
4. **Tune LOW_VS_FORECAST thresholds** after ~30 days of accumulated
   FORECAST# data (target: late June 2026). Defaults are first-pass
   guesses. Env vars in `forecast_signal.py` let tuning be a
   config change, not a redeploy.
5. **Update MVMCP + Jollywood dates** when Disney publishes them
   (~10 min, manual). Gated on Disney announcing.
6. **Blog at megillini.dev** — first post showcases Magic Monitor.
   Separate project queued at `.planning/blog/`. Not blocking.
7. **M9 Phases 2-6 (custom web chat UI)** — deferred.
8. **M5 (trip planning)** — personal-use polish, can slip.

**Sequencing rationale for what's not shipped:** M6-B is closed
(Phase 3 shipped 2026-05-25 — nightly automation in place) and
M9 Phase 1 closed 2026-05-31 (session 2B shipped Cognito OAuth +
mobile verified end-to-end). Analytics is AWS-native nightly;
mobile MCP works from anywhere. The Pi runs in parallel as a free
backup data source until the user decides to retire it.
