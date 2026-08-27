# MagicMonitor code review — 2026-06-11

Multi-agent review of `origin/main` @ `aeadabb` (github.com/illinigirl/MagicMonitor).
8 scoped finder agents (MCP tools, MCP auth, web, infra, poller, security sweep,
data-growth hunter, tests/CI) + adversarial verification of every medium+ finding
against the actual code. **31 findings confirmed, 0 refuted, 23 low-severity notes.**
Several findings were independently discovered by 2–3 agents — those are merged below.

---

## What's healthy (verified, not assumed)

- **Web auth**: every per-user page and server action checks `auth()` and derives the
  DDB partition from the session sub; `/trips` has a correct family allowlist gate; no
  debug routes exist in this checkout.
- **MCP auth core**: RS256 pinned, exp/iss/sub required, `token_use=access` enforced,
  sub allowlist defaults to deny-all, middleware is default-deny.
- **Post-incident pagination**: `getParkRides` (web), poller plan index, MCP trip
  queries, and the analytics aggregator are properly paginated with documented bounds.
- **alert_routing**: the priority-resolver pattern holds; no caller has reintroduced
  cross-source membership-check coordination.
- **CDK stateful safety**: RETAIN + PITR on DisneyData, scoped LeadingKeys write
  conditions on both the SSR compute role and the MCP Lambda, SSM-name-only env vars
  for the poller.
- **Test quality where tests exist**: the web vitest suite asserts real command inputs
  and walks pagination; the poller stub table drives the ExclusiveStartKey loop;
  JWT tests do real RSA verification; CI wiring (installs, working dirs, lockfile)
  is correct.

---

## Tier 1 — fix first

### 1. `getUserParkSubscriptions` is a verbatim repeat of the 2026-05-24 incident ⚠️
**`web/src/lib/dynamodb-writes.ts:173` — HIGH** *(found independently by 3 agents)*

One `ScanCommand` with `FilterExpression: "SK = :sk AND begins_with(PK, :pk)"`, reads
only `resp.Items`, no `LastEvaluatedKey` loop. A Scan page caps *scanned* data at 1MB;
the repo's own comments document the table at ~5GB (~632MB / ~3M WAIT# rows as of
2026-06-03 per `poller/db.py:363`), so the first page is ~0.16% of the table and the
user's `PARK#<key>/USER#<sub>` rows are almost certainly not in it **today, in
production**. The function's comment reasons about matched-item count ("4 parks × N
users is small enough") — exactly the reasoning error TESTING.md documents as the #1
watched failure class.

Consequences: (a) `/me` renders park-alert toggles unchecked for parks the user IS
subscribed to; (b) worse, `saveSettings` (`me/actions.ts:85-93`) diffs against this
broken read, so `toRemove` stays empty and **unsubscribing from a park silently
doesn't work** — stale `PARK#/USER#` rows persist and alerts keep firing.

**Fix:** the key is fully known — replace the Scan with 4 parallel `GetItem` calls
(`PK: PARK#<key>, SK: USER#<sub>`). O(4) reads, structurally immune to table growth,
cheaper than any Scan. Add a `dynamodb-writes.test.ts` mirroring the existing
pagination-mock pattern. (`dynamodb-writes.ts` currently has **zero** test coverage,
and canary.yml's path filter doesn't include it.)

### 2. Canary grep can never match — the stop-loss for the flagship failure mode is permanently green
**`.github/workflows/canary.yml:55` — MEDIUM (verified against the live site)**

The canary greps raw HTML for `"0 attractions · 0 open ·.*0 down · 0 closed"`. React 19
SSR inserts `<!-- -->` separators between text nodes, so the live meta line renders as
`35<!-- --> attractions · <!-- -->29<!-- --> open ·…`. The verifier curled
magicmonitor.megillini.dev and simulated the empty-data case: the grep **cannot match
even when the regression occurs**. The runtime layer of the three-layer defense is
dead code.

**Fix:** strip separators (`sed 's/<!-- -->//g'`) AND invert to positive assertion
(extract the attractions count, assert > 0). Add a self-test against a captured
known-bad HTML fixture so the canary's own grep can't rot silently again.

### 3. `record_plan` same-day upsert silently destroys `completed_rides` / `dropped_rides` / `plan_window`
**`mcp/server.py:1546` — HIGH**

The upsert path reuses only the existing row's SK, then rebuilds the item via
`_build_plan_item` — which hardcodes `completed_rides: []` and `dropped_rides: []`
(`_tool_impls.py:2208-2209`) — and `put_item` replaces the whole row. If the user has
been marking rides complete mid-day (the "strongest calibration signal") and Claude
re-records the plan (e.g. a revised afternoon plan), every `actual_wait_min`,
drop reason, and the resolved `plan_window` is wiped silently.

**Fix:** merge instead of replace — carry forward `completed_rides`, `dropped_rides`,
`activated_at`, `plan_window` from the existing row, or switch to `update_item` on
only the fields `record_plan` owns. Test: `record_plan → mark_ride_complete →
record_plan → assert completed_rides survives`.

### 4. Unvalidated `context.planned_at` becomes the PLAN# sort key
**`mcp/server.py:1501` (identical in `server_http.py:1526`) — HIGH**

`plan_ts = (context or {}).get("planned_at") or now_utc.isoformat()` — no validation
(park and `planned_for_date` ARE validated in the same function). Three failures:
(1) a **naive** ISO string ('2026-06-09T18:00' — very plausible LLM output) parses fine
in `get_user_plan_history`, but aware-minus-naive subtraction raises `TypeError`, caught
only as `ValueError` → **the session-start entry-point tool errors on every call** until
that row TTLs or is deleted; (2) reusing one context snapshot across two `record_plan`
calls for different dates produces identical PK+SK — the second silently overwrites the
first; (3) arbitrary strings corrupt SK sort order.

**Fix:** parse-and-normalize `planned_at` (require aware, or coerce naive to UTC,
re-serialize); independently widen `server.py:2921` to `except (ValueError, TypeError)`.

### 5. Park-day window wrong between midnight and 4am ET
**`mcp/_tool_impls.py:250` — HIGH**

`_park_day_window_utc` never shifts the anchor date when `now` is before the 4am
boundary, despite both its own docstring and the tool docstring promising the
analytics convention (12–3am attributes to the previous park-day — which
`tools/aggregate-analytics.py:813` implements correctly). At 2am ET, "today"'s window
is entirely in the future → `get_ride_downtime_today` reports `down_count=0` after an
evening full of breakdowns. Late-night park hours are exactly when this gets asked.

**Fix:** `if now_et.hour < _PARK_DAY_BOUNDARY_HOUR: now_et -= timedelta(days=1)` before
subtracting `days_back`. Pure function — pin with a unit test for the 0–4am case.

### 6. MCP HTTPS server: five full-table Scans growing toward the 30s API Gateway hard cap
**`mcp/server_http.py:471, 566, 271; mcp/_tool_impls.py:281; mcp/server.py:805` — HIGH**

Correctly paginated (no silent-empty bug), but every page is a sequential ~1MB
round-trip over a multi-GB table growing every 2 minutes for a year. Sites:
`get_live_ride_status`, `get_park_live_status`, `_resolve_ride_via_ddb` (called by
`get_ride_forecast` + `get_ride_downtime_today`), `_fetch_park_currently_down` (inside
every `get_planning_context`), and the stdio variant. Latency grows linearly with WAIT#
accumulation; nothing watches it; the failure at the cap is a 504.

**Fix:** the structural fix already exists — the `park_key-SK-index` GSI (the same
index `getParkRides` switched to post-incident). Replace each Scan with a per-park GSI
Query (`park_key = :p AND SK = "STATE"`). Add a CloudWatch p95-duration alarm on
McpHttpFunction as the runtime stop-loss.

### 7. No CloudWatch alarm or DLQ anywhere — a dead poller is silent
**`infra/lib/disney-stack.ts:334` — MEDIUM**

Zero alarms, no SNS, no DLQ, no onFailure destination across all of infra. A dead or
persistently erroring poller leaves stale-but-present STATE rows, so the page stays
non-empty and the canary (even once fixed per #2) never trips — the 2026-05-24 shape,
on the write side.

**Fix:** ~20 lines of CDK: alarm on poller `Errors` (≥1 for 2 periods) + a freshness
alarm (`Invocations < 25/hr` or a metric filter on a per-poll success line), routed to
SNS → email/Pushover. onFailure SQS destination for inspectability.

---

## Tier 2 — security (MCP remote surface)

### 8. No `client_id` check — any app client on the shared Cognito pool mints valid MCP tokens
**`mcp/jwt_verifier.py:127` — MEDIUM**

`aud` verification is correctly skipped for Cognito access tokens, but the `client_id`
claim is never checked. An access token minted for an allowlisted user by ANY app
client on the shared pool (Watchtower, the MM web dashboard's NextAuth client) passes
verification and grants full MCP read/write. A token compromise anywhere else on the
pool becomes MCP write access.

**Fix:** track DCR-issued client_ids (or env-configured allowlist) and reject tokens
whose `client_id` isn't in it; or define a resource-server scope (e.g. `mcp/invoke`)
granted only to DCR-created clients and require it.

### 9. Unauthenticated, unthrottled `/register` — quota exhaustion + OAuth phishing vector
**`mcp/dcr_proxy.py:100` / `infra/lib/disney-mcp-stack.ts:339` — MEDIUM** *(found by 3 agents)*

`/register` is public by spec, advertised in the discovery metadata, has no APIGW
throttle (the "auth gate is the rate-limit story" comment doesn't cover pre-auth
routes), creates real Cognito app clients that are never deleted, on a pool **shared
with Watchtower** — so a scanner flood exhausts the pool's app-client quota for both
projects. Additionally `_is_acceptable_redirect_uri` accepts any `https://*`
(plus `startswith("http://localhost")` matching `http://localhost.evil.com`), enabling
a phishing flow: attacker registers a client with `redirect_uri=https://evil.example`,
lures an allowlisted family member through the hosted UI.

**Fix:** stage-level APIGW throttle (one line of CDK) + a client-count ceiling check in
the DCR proxy before create + tighten redirect URIs to known Claude callback patterns
(exact-match localhost host, allowlist of hosted callbacks).

### 10. `NEXTAUTH_SECRET` + `COGNITO_CLIENT_SECRET` land as plaintext Amplify env vars
**`infra/lib/disney-stack.ts:481` — MEDIUM**

`unsafeUnwrap()` keeps source/cdk.out/template clean (as the comment says), but
CloudFormation resolves the dynamic reference at deploy and stores literal values on
the Amplify App — readable in the console, via `aws amplify get-app`, and re-echoed
into `.env.production` every build. NEXTAUTH_SECRET is the session-signing key.

**Fix:** put only the secret *names* in env vars and fetch values at server boot
(computeRole already has scoped ssm:GetParameter; add scoped
secretsmanager:GetSecretValue). At minimum fix the comment that denies this exposure.

### 11. Unauthenticated MCP endpoint shares the 10-slot account concurrency with the poller
**`infra/lib/disney-mcp-stack.ts:336` — MEDIUM**

JWT verification happens *inside* the Lambda, so anonymous requests burn concurrency
before the 401. With ~3-4 slots used by another project, ~6 concurrent anonymous
requests to the public URL throttle the every-2-min poller invoke → silently stale
data (and there's no freshness alarm, see #7).

**Fix:** HTTP API stage throttle (`throttlingRateLimit: 5, burst: 10`); longer-term,
request a concurrency quota increase.

### 12. Zero logging on the entire auth path; comments claim it exists
**`mcp/server_http.py:2659` — MEDIUM**

"verifier logs the detail server-side" — it doesn't; no logging statement exists in
any of the four auth files. The catch-all `except Exception → 503` also silently
absorbs JWKS network failures and misconfig. Directly conflicts with the project's
"add diagnostic logging at each boundary" doctrine.

**Fix:** module logger; `VerifyError` at WARNING, unexpected exceptions at ERROR.

### 13. OIDC `pull_request` trust claim on an AdministratorAccess role (public repo)
**`infra/lib/disney-stack.ts:732` — LOW (downgraded from medium: not currently exploitable)**

The claim is dead surface — no PR workflow uses AWS. Not exploitable today (GitHub
denies id-token to fork PR runs), but one future `pull_request_target` +
`id-token: write` mistake hands fork code an admin role. **Fix:** delete the
`:pull_request` line from the StringLike condition.

---

## Tier 3 — poller correctness (all MEDIUM)

14. **Cooldown gates trust the DDB TTL reaper** (`db.py:243, 270, 295, 598; index.py:645`).
    Existence-only checks; expired-but-undeleted items (AWS: "typically within a few
    days") silently stretch a 15-min cooldown, suppressing alerts for a second distinct
    outage. Fix: one shared `_cooldown_active()` helper comparing `ttl` to `now`.
15. **Stale weather snapshot suppresses day-2 storm alerts** (`index.py:210`,
    `weather.py:159`). Snapshot freezes overnight when no plan is active; next day a
    brand-new storm is classified "already known" against a 12h-old prior. Fix: treat
    a prior older than ~2 polls as None.
16. **After-midnight park hours dropped** (`wait_times.py:212`). Schedule entries are
    keyed to the operating date, so at 00:30 during a 1am close the filter selects the
    wrong day and all alerts are suppressed while the park is open. Also conflates
    "closed today" with "fetch failed". Fix: include yesterday's entry when its close
    extends past midnight; distinct return values for closed vs failed.
17. **SSM token fetch escapes the notifier's try block** (`notifier.py:54`). A
    cold-start SSM failure aborts the whole poll AND the cooldown was already marked,
    so the retry skips the alert — lost for the full window. Fix: move
    `_get_app_token()` inside the try / return False on failure.
18. **Zero test coverage of `index.py`'s status-transition state machine** — where both
    previously-shipped bugs lived. `upsert_ride`, `record_status_change`,
    `record_forecast`, `set/get/clear_down_since` also untested. Fix: one handler-level
    test with the existing stub table + monkeypatched fetch/notifier.

---

## Tier 4 — MCP tool contract (all MEDIUM)

19. **Date params accept datetime-shaped ISO strings** (`server.py:1507, 1658`).
    `datetime.fromisoformat` accepts `'2026-06-23T09:00:00'` and `'20260623'`; stored
    verbatim, then every downstream check is string equality against `YYYY-MM-DD` — a
    same-day plan passed as a timestamp **silently stays DORMANT** (no alerts).
    Fix: normalize via `.date().isoformat()` at every write-path entry.
20. **`create_trip` docstring promises "no partial trip is left" — false on write
    failure** (`server.py:1639`). Non-transactional batch_writer; a mid-flush failure
    leaves header + partial day rows, and a docstring-trusting retry mints a second
    trip_id. Fix: TransactWriteItems, or soften the contract + best-effort cascade
    delete on failure.
21. **`get_user_plan_history` applies `include_unrecorded_only` AFTER `Limit`** (both
    transports). A multi-day trip's dormant future rows push the 1-14-day-old
    unrecorded plan past limit=10 — the "anything to ask about?" prompt silently never
    fires and the calibration row TTLs away. Fix: paginate when the flag is set, stop
    once rows predate the recall window; or return `truncated: true`.
22. **`record_plan_outcome` accepts any rating string** (`server.py:2334`). Near-miss
    enum values (classic LLM output) are stored, marked recorded (1yr TTL), then
    silently excluded from calibration — feedback permanently lost. Fix: validate
    against the enum at write, error payload listing valid values.
23. **`_compute_load_vs_forecast` drops 0-minute waits via falsy check**
    (`_tool_impls.py:678`). `if not actual` conflates 0 (real walk-on, strongest
    below-forecast sample) with None → `park_load_ratio` biased toward 1.0 on light
    days, and the planner scales cost-of-delay by it. Fix: `if actual is None`.
24. **`saveFavorites` wipes all of a user's favorites for a park if `getParkRides`
    returns `[]`** (`web/src/app/me/rides/[park]/actions.ts:47`) — the exact
    empty-read condition that held for 7 days in the 2026-05-24 incident becomes
    silent data loss + "Saved (−N)" success. Fix: bail with an error when the
    source-of-truth list is empty.
25. **The eval suite cannot detect server.py docstring regressions**
    (`mcp/evals/tool_schemas.py`). It tests a hand-copied parallel schema set (11 of
    28 tools, paraphrased descriptions) with no sync check — the documented defense
    ("run the eval suite after any docstring change") exercises the old copy and
    passes regardless. Fix: a cheap non-LLM CI test asserting name/param-set/
    description sync between server.py's registry and tool_schemas.py.

---

## Low-severity notes (23)

**MCP:** unguarded read-modify-write on shared plan rows (concurrent family edits lose
updates); `activate_plan` future-date guard is docstring-only; upsert lookup swallows
read failures → duplicate day rows; OPTIONS bypasses auth middleware; 401s missing
`WWW-Authenticate` (MCP auth spec / RFC 9728).

**Web:** `trips-access.ts` missing the `import "server-only"` guard its comment
promises; `/trips` allowlist trusts session email without requiring verified Google
identity; `todayDates()` DST fall-back edge; stale `?skip=1` comment; wrong
client-singleton comment.

**Infra:** GSI definition changes delete live indexes with no deploy-hazard guard;
documented rollback path collides on the retained fixed-name S3 bucket; production
Cognito client whitelists `http://localhost:3000` callbacks.

**Poller:** outcome_recorded Python re-guard diverges from the GSI FilterExpression on
missing attributes (stub can't detect); `db.py:339` unpaginated Query+Filter with no
documented expiry; second-alert sweep bypasses the db helper layer (`from db import
_table` + hand-rolled cooldown); no per-attraction error isolation on the DDB hot path.

**Tests/CI:** TESTING.md materially stale (denies web tests that exist, wrong eval
count, omits 6 of 8 MCP test modules); `pytest mcp` from root collects the paid eval
suite; canary's PR trigger curls *production*, not the PR's code; no canary for /trips
or /me; park canary blind while parks are closed.

---

## Suggested fix order

1. `getUserParkSubscriptions` → 4 GetItems + test (#1) — live wrong behavior today
2. Canary grep fix + positive assertion (#2) — restores the stop-loss for everything else
3. The two `record_plan` write-path holes (#3, #4) — calibration data loss + a tool-killing crash
4. Poller alarms + the `/register` throttle (#7, #9, #11) — one small CDK change covers all three
5. Park-day window + the poller timing trio (#5, #14–17)
6. Tool-contract batch (#19–25) — mostly small validation/normalization changes
7. `client_id` enforcement + auth logging (#8, #12)
