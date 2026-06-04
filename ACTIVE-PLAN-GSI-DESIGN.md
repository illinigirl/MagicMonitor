# Design: sparse GSI for active-plan lookup (poller)

**Status:** proposed, awaiting Megan's review. Local working doc — NOT
committed (same convention as `MULTIDAY-TRIP-PLANNER-DESIGN.md`,
`TIER-2-PLAN.md`).

**Date:** 2026-06-03. Author: Claude (design), Megan (review/architecture).

---

## Problem (found during M5 Phase 5 deploy-verify, 2026-06-03)

The poller's `build_active_plan_ride_index` (`infra/lambda/poller/db.py`)
finds today's active plans by **scanning the entire `DisneyData` table**
with a `FilterExpression`, paginated with a 50-page safety cap.

`DisneyData` is now **632 MB / ~3 M items** (dominated by `WAIT#` rows,
365-day retention). That's **~632 scan pages**; the 50-page cap covers
only **~8%** of the table. The live poller is hitting the cap:

```
[poller] build_active_plan_ride_index hit page cap (50), stopping early
```

A user's `PLAN#` rows all live in one partition (`USER#<id>`), so they're
either reached within the first 50 pages or not at all. With ~8% coverage
they're **most likely beyond the cap → activated plans get no disruption
alerts.** The headline M5 feature (dormant → activate → alerts) is
unreliable in production right now. It's also a cost issue: the poller
already reads ~50 MB every 2 min (the code's "~125 RCU / $0.025-day"
comment is long stale).

Not introduced by Phase 3 — the full-Scan + cap predates it (original
plan-aware alert path, ~2026-05-11). Phase 3 only added the `active` /
window clauses. M5 is what makes it load-bearing.

This is the **exact category** the project watches for (CLAUDE.md "silent
regression from data growth") and the cure is the one the code's own
comment already names (db.py:360-366): **Scan → Query via a GSI.** It's
also the same fix shipped for the 2026-05-24 `getParkRides` regression
(the `park_key-SK-index` GSI).

---

## Chosen approach: sparse GSI on `planned_for_date`

**Key insight:** `planned_for_date` already exists on **every** `PLAN#`
row and on **no other row type** (no `WAIT#`/`STATE`/`TRIP#`/cooldown row
has it). Every plan row ever written carries it (pre-M5 `record_plan`
hardcoded it to today; M5 made it future-capable). So a GSI partition-keyed
on `planned_for_date` is **automatically sparse** — it indexes only plan
rows.

Consequences that make this minimal:
- **No new attribute to stamp** on writes — `planned_for_date` is already
  there. `record_plan` / `create_trip` / `activate_plan` need **zero
  changes**.
- **No manual backfill** — DynamoDB auto-backfills a new GSI from existing
  base rows that have the key attributes. Every existing `PLAN#` row gets
  indexed automatically (they all have `planned_for_date`). Same as the
  `park_key-SK-index` precedent: "AWS backfills existing rows automatically
  … no schema migration code needed" (disney-stack.ts:225-226).
- **Index stays tiny** — only plan rows (dozens to low-hundreds, TTL-bounded
  to ≤365 days), vs the 632 MB base table.

### GSI spec (CDK — `infra/lib/disney-stack.ts`)

```ts
dataTable.addGlobalSecondaryIndex({
  indexName: "planned_for_date-index",
  partitionKey: { name: "planned_for_date", type: dynamodb.AttributeType.STRING },
  sortKey:      { name: "SK", type: dynamodb.AttributeType.STRING },
  projectionType: dynamodb.ProjectionType.ALL,
});
```

- **PK = `planned_for_date`** (e.g. `"2026-06-23"`). The poller queries
  exactly one partition: today.
- **SK = `SK`** (`PLAN#<iso_ts>`) — gives uniqueness; lets a future caller
  do `begins_with(SK,"PLAN#")` if ever needed (not required — the index is
  already plan-only).
- **Projection ALL** — the poller reads `ride_sequence`, `park_key`,
  `plan_window`, `active`, `outcome_recorded`, `planned_for_date`,
  `trip_id`. ALL avoids a base-table re-fetch and is cheap here because the
  index is sparse (few rows). (Contrast the existing `park_key-SK-index`,
  which is projection-ALL but NOT sparse — it indexes all `WAIT#`/`HIST#`
  rows, costing ~$1.25/mo. This one indexes only plan rows → negligible.)

Note: CloudFormation allows adding only **one** GSI per stack update — this
is the only GSI change in the deploy.

### Poller rewrite (`build_active_plan_ride_index`, db.py)

Replace the paginated `scan(FilterExpression=…)` with a `query` on the GSI:

```python
resp = _table.query(
    IndexName="planned_for_date-index",
    KeyConditionExpression="planned_for_date = :d",
    FilterExpression="outcome_recorded = :false "
                     "AND (attribute_not_exists(#active) OR #active = :true)",
    ExpressionAttributeNames={"#active": "active"},
    ExpressionAttributeValues={":d": today_date_iso, ":false": False, ":true": True},
)
```

- The date filter moves from a scanned `FilterExpression` to the **key
  condition** (server-side, indexed) — this is the whole win.
- Keep the existing **Python-side guards** for `active` and the plan-window
  check, AND add Python guards for `outcome_recorded` and a defensive
  `planned_for_date == today` / `SK startswith PLAN#`. This mirrors the
  established convention (db.py comment: "enforced in Python too so the
  stub-table tests, which don't parse FilterExpression, cover them").
- Keep pagination (`LastEvaluatedKey`). The 50-page cap becomes
  unreachable (a date partition holds a handful of plans); drop the cap or
  lower it to a sane value with an updated comment. The fail-soft
  `return index, active_plans` on exception stays — important during the
  GSI backfill window (see below).

### Write side

**No change.** `_build_plan_item` already writes `planned_for_date` on
every plan row (server.py + server_http.py). Nothing to stamp, nothing to
keep in sync.

---

## Tests (`infra/lambda/poller/tests/test_db.py`)

- Add a `query()` method to `_StubTable`. Minimal: return all items (like
  the existing `scan()` does) and let the Python guards in
  `build_active_plan_ride_index` do the filtering — same approach the
  current active/window tests already rely on.
- Existing `TestActivePlanGating` (7 cases) should pass unchanged once the
  Python guards cover `outcome_recorded` + `planned_for_date`.
- Add cases: (a) a plan with a **different `planned_for_date`** is excluded;
  (b) `outcome_recorded=true` is excluded; (c) a larger fixture
  (many plan rows) to exercise the query/pagination path — the
  "larger-fixture" test CLAUDE.md asks for in this category.

---

## Deploy + verify

1. `cd infra && npx cdk diff` — expect: new GSI `planned_for_date-index`
   + the `PollerFunction` code change. (Adding a GSI on a 632 MB table
   triggers an **online backfill**; no downtime. CloudFormation waits for
   the GSI to reach ACTIVE before the stack update completes.)
2. `npx cdk deploy DisneyStack --require-approval never`.
3. During backfill (minutes), the poller's GSI query may transiently fail
   → fail-soft returns `({},[])` (no plan alerts for a few min). Acceptable;
   no crash.
4. Manual poll (`aws lambda invoke …`), confirm: **no "hit page cap" log**,
   `status: ok`, elapsed drops (no more 50 MB scan).
5. **Functional check:** record a test plan for *today* via the MCP
   `record_plan` (auto-active) or write a fixture row, run a poll, confirm
   it appears in `active_plans` / the ride index. Then clean up the test
   plan.

## Rollback

GSIs are additive — if the poller query misbehaves, redeploy the prior
poller code (the GSI can stay; it's harmless and cheap). Or
`removeGlobalSecondaryIndex` in a later deploy. PITR is on for the table.

## Cost

- **Before:** ~50 MB scanned every 2 min (capped), growing with the table.
- **After:** one small Query of a sparse index every 2 min (a few rows).
  GSI storage ≈ size of all plan rows (tiny). Write amplification only on
  plan writes (rare). Net: large reduction, and it stops growing with
  `WAIT#` volume.

## Out of scope (future)

- Same idea could back `get_plan_for_day` / `get_upcoming_trip` on the MCP
  side (they currently `query` the `USER#` partition with `begins_with`,
  which is already efficient — one partition, not a table scan — so no
  urgency).
