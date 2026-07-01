# Data-growth model — `DisneyData`

The single source of truth for **what grows in the table and why**, so the
silent-data-growth failure class (see `CLAUDE.md`) is something the system
*prevents*, not something we periodically hunt for.

The rule this encodes: **you don't have to keep re-discovering what grows —
it's a small, known set.** Two invariants fall out of it (enforced in CI,
see "Invariants" below).

## Row types by growth

Every row's SK prefix places it in one of two buckets.

### Unbounded — grow with time. THIS is the entire growth story.

| SK type | PK | Written by | Rate | Prod TTL | Read by |
|---|---|---|---|---|---|
| `WAIT#<iso>` | `RIDE#<id>` | poller, per operating ride per poll | ~150 rides × ~30/hr × ~13 park-hrs/day | **180d** (`WAIT_OBSERVATION_RETENTION_DAYS`, set in `disney-stack.ts`) | **only the nightly aggregator** (`tools/aggregate-analytics.py`, full sweep — no date bound) |
| `HIST#<iso>` | `RIDE#<id>` | poller, per status transition | ~a few / ride / day | **1825d / 5yr** (`HISTORY_RETENTION_DAYS`, set in `disney-stack.ts`) | aggregator (full sweep) + `get_ride_downtime_today` (recent park-days only) |
| `FORECAST#<iso>` | `RIDE#<id>` | poller, per poll (when a forecast is present) | ~150 × ~30/hr | **7d** (`FORECAST_RETENTION_DAYS`) | aggregator |

Approximate steady-state ceilings (150 rides, ~30 obs/hr, ~13 park-hrs/day):
- `WAIT#` @ 180d → **~10M rows** (still the dominant component; was ~20M at
  365d before the 2026-07-01 cut).
- `HIST#` @ 1825d → ~1M rows.
- `FORECAST#` @ 7d → ~0.4M rows (self-limiting).

### Bounded — one per entity, or TTL'd short. Do NOT grow with time.

`STATE` (~150, one per ride) · `DOWN_SINCE` (≤150) · `COOLDOWN#*`
(TTL'd minutes–hours) · `WEATHER` (1, TTL 2d) · `PROFILE` (1/user) ·
`PARK#<key>/USER#<sub>` (N users × 4 parks) · `FAV_RIDE#<id>` (per user
favorite) · `PLAN#<iso>` / `TRIP#<id>` (per plan/trip, TTL'd 24h pending /
365d recorded) · `MCPCLIENT#<id>` (per DCR client).

## Invariants (enforced in CI)

Because the unbounded set is *known*, two rules make the failure class
un-introducible rather than something to inspect for:

1. **Every write to an unbounded SK type (`WAIT#` / `HIST#` / `FORECAST#`)
   MUST set a `ttl`.** No TTL = infinite growth. A new unbounded type must
   be added to this doc + given a TTL.
2. **No `table.scan()` on a live/interactive read path.** A Scan is
   O(table size); the unbounded types guarantee that grows without bound.
   Use a keyed `Query` / GSI. The only sanctioned full-table scan is the
   **offline** nightly aggregator (`tools/aggregate-analytics.py`), which
   must read the whole table by design, is paginated, and has no request
   timeout. Any other `table.scan(` must carry an explicit
   `# bounded-scan: <reason>` justification comment.

The CI check (`tools/check_growth_invariants.py`) greps for new
`table.scan(` without that justification and for unbounded-SK writes
missing a `ttl`.

## Retention rationale + decisions

- **`WAIT#`: 180d (decided 2026-07-01, was 365d).** It's the dominant growth
  component and the *only* reader is the nightly aggregator (the MCP
  forecast/baseline tools read the aggregated **snapshot**, not raw `WAIT#`).
  180d keeps two seasons of raw for the aggregator's baselines while ~halving
  the component. TTL is stamped at write time, so the table converges to the
  new ceiling over ~180d as existing 365d rows age out (no re-stamp done).
- **`HIST#`: kept at 1825d (5yr); MCP reconciled up to match.** The poller's
  5-year retention is intentional for the aggregator's downtime
  reconstruction. `mcp/_tool_impls.py` (`_HIST_RETENTION_DAYS`) was stalely
  90d, needlessly capping `get_ride_downtime_today`'s `days_back` — fixed to
  1825 (2026-07-01).
- **Trends vs. raw:** raw `WAIT#` is short-lived by design. Long-term
  wait-time *trends* (multi-year seasonality) are NOT retained today — the
  snapshot is recomputed nightly and overwritten. If long-term trends become
  desired, add a compact **rollup** layer (e.g., per-ride monthly wait
  profile by hour × day-of-week) kept indefinitely — ~1000× smaller than the
  raw it derives from. That is a deliberate *feature*, decoupled from this
  growth hygiene — design captured in `WAIT-TRENDS-ROLLUP-DESIGN.md` (parked).
  `HIST#`'s 5-year retention already provides long-term *downtime* history.

_Last reviewed: 2026-07-01._
