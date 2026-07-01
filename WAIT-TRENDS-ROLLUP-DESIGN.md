# Wait-trends rollup — design note (PARKED, not built)

Status: **idea captured 2026-07-01, not scheduled.** A deliberate *feature*,
decoupled from the data-growth hygiene work (raw `WAIT#` retention + the CI
invariant). Build only if long-term wait-time trends become something the
planner should use.

## Problem

Raw `WAIT#` observations are short-lived by design (target 180d retention —
see `DATA-GROWTH-MODEL.md`). But there is **no long-term wait-time trend
store today**: the analytics snapshot is recomputed from scratch every night
and overwritten, so "trends" only reach as far back as raw `WAIT#`. Shortening
raw retention (the growth fix) therefore also caps how far seasonality can be
observed — *unless* we keep a compact rollup.

The reframe: **raw ≠ trends.** A downsampled summary is ~1000× smaller than the
raw it derives from, so years of trend can be kept for almost nothing.

## Goal

Retain multi-year wait-time **seasonality** ("Space Mountain runs ~55 min at
2pm on summer Saturdays") independently of raw retention, cheaply, for better
planner predictions — without carrying ~20M raw rows.

## Design (tiered retention)

- **Raw `WAIT#`:** short TTL (180d). Feeds current baselines + recent
  granularity. Unchanged from the growth-hygiene work.
- **Rollup layer (new):** the nightly aggregator writes a compact per-ride
  wait profile, kept **indefinitely** (or multi-year TTL).

Candidate schema (DDB rows, or a history file appended to the S3 snapshot):

```
PK = RIDE#<id>
SK = WAITROLLUP#<period>          # e.g. monthly: WAITROLLUP#2026-07
attrs: per (hour_of_day × day_of_week) bucket → {p50, p90, mean, n}
```

~one row per ride per month → ~150 rides × 12 = ~1,800 rows/year. Trivial.

- **Accumulation:** each aggregator run upserts only the *current* period's
  rollup from recent raw `WAIT#` (not a full-history sweep). Past periods are
  immutable once closed — so old raw can TTL away without losing the trend.
- **Consumers:** `get_planning_context` / forecast tools could blend the
  long-term profile into predictions; a future `get_ride_wait_profile` tool
  could expose "typical wait by hour/day-of-week over the last N years."

## Open questions (decide at build time)

- Granularity: monthly vs. weekly buckets.
- Stats to keep: p50/p90/mean/n — enough for planning without storing
  distributions.
- Storage: DDB rows (queryable per ride) vs. extending the S3 snapshot with a
  `history` section (simpler, but not point-queryable).
- Aggregator: confirm it can compute the current period incrementally rather
  than re-sweeping all raw each night (also a nice cost win regardless).
- Backfill: whether to seed rollups from existing raw before it's TTL'd down
  to 180d (one-time, if we want history that predates the cutover).

## Why decoupled

The growth fix (raw retention + CI gate) stands alone and stops the problem.
This rollup adds *capability* (long-term trend) and should be scheduled on its
own merits, not as a dependency of the hygiene work. `HIST#`'s 5-year
retention already provides long-term *downtime* history separately.
