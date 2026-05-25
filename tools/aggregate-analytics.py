#!/usr/bin/env python3
"""
Aggregate Pi-snapshot SQLite into web/src/data/analytics-snapshot.ts.

One-shot generator for the M6 analytics page (Option C — frozen
JSON checked into the repo, no AWS data plane required). Reads
`.scratch/disney-pi-snapshot.db` (snapshot of the Pi's wait_history),
buckets ~8.8M poll rows by ride/hour/dow, writes a typed TypeScript
module the Next.js Server Components import directly.

Re-run by hand whenever you re-snapshot the Pi:
    python3 tools/aggregate-analytics.py

Emits:
    web/src/data/analytics-snapshot.ts  (~50 KB)

The snapshot DB itself is gitignored under .scratch/; only the
aggregated TS file is committed.

Implementation note: an earlier version did the hour/dow bucketing in
SQL via `strftime('%H', datetime(polled_at, '-4 hours'))`. That ran
the Python datetime parser inside SQLite for every one of 8.8M rows
in every grouping query, and didn't finish in 12 minutes. Streaming
the rows out and bucketing in Python with epoch arithmetic finishes
in ~60s.
"""

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Optional — only imported when --source ddb is used. Pi-snapshot
# runs don't need boto3 in PATH.
try:
    import boto3
    from boto3.dynamodb.conditions import Key
except ImportError:
    boto3 = None
    Key = None

# Bidirectional park-id ↔ park-key map. UUIDs match
# web/src/lib/schedule.ts and web/src/lib/showtimes-server.ts; the
# source of truth is themeparks.wiki.
PARK_IDS = {
    "75ea578a-adc8-4116-a54d-dccb60765ef9": "magic_kingdom",
    "47f90d2c-e191-4239-a466-5892ef59a88b": "epcot",
    "288747d1-8b4f-4a64-867e-ea7c9b27bad8": "hollywood_studios",
    "1c84a229-8862-4648-9c71-378ddd2c7693": "animal_kingdom",
}

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / ".scratch" / "disney-pi-snapshot.db"

# Two-file snapshot output:
#   .json — source of truth, machine-readable. Read by the MCP
#           server (Python) and any other non-TS tooling.
#   .ts   — thin typed re-export. Web app keeps importing
#           ANALYTICS_SNAPSHOT from this same path; only the
#           internals change. Next.js handles `import data from
#           "./*.json"` natively.
JSON_PATH = ROOT / "web" / "src" / "data" / "analytics-snapshot.json"
TS_PATH = ROOT / "web" / "src" / "data" / "analytics-snapshot.ts"

# Side-output for the poller Lambda — per (ride, hour-ET) wait
# thresholds used by the M7-promoted "short wait" alerts. Bundled
# into the Python Lambda asset by CDK.
BASELINES_PATH = ROOT / "infra" / "lambda" / "poller" / "baselines.json"

# Short-wait alert thresholds. We only emit a threshold for (ride, hour)
# combinations where the typical wait is "interesting" — alerting that
# Tom Sawyer Island has a 5-min wait at 9am is noise because it always
# does. The threshold itself is half the typical wait, capped so we
# never alert on a 60-min wait as "short."
MIN_INTERESTING_AVG_WAIT = 25  # baseline must be at least this many mins
SHORT_WAIT_THRESHOLD_CAP = 30  # never alert if current wait > this many mins

EASTERN = ZoneInfo("America/New_York")

# Per-ride data with fewer than this many active wall-clock minutes
# is dropped — rides that closed for refurb during the data window or
# were rare event-only entities. 200 min ≈ 3.3 hours of activity, low
# bar.
#
# Switched from poll-count to wall-clock-minute gating during the
# M6-B Phase 4 cutover: the Pi snapshot had concurrent multi-stream
# polling that inflated per-poll counts non-uniformly, and the live
# poller's WAIT# write rule (skip rows with wait_mins=NULL) means
# poll counts and operational time can diverge dramatically. Wall-
# clock minutes are the right denominator for "how active was this
# ride during this window."
MIN_RIDE_MINUTES = 200
# Heatmap cell needs at least this many active wall-clock minutes
# to be shown, else we treat it as "park closed at this hour" and
# leave the cell empty in the UI. 40 min ≈ 20 polls at 2-min
# cadence, matching the prior poll-count gate.
MIN_HEATMAP_CELL_MINUTES = 40
# Max gap to attribute to a single status when computing wall-clock
# minutes. Caps long inter-poll gaps (e.g., overnight when the API
# wasn't reachable, or a multi-day data outage) so they don't bias
# any single status's minute total. 5 min matches the existing
# polled-status semantics: a missed poll beyond 5 min counts as a
# data gap, not as continued time in the prior status.
MAX_INTERPOLL_GAP_MINUTES = 5
# Hours strictly before this (in ET) get attributed to the previous
# day's heatmap row. Disney parks regularly stay open past midnight
# for special events (Halloween parties, EEH for deluxe guests); a
# 1am Friday poll is really part of Thursday's park-day. 4am is the
# safe cutoff — no WDW park has ever operated past 3am.
PARK_DAY_BOUNDARY_HOUR = 4

# DOWN-cluster detection. A "cluster" is a contiguous run of DOWN
# polls (gaps up to GAP_TOLERANCE_MINUTES allowed — 2-min poll cadence
# means a single missed poll shouldn't break a cluster) lasting at
# least MIN_CLUSTER_MINUTES. Single 2-min DOWN flaps aren't clusters.
# Clusters lasting >= LONG_CLUSTER_MINUTES are "structural" — the
# kind of pattern the BTM Sunday-evening anomaly looks like — and
# get attributed to recurring_down_fraction in the heatmap cells.
MIN_CLUSTER_MINUTES = 30
LONG_CLUSTER_MINUTES = 120
GAP_TOLERANCE_MINUTES = 4

# DDB configuration. Only used when --source ddb.
DDB_TABLE_NAME = "DisneyData"
DDB_REGION = "us-east-2"
DDB_PROFILE = "watchtower"

# Live poller's 2-minute cadence. Used in DDB mode to synthesize
# per-poll DOWN counts from HIST# cluster durations. Keep in sync
# with the EventBridge schedule in infra/lib/disney-stack.ts.
POLL_INTERVAL_MINUTES = 2


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source", choices=("sqlite", "ddb"), default="ddb",
        help=(
            "Where to read poll history from. ddb (default) reads "
            "the live DynamoDB table — the authoritative source "
            "post-M6-B Phase 4. sqlite reads the Pi snapshot at "
            ".scratch/disney-pi-snapshot.db (kept for historical "
            "diffing while the Pi runs in parallel as a backup)."
        ),
    )
    parser.add_argument(
        "--end-date", type=str, default=None,
        help=(
            "Trim DDB data to timestamps with date ≤ this ISO "
            "date (YYYY-MM-DD). Used for apples-to-apples diff "
            "against a SQLite snapshot whose data window ends "
            "earlier than the live table. Only applies to "
            "--source ddb."
        ),
    )
    args = parser.parse_args()

    t0 = time.time()
    con: sqlite3.Connection | None = None
    table = None  # boto3 Table resource when source == 'ddb'

    if args.source == "sqlite":
        if not DB_PATH.exists():
            print(f"Snapshot not found at {DB_PATH}", file=sys.stderr)
            sys.exit(1)
        print(f"Reading {DB_PATH}")
        con = sqlite3.connect(str(DB_PATH))
        con.row_factory = sqlite3.Row

        # --- ride metadata ---
        rides_meta: dict = {}  # ride_id → {name, park_id}
        for row in con.execute("SELECT id, name, park_id FROM rides"):
            rides_meta[row["id"]] = {
                "name": row["name"], "park_id": row["park_id"]
            }
        print(f"  rides: {len(rides_meta)}")

        # --- date range (one cheap query) ---
        rr = con.execute(
            "SELECT MIN(polled_at) AS start, MAX(polled_at) AS end_, "
            "COUNT(*) AS n FROM wait_history"
        ).fetchone()
        range_info = {"start": rr["start"], "end_": rr["end_"], "n": rr["n"]}
    else:
        print(f"Reading DDB table {DDB_TABLE_NAME} ({DDB_REGION})")
        table = _ddb_table()
        rides_meta = _load_ride_meta_ddb(table)
        print(f"  rides: {len(rides_meta)}")
        # One-shot fetch of every WAIT# + HIST# row needed by the
        # four downstream passes. Trades ~1.4 GB of memory for a
        # ~10× wall-time improvement vs re-Querying per pass.
        wait_by_ride, hist_by_ride = _prefetch_ddb(table, rides_meta)
        # Optional date-window trim for apples-to-apples diff
        # against a SQLite snapshot. The filter is purely client-
        # side after the prefetch — easier than parameterizing the
        # DDB Query KeyConditionExpression and only used during
        # cutover verification, so the inefficiency doesn't matter.
        if args.end_date:
            cutoff = args.end_date
            before_w = sum(len(v) for v in wait_by_ride.values())
            before_h = sum(len(v) for v in hist_by_ride.values())
            wait_by_ride = {
                rid: [it for it in items if it["polled_at"][:10] <= cutoff]
                for rid, items in wait_by_ride.items()
            }
            hist_by_ride = {
                rid: [it for it in items if it["changed_at"][:10] <= cutoff]
                for rid, items in hist_by_ride.items()
            }
            after_w = sum(len(v) for v in wait_by_ride.values())
            after_h = sum(len(v) for v in hist_by_ride.values())
            print(
                f"  trimmed to --end-date {cutoff}: "
                f"WAIT# {before_w:,} → {after_w:,}, "
                f"HIST# {before_h:,} → {after_h:,}"
            )
        range_info = _get_date_range_from_prefetch(wait_by_ride)

    print(
        f"  span: {range_info['start'][:10]} → {range_info['end_'][:10]}  "
        f"({range_info['n']:,} rows)"
    )

    # --- pass 1: derive operating hours per (park, park-day) ---
    # The signal: earliest and latest polled_at where ANY ride at the
    # park was OPERATING or DOWN (which together mean "the API thinks
    # this attraction is currently in service or trying to be"). Polls
    # outside that window are park-closed and shouldn't contribute to
    # downtime % calculations — without this filter, late-evening and
    # early-morning DOWN polls during the closed window get counted
    # against the ride's reliability when they're actually just "park
    # not open." See README "Engineering judgment moments" for context.
    if args.source == "sqlite":
        park_hours = _derive_park_hours(con, rides_meta)
    else:
        park_hours = _derive_park_hours_ddb(wait_by_ride, rides_meta)
    print(f"  derived park hours for {len(park_hours)} (park, park-day) keys")

    # --- pass 1b: detect DOWN clusters per ride ---
    # Contiguous runs of DOWN polls per ride. Park-hours filter applied
    # — a DOWN run that spans from inside operating hours into a closed
    # window terminates at the boundary. The clusters list is exposed
    # via the MCP get_ride_down_clusters tool; the long-cluster poll
    # counts feed back into the per-(ride, dow, hour) cells as a
    # `recurring_down_fraction` so the heatmap can distinguish flap-
    # style breakdowns (low fraction) from structural patterns like
    # BTM's Sunday-evening recurrence (high fraction).
    if args.source == "sqlite":
        clusters_by_ride, long_cluster_polls = _detect_down_clusters(
            con, rides_meta, park_hours
        )
    else:
        clusters_by_ride, long_cluster_polls = _detect_down_clusters_ddb(
            hist_by_ride, rides_meta, park_hours
        )
    n_clusters = sum(len(v) for v in clusters_by_ride.values())
    n_long = sum(
        1 for cs in clusters_by_ride.values()
        for c in cs if c["duration_minutes"] >= LONG_CLUSTER_MINUTES
    )
    print(
        f"  detected {n_clusters:,} DOWN clusters "
        f"({n_long:,} long ≥{LONG_CLUSTER_MINUTES}m, the rest flap-style)"
    )

    # --- pass 1c: Lightning Lane drop patterns per ride ---
    # An LL "drop" is a same-day event where Disney moves a ride's
    # next-available return time earlier (cancellations / no-shows /
    # refreshes). These are the moments a guest can grab a better
    # slot through the app. The Pi captures every LL state change to
    # ll_history; here we aggregate into drop hours, dow patterns, and
    # typical shift size so the MCP planner can advise guests when to
    # check for swap opportunities.
    #
    # DDB mode: returns {}. LL replication to DDB is queued as a
    # post-cutover follow-up; in DDB mode every ride emits no
    # ll_drop_* fields in the output snapshot.
    if args.source == "sqlite":
        ll_drops_by_ride = _compute_ll_drop_analytics(con)
    else:
        ll_drops_by_ride = _compute_ll_drop_analytics_ddb(table)
    n_rides_with_drops = len(ll_drops_by_ride)
    n_drops_total = sum(d["ll_drops_total"] for d in ll_drops_by_ride.values())
    print(
        f"  LL drop analytics: {n_drops_total:,} drops across "
        f"{n_rides_with_drops} rides"
    )

    # --- pass 2: compute wall-clock-minute accumulators ---
    # Post-cutover: ride_active and ride_down are MINUTES, not poll
    # counts. The SQLite path uses inter-poll gaps to attribute time
    # to each status; the DDB path uses HIST# transition pairs. Both
    # produce cadence-independent totals — the Pi's multi-stream
    # polling no longer biases the result, and the DDB live poller's
    # "skip wait_mins=NULL" WAIT# write rule no longer skews the
    # denominator (because we measure from HIST# transitions, not
    # WAIT# row counts).
    #
    # See the M6-B Phase 4 cutover notes in PROJECT.md for why we
    # switched off poll-count accumulators.
    print("  streaming poll rows…")
    t_stream = time.time()
    if args.source == "sqlite":
        acc, n_seen, n_closed_filtered = _compute_minutes_sqlite(
            con, rides_meta, park_hours
        )
        print(
            f"  walked {n_seen:,} polls in {time.time() - t_stream:.1f}s"
        )
    else:
        acc, n_wait_seen = _compute_minutes_ddb(
            wait_by_ride, hist_by_ride, rides_meta, park_hours
        )
        n_seen = n_wait_seen
        n_closed_filtered = 0  # park-hours filter is applied per slice
        print(
            f"  walked {n_wait_seen:,} WAIT# + per-ride HIST# in "
            f"{time.time() - t_stream:.1f}s"
        )

    # --- shape outputs ---
    # Pull out accumulators by name for clarity. Each is keyed as
    # described in _new_accumulators(); the _min variants hold
    # wall-clock minutes.
    ride_active_min = acc["ride_active_min"]
    ride_down_min = acc["ride_down_min"]
    ride_max_wait = acc["ride_max_wait"]
    ride_wait_sum = acc["ride_wait_sum"]
    ride_wait_n = acc["ride_wait_n"]
    rh_wait_sum = acc["rh_wait_sum"]
    rh_wait_n = acc["rh_wait_n"]
    rh_active_min = acc["rh_active_min"]
    rh_down_min = acc["rh_down_min"]
    rdh_wait_sum = acc["rdh_wait_sum"]
    rdh_wait_n = acc["rdh_wait_n"]
    rdh_active_min = acc["rdh_active_min"]
    rdh_down_min = acc["rdh_down_min"]
    pdh_wait_sum = acc["pdh_wait_sum"]
    pdh_wait_n = acc["pdh_wait_n"]
    pdh_active_min = acc["pdh_active_min"]

    rides_list = []
    for ride_id, meta in rides_meta.items():
        active_min = ride_active_min[ride_id]
        if active_min < MIN_RIDE_MINUTES:
            continue
        park_key = PARK_IDS.get(meta["park_id"])
        if not park_key:
            continue
        avg_wait_count = ride_wait_n[ride_id]

        hourly_wait = []
        hourly_downtime = []
        for h in range(24):
            wn = rh_wait_n[(ride_id, h)]
            if wn > 0:
                hourly_wait.append(
                    {"hour": h, "wait": round(rh_wait_sum[(ride_id, h)] / wn)}
                )
            ha_min = rh_active_min[(ride_id, h)]
            if ha_min >= MIN_HEATMAP_CELL_MINUTES:
                hourly_downtime.append(
                    {"hour": h, "pct": round(100.0 * rh_down_min[(ride_id, h)] / ha_min, 1)}
                )

        # Per-(dow, hour) breakdown for this ride. Same
        # MIN_HEATMAP_CELL_MINUTES gate as the park-level heatmap
        # so cells with thin samples don't show up as confident
        # numbers.
        dow_hourly = []
        for dow in range(7):
            for h in range(24):
                key = (ride_id, dow, h)
                ha_min = rdh_active_min[key]
                if ha_min < MIN_HEATMAP_CELL_MINUTES:
                    continue
                wn = rdh_wait_n[key]
                down_min_cell = rdh_down_min[key]
                cell = {
                    "dow": dow,
                    "hour": h,
                    "downtime_pct": round(100.0 * down_min_cell / ha_min, 1),
                    # n_active is now in minutes, not polls; emit as
                    # integer minutes to keep the field compact.
                    "n_active": int(round(ha_min)),
                }
                # Only include `wait` when there's at least one
                # operating poll with a wait_mins value — wait isn't
                # meaningful when the ride was 100% DOWN at that
                # (dow, hour).
                if wn > 0:
                    cell["wait"] = round(rdh_wait_sum[key] / wn)
                # `recurring_down_fraction`: of the DOWN minutes in
                # this bucket, what fraction belonged to a long
                # (>=2h) cluster? 1.0 means all DOWN time was part
                # of sustained recurring-looking patterns; 0.0 means
                # all flap-style. Omit when no DOWN minutes.
                #
                # The numerator (long_cluster_polls) still holds
                # synthesized poll counts from _detect_down_clusters;
                # convert to minutes via × POLL_INTERVAL_MINUTES to
                # match the down_min_cell denominator.
                if down_min_cell > 0:
                    long_count = long_cluster_polls.get(key, 0)
                    long_min = long_count * POLL_INTERVAL_MINUTES
                    cell["recurring_down_fraction"] = round(
                        min(1.0, long_min / down_min_cell), 2
                    )
                dow_hourly.append(cell)

        ll_drops = ll_drops_by_ride.get(ride_id, {})
        rides_list.append(
            {
                "ride_id": ride_id,
                "ride_name": meta["name"],
                "park_key": park_key,
                # total_polls field now reports active wall-clock
                # minutes (renamed semantically but field name kept
                # for web-UI compatibility — the analytics page only
                # uses this as a rough activity indicator). Web UI
                # displays formatPollCount() over this value.
                "total_polls": int(round(active_min)),
                "downtime_pct": round(100.0 * ride_down_min[ride_id] / active_min, 1),
                "max_wait": ride_max_wait[ride_id],
                "avg_wait": round(ride_wait_sum[ride_id] / avg_wait_count)
                if avg_wait_count > 0
                else None,
                "hourly_wait": hourly_wait,
                "hourly_downtime": hourly_downtime,
                "dow_hourly": dow_hourly,
                "down_clusters": clusters_by_ride.get(ride_id, []),
                # LL drop analytics: present only for rides that had
                # any drops in the window. Rides without LL offerings
                # (or with too few drops to characterize) omit the
                # whole block rather than emitting zeros.
                "ll_drops_total": ll_drops.get("ll_drops_total"),
                "ll_drop_hours": ll_drops.get("ll_drop_hours"),
                "ll_drop_dow": ll_drops.get("ll_drop_dow"),
                "ll_typical_shift_mins": ll_drops.get("ll_typical_shift_mins"),
                "ll_active_days": ll_drops.get("ll_active_days"),
                "ll_drops_per_active_day": ll_drops.get(
                    "ll_drops_per_active_day"
                ),
            }
        )

    # Sort by downtime % desc — analytics page leads with most-down rides.
    rides_list.sort(key=lambda r: -r["downtime_pct"])

    heatmaps = {pk: [] for pk in PARK_IDS.values()}
    for (park_id, dow, hour), n_min in pdh_active_min.items():
        if n_min < MIN_HEATMAP_CELL_MINUTES:
            continue
        park_key = PARK_IDS.get(park_id)
        if not park_key:
            continue
        wn = pdh_wait_n[(park_id, dow, hour)]
        if wn == 0:
            continue
        heatmaps[park_key].append(
            {
                "hour": hour,
                "dow": dow,
                "wait": round(pdh_wait_sum[(park_id, dow, hour)] / wn),
                # n field is now active wall-clock minutes for this
                # (park, dow, hour) cell.
                "n": int(round(n_min)),
            }
        )
    for pk in heatmaps:
        heatmaps[pk].sort(key=lambda d: (d["dow"], d["hour"]))

    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "date_range": {"start": range_info["start"], "end": range_info["end_"]},
        "total_polls": range_info["n"],
        "polls_filtered_by_park_hours": n_closed_filtered,
        "rides": rides_list,
        "heatmaps": heatmaps,
    }

    JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))
    TS_PATH.write_text(
        f"""// AUTO-GENERATED by tools/aggregate-analytics.py — do not edit by hand.
// Re-export of analytics-snapshot.json with the typed shape from
// @/lib/analytics applied. The .json file is the source of truth
// (read by Python tooling, e.g. the MCP server). The web app
// keeps importing `ANALYTICS_SNAPSHOT` from this module; only the
// internals changed.
//
// Regenerate by re-snapshotting the Pi and running:
//   python3 tools/aggregate-analytics.py
//
// {len(rides_list)} rides × hourly + per-park hour×day heatmaps,
// aggregated from {snapshot['total_polls']:,} polls spanning
// {snapshot['date_range']['start'][:10]} → {snapshot['date_range']['end'][:10]}.

import type {{ AnalyticsSnapshot }} from "@/lib/analytics";
import data from "./analytics-snapshot.json";

export const ANALYTICS_SNAPSHOT = data as AnalyticsSnapshot;
"""
    )

    # Side-output: short-wait baselines for the poller Lambda.
    _write_short_wait_baselines(rides_list, snapshot["generated_at"])

    print(f"  total: {time.time() - t0:.1f}s")
    print(f"  wrote {JSON_PATH}  ({JSON_PATH.stat().st_size / 1024:.1f} KB)")
    print(f"  wrote {TS_PATH}    ({TS_PATH.stat().st_size} B)")
    print(f"    rides: {len(rides_list)}")
    print(f"    heatmap cells: {sum(len(v) for v in heatmaps.values())}")
    if con is not None:
        con.close()


def _compute_minutes_ddb(
    wait_by_ride: dict, hist_by_ride: dict, rides_meta: dict, park_hours: dict
) -> tuple[dict, int]:
    """DDB-backed wall-clock-minute accumulation.

    For each ride:
      - Walk HIST# transitions (already SK-sorted = time-sorted).
        Each consecutive pair (transition[N], transition[N+1])
        bounds a run of `transition[N].new_status`. Attribute the
        run's duration to that status via _bucket_minutes.
      - Walk WAIT# rows and accumulate into wait_sum/wait_n/
        max_wait via _accumulate_wait. Park-hours filter applied
        per row.

    Boundary handling: the FIRST observed transition for a ride
    leaves the prior-status run unbounded (we don't know when the
    ride entered new_status before this transition). Same for the
    LAST transition. Both partial runs are skipped. For a 76-day
    window, this drops ~minutes-to-hours per ride at each end,
    well under 1% of total active time per ride.

    Returns:
        (accumulators, n_wait_rows_seen)
    """
    fromiso = datetime.fromisoformat
    acc = _new_accumulators()
    n_wait_seen = 0

    for ride_id, meta in rides_meta.items():
        park_id = meta["park_id"]

        # --- HIST#-driven active/down minute accumulation ---
        # Transitions arrive SK-ascending (changed_at-ascending) from
        # the DDB Query. Each transition says "at changed_at, the
        # ride switched from old_status to new_status." The run of
        # new_status begins at this transition's changed_at and ends
        # at the NEXT transition's changed_at.
        transitions = hist_by_ride.get(ride_id, [])
        for i in range(len(transitions) - 1):
            cur_t = transitions[i]
            next_t = transitions[i + 1]
            new_status = cur_t.get("new_status")
            cur_ts = cur_t.get("changed_at")
            next_ts = next_t.get("changed_at")
            if not cur_ts or not next_ts:
                continue
            if new_status not in ("OPERATING", "DOWN"):
                continue
            try:
                start_dt = fromiso(cur_ts).astimezone(EASTERN)
                end_dt = fromiso(next_ts).astimezone(EASTERN)
            except ValueError:
                continue
            _bucket_minutes(
                acc, ride_id, park_id, new_status,
                start_dt, end_dt, park_hours,
            )

        # --- WAIT#-driven wait_mins accumulation ---
        for item in wait_by_ride.get(ride_id, []):
            n_wait_seen += 1
            polled_at = item.get("polled_at")
            wait_mins = item.get("wait_mins")
            if polled_at is None or wait_mins is None:
                continue
            try:
                dt_et = fromiso(polled_at).astimezone(EASTERN)
            except ValueError:
                continue
            if not _within_park_hours(
                park_hours, park_id, dt_et, polled_at
            ):
                continue
            _accumulate_wait(acc, ride_id, park_id, dt_et, int(wait_mins))

    return acc, n_wait_seen


def _compute_minutes_sqlite(
    con: sqlite3.Connection, rides_meta: dict, park_hours: dict
) -> tuple[dict, int, int]:
    """SQLite-backed wall-clock-minute accumulation.

    Walks wait_history once per ride in (polled_at) order. For each
    consecutive poll pair (poll_N, poll_{N+1}), attributes the gap
    (capped at MAX_INTERPOLL_GAP_MINUTES) to poll_N's status. The
    gap-attribution model naturally handles Pi's multi-stream
    polling: when the poller density is higher, individual gaps
    are smaller, but the total time attributed to each status is
    the same. The active/down minute totals end up cadence-
    independent.

    WAIT# observations (status='OPERATING' with non-null wait_mins)
    are accumulated into wait_sum/wait_n/max_wait separately —
    averaging wait_mins values is correct regardless of how many
    samples we have.

    Returns:
        (accumulators, n_polls_seen, n_outside_park_hours)
    """
    fromiso = datetime.fromisoformat
    acc = _new_accumulators()
    n_seen = 0
    n_outside_ph = 0
    max_gap_sec = MAX_INTERPOLL_GAP_MINUTES * 60

    for ride_id, meta in rides_meta.items():
        park_id = meta["park_id"]
        rows = con.execute(
            "SELECT polled_at, status, wait_mins FROM wait_history "
            "WHERE ride_id=? ORDER BY polled_at",
            (ride_id,),
        )
        prev_dt = None
        prev_status = None
        for polled_at, status, wait_mins in rows:
            n_seen += 1
            try:
                dt_et = fromiso(polled_at).astimezone(EASTERN)
            except ValueError:
                continue

            if prev_dt is not None and prev_status is not None:
                gap_sec = (dt_et - prev_dt).total_seconds()
                # Attribute the gap to prev_status (the status the
                # ride was in during the gap interval). Cap at
                # max_gap_sec so a multi-hour outage doesn't bias
                # the totals.
                if gap_sec > 0:
                    gap_capped_sec = min(gap_sec, max_gap_sec)
                    gap_end = prev_dt + timedelta(seconds=gap_capped_sec)
                    # Park-hours filter happens inside _bucket_minutes
                    # per-slice; track polls outside park hours for
                    # the final summary using the prev_dt timestamp
                    # as the representative timestamp for this gap.
                    if not _within_park_hours(
                        park_hours, park_id, prev_dt, prev_dt.isoformat()
                    ):
                        n_outside_ph += 1
                    _bucket_minutes(
                        acc, ride_id, park_id, prev_status,
                        prev_dt, gap_end, park_hours,
                    )

            # Accumulate WAIT# observation for avg_wait/max_wait.
            if status == "OPERATING" and wait_mins is not None:
                if _within_park_hours(
                    park_hours, park_id, dt_et, polled_at
                ):
                    _accumulate_wait(acc, ride_id, park_id, dt_et, wait_mins)

            prev_dt = dt_et
            prev_status = status

    return acc, n_seen, n_outside_ph


def _bucket_minutes(
    accumulators: dict,
    ride_id: str,
    park_id: str,
    status: str,
    start_dt: datetime,
    end_dt: datetime,
    park_hours: dict,
) -> None:
    """Distribute the wall-clock minutes from start_dt to end_dt
    into the (ride, hour, dow) and (park, hour, dow) accumulators.

    Status is one of 'OPERATING' | 'DOWN' — caller filters out
    'CLOSED' and 'REFURBISHMENT' before calling. Both OPERATING and
    DOWN contribute to "active" (ride was in-service or trying to
    be); only DOWN also contributes to "down."

    The duration is sliced at hour boundaries so a 30-min run from
    10:45 → 11:15 attributes 15 min to (h=10) and 15 min to (h=11),
    matching how the poll-count version attributed per-poll based
    on each poll's individual hour.

    Park-hours filter is applied per-slice: a slice falling outside
    the (park, park-day) operating window is dropped entirely.
    Matches the SQLite poll-count path's per-poll filter.
    """
    if end_dt <= start_dt:
        return
    if status not in ("OPERATING", "DOWN"):
        return

    is_down = status == "DOWN"
    cur = start_dt
    while cur < end_dt:
        # Slice at the next hour boundary (in ET).
        next_hour = (cur + timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0
        )
        slice_end = min(next_hour, end_dt)
        slice_minutes = (slice_end - cur).total_seconds() / 60
        if slice_minutes <= 0:
            cur = slice_end
            continue

        # Park-hours filter for this slice. Use cur's ET timestamp
        # as the park-day key and the comparison anchor.
        slice_polled_at = cur.isoformat()
        if not _within_park_hours(park_hours, park_id, cur, slice_polled_at):
            cur = slice_end
            continue

        hour = cur.hour
        dow_raw = (cur.weekday() + 1) % 7
        heatmap_dow = (
            (dow_raw - 1) % 7 if hour < PARK_DAY_BOUNDARY_HOUR else dow_raw
        )

        accumulators["ride_active_min"][ride_id] += slice_minutes
        accumulators["rh_active_min"][(ride_id, hour)] += slice_minutes
        accumulators["rdh_active_min"][(ride_id, heatmap_dow, hour)] += slice_minutes
        accumulators["pdh_active_min"][(park_id, heatmap_dow, hour)] += slice_minutes
        if is_down:
            accumulators["ride_down_min"][ride_id] += slice_minutes
            accumulators["rh_down_min"][(ride_id, hour)] += slice_minutes
            accumulators["rdh_down_min"][(ride_id, heatmap_dow, hour)] += slice_minutes

        cur = slice_end


def _new_accumulators() -> dict:
    """Build a fresh accumulator dict shared by SQLite and DDB
    streaming passes. _min suffixes hold wall-clock minutes (post-
    cutover semantics); wait_* hold per-WAIT#-row sums and counts
    (unchanged from the poll-count era — averaging wait_mins across
    samples is correct regardless of cadence)."""
    return {
        # Per-ride
        "ride_active_min": defaultdict(float),
        "ride_down_min": defaultdict(float),
        "ride_max_wait": defaultdict(lambda: None),
        "ride_wait_sum": defaultdict(int),
        "ride_wait_n": defaultdict(int),
        # Per-(ride, hour ET)
        "rh_active_min": defaultdict(float),
        "rh_down_min": defaultdict(float),
        "rh_wait_sum": defaultdict(int),
        "rh_wait_n": defaultdict(int),
        # Per-(ride, dow ET, hour ET)
        "rdh_active_min": defaultdict(float),
        "rdh_down_min": defaultdict(float),
        "rdh_wait_sum": defaultdict(int),
        "rdh_wait_n": defaultdict(int),
        # Per-(park_id, dow ET, hour ET)
        "pdh_active_min": defaultdict(float),
        "pdh_wait_sum": defaultdict(int),
        "pdh_wait_n": defaultdict(int),
    }


def _accumulate_wait(
    accumulators: dict,
    ride_id: str,
    park_id: str,
    dt_et: datetime,
    wait_mins: int,
) -> None:
    """Bucket a single WAIT# observation into wait_sum/wait_n/
    max_wait accumulators across the (ride, hour, dow) cells.
    Independent of the duration-based active/down accumulators —
    those use HIST# transitions, this uses individual WAIT# rows."""
    hour = dt_et.hour
    dow_raw = (dt_et.weekday() + 1) % 7
    heatmap_dow = (
        (dow_raw - 1) % 7 if hour < PARK_DAY_BOUNDARY_HOUR else dow_raw
    )
    accumulators["ride_wait_sum"][ride_id] += wait_mins
    accumulators["ride_wait_n"][ride_id] += 1
    accumulators["rh_wait_sum"][(ride_id, hour)] += wait_mins
    accumulators["rh_wait_n"][(ride_id, hour)] += 1
    accumulators["rdh_wait_sum"][(ride_id, heatmap_dow, hour)] += wait_mins
    accumulators["rdh_wait_n"][(ride_id, heatmap_dow, hour)] += 1
    accumulators["pdh_wait_sum"][(park_id, heatmap_dow, hour)] += wait_mins
    accumulators["pdh_wait_n"][(park_id, heatmap_dow, hour)] += 1
    current_max = accumulators["ride_max_wait"][ride_id]
    if current_max is None or wait_mins > current_max:
        accumulators["ride_max_wait"][ride_id] = wait_mins


def _park_day_iso(dt_et: datetime) -> str:
    """Return the ISO date this ET timestamp belongs to in 'park-day' terms.

    Polls before 4am ET attribute to the previous calendar day, matching
    the same boundary-shift the heatmap aggregator uses. A 1am Friday
    poll belongs to Thursday's park-day.
    """
    if dt_et.hour < PARK_DAY_BOUNDARY_HOUR:
        return (dt_et.date() - timedelta(days=1)).isoformat()
    return dt_et.date().isoformat()


def _derive_park_hours(con: sqlite3.Connection, rides_meta: dict) -> dict:
    """Derive (open_iso, close_iso) per (park_id, park_date) from the data.

    Heuristic: the operating window is bounded by the earliest and
    latest polled_at where ANY ride at that park had status in
    (OPERATING, DOWN). Both signals mean "the API considers the ride
    in service or trying to be." Polls labeled CLOSED are explicit
    park-closed signals and don't extend the window; REFURBISHMENT
    is multi-week and unrelated to operating hours.

    Could fetch authoritative hours from themeparks.wiki /schedule
    instead, but the historical-schedule API doesn't reliably go back
    months, and deriving from the data itself is self-consistent and
    requires no external calls.
    """
    fromiso = datetime.fromisoformat
    park_hours: dict[tuple[str, str], list[str]] = {}
    for ride_id, status, polled_at in con.execute(
        "SELECT ride_id, status, polled_at FROM wait_history "
        "WHERE status IN ('OPERATING', 'DOWN')"
    ):
        meta = rides_meta.get(ride_id)
        if not meta:
            continue
        try:
            dt_et = fromiso(polled_at).astimezone(EASTERN)
        except ValueError:
            continue
        key = (meta["park_id"], _park_day_iso(dt_et))
        cur = park_hours.get(key)
        if cur is None:
            park_hours[key] = [polled_at, polled_at]
        else:
            if polled_at < cur[0]:
                cur[0] = polled_at
            if polled_at > cur[1]:
                cur[1] = polled_at
    # Freeze list → tuple for compactness; consumers use indexing.
    return {k: (v[0], v[1]) for k, v in park_hours.items()}


def _within_park_hours(
    park_hours: dict, park_id: str, dt_et: datetime, polled_at: str
) -> bool:
    """True if this poll falls within the derived operating window."""
    key = (park_id, _park_day_iso(dt_et))
    bounds = park_hours.get(key)
    if not bounds:
        # No active polls for this (park, park-day) at all — could be
        # park entirely closed or a data gap. Either way, don't count
        # any poll from this date as "active."
        return False
    return bounds[0] <= polled_at <= bounds[1]


def _detect_down_clusters(
    con: sqlite3.Connection, rides_meta: dict, park_hours: dict
) -> tuple[dict, dict]:
    """Detect contiguous DOWN runs per ride.

    Streams wait_history ordered by (ride_id, polled_at). Within a
    ride, a "cluster" is a sequence of DOWN polls with gaps no larger
    than GAP_TOLERANCE_MINUTES (handles the occasional missed poll on
    the 2-min cadence). When the status flips to non-DOWN, the gap
    grows too large, the ride changes, or the poll falls outside park
    hours, the current cluster closes.

    Only clusters lasting >= MIN_CLUSTER_MINUTES are emitted — single-
    poll DOWN events are flap-style, not "clusters." Clusters lasting
    >= LONG_CLUSTER_MINUTES contribute to the per-(ride, dow, hour)
    long-cluster poll counts so cells can compute a
    `recurring_down_fraction` signal.

    Returns:
        (clusters_by_ride, long_cluster_polls)

        clusters_by_ride: {ride_id: [cluster_dict, ...]} where each
            cluster_dict has start_ts, end_ts, duration_minutes,
            poll_count, start_hour (ET), start_dow (heatmap-shifted).

        long_cluster_polls: {(ride_id, heatmap_dow, hour): int} —
            counts of DOWN polls inside long clusters, keyed by the
            same (ride, dow, hour) buckets as the heatmap cells.
    """
    fromiso = datetime.fromisoformat
    clusters_by_ride: dict = defaultdict(list)
    long_cluster_polls: dict = defaultdict(int)

    state = {
        "ride_id": None,
        "park_id": None,
        "start_ts": None,   # raw UTC ISO string of first DOWN poll
        "start_dt": None,   # ET datetime of first DOWN poll (for hour/dow)
        "last_ts": None,    # raw UTC ISO string of latest DOWN poll
        "last_dt": None,    # ET datetime of latest (for time-gap calc)
        "poll_count": 0,
        # Per-poll (ride_id, heatmap_dow, hour) keys, buffered so we
        # only attribute to long_cluster_polls if the cluster actually
        # ends up long.
        "pending_keys": [],
    }

    def close():
        if state["start_dt"] is None:
            return
        duration = (state["last_dt"] - state["start_dt"]).total_seconds() / 60
        if duration >= MIN_CLUSTER_MINUTES and state["ride_id"]:
            start_hour = state["start_dt"].hour
            start_dow = (state["start_dt"].weekday() + 1) % 7
            heatmap_dow = (
                (start_dow - 1) % 7
                if start_hour < PARK_DAY_BOUNDARY_HOUR
                else start_dow
            )
            clusters_by_ride[state["ride_id"]].append({
                # Both timestamps in raw UTC ISO so they're directly
                # comparable. Consumers can convert to ET if they want
                # to display them — duration_minutes is already
                # tz-agnostic.
                "start_ts": state["start_ts"],
                "end_ts": state["last_ts"],
                "duration_minutes": int(round(duration)),
                "poll_count": state["poll_count"],
                "start_hour": start_hour,
                "start_dow": heatmap_dow,
            })
            if duration >= LONG_CLUSTER_MINUTES:
                for k in state["pending_keys"]:
                    long_cluster_polls[k] += 1
        state["start_ts"] = None
        state["start_dt"] = None
        state["last_ts"] = None
        state["last_dt"] = None
        state["poll_count"] = 0
        state["pending_keys"] = []

    for ride_id, status, polled_at in con.execute(
        "SELECT ride_id, status, polled_at FROM wait_history "
        "ORDER BY ride_id, polled_at"
    ):
        if ride_id != state["ride_id"]:
            close()
            state["ride_id"] = ride_id
            meta = rides_meta.get(ride_id)
            state["park_id"] = meta["park_id"] if meta else None

        if not state["park_id"]:
            continue

        try:
            dt_et = fromiso(polled_at).astimezone(EASTERN)
        except ValueError:
            continue

        # Park-hours filter — same as the main aggregator. A cluster
        # spanning into a closed window terminates at the boundary.
        if not _within_park_hours(
            park_hours, state["park_id"], dt_et, polled_at
        ):
            close()
            continue

        if status == "DOWN":
            # Time-gap check before extending an open cluster.
            if state["start_dt"] is not None:
                gap = (dt_et - state["last_dt"]).total_seconds() / 60
                if gap > GAP_TOLERANCE_MINUTES:
                    close()
            if state["start_dt"] is None:
                state["start_ts"] = polled_at
                state["start_dt"] = dt_et
            state["last_ts"] = polled_at
            state["last_dt"] = dt_et
            state["poll_count"] += 1
            # Record the (ride, heatmap_dow, hour) bucket this poll
            # belongs to so we can attribute to long_cluster_polls
            # later if the cluster ends up long.
            hour = dt_et.hour
            dow_raw = (dt_et.weekday() + 1) % 7
            heatmap_dow = (
                (dow_raw - 1) % 7
                if hour < PARK_DAY_BOUNDARY_HOUR
                else dow_raw
            )
            state["pending_keys"].append((ride_id, heatmap_dow, hour))
        else:
            close()

    close()
    return dict(clusters_by_ride), dict(long_cluster_polls)


def _compute_ll_drop_analytics(con: sqlite3.Connection) -> dict[str, dict]:
    """Per-ride Lightning Lane drop pattern aggregations.

    A "drop" is an LL state change where Disney moves a ride's
    next-available return time EARLIER on the same calendar day:
      - both old_return_time and new_return_time present
      - new_return_time < old_return_time
      - both states are AVAILABLE (filters out SOLD_OUT transitions
        and other state-only changes that aren't actionable for guests)
      - all three timestamps (changed_at, old return, new return)
        fall on the same ET calendar date (excludes overnight resets
        where yesterday's late slot rolls to today's morning slot —
        those aren't actionable drops a guest can grab)

    For each ride that had any drops in the snapshot window, returns:
      ll_drops_total: total same-day drops observed
      ll_drop_hours: histogram by ET hour of day [{hour, count}, ...]
      ll_drop_dow: histogram by day of week (0=Sun..6=Sat to match
        the rest of the snapshot's dow convention) [{dow, count}, ...]
      ll_typical_shift_mins: median time-shift in minutes (how much
        earlier the slot typically moves on a drop)
      ll_active_days: distinct ET days the ride had any LL data
      ll_drops_per_active_day: drops_total / active_days, a rough
        "how often does this ride's LL refresh on a given day" baseline

    Rides with zero drops are absent from the returned dict — caller
    treats absence as "no drop data" rather than "zero drops" (could
    mean no LL offering at all, not necessarily that Disney never
    refreshes the slot).
    """
    out: dict[str, dict] = defaultdict(lambda: {
        "ll_drops_total": 0,
        "drop_hours": defaultdict(int),
        "drop_dow": defaultdict(int),
        "shifts": [],
    })
    active_days_by_ride: dict[str, set] = defaultdict(set)

    cursor = con.execute("""
        SELECT ride_id, old_state, new_state,
               old_return_time, new_return_time, changed_at
        FROM ll_history
        WHERE old_return_time IS NOT NULL AND new_return_time IS NOT NULL
    """)

    for ride_id, old_state, new_state, old_rt, new_rt, changed_at in cursor:
        try:
            changed_dt = datetime.fromisoformat(changed_at).astimezone(EASTERN)
        except (ValueError, AttributeError):
            continue
        # Track "active days" using every LL row, not just drops —
        # gives us a rate-per-day denominator that reflects how
        # often the ride's LL system was active at all.
        active_days_by_ride[ride_id].add(changed_dt.date())

        # Only AVAILABLE→AVAILABLE return-time changes count as drops.
        if old_state != "AVAILABLE" or new_state != "AVAILABLE":
            continue
        try:
            old_dt = datetime.fromisoformat(old_rt).astimezone(EASTERN)
            new_dt = datetime.fromisoformat(new_rt).astimezone(EASTERN)
        except ValueError:
            continue
        if new_dt >= old_dt:
            continue  # not a drop (slot moved later or unchanged)
        # All three timestamps must be the same ET date
        if not (
            old_dt.date() == new_dt.date() == changed_dt.date()
        ):
            continue

        d = out[ride_id]
        d["ll_drops_total"] += 1
        d["drop_hours"][changed_dt.hour] += 1
        # Python weekday(): Mon=0..Sun=6. Convert to SQLite strftime
        # convention 0=Sun..6=Sat to match the rest of the snapshot.
        sqlite_dow = (changed_dt.weekday() + 1) % 7
        d["drop_dow"][sqlite_dow] += 1
        shift_mins = (old_dt - new_dt).total_seconds() / 60
        d["shifts"].append(shift_mins)

    finalized: dict[str, dict] = {}
    for ride_id, d in out.items():
        if d["ll_drops_total"] == 0:
            continue
        shifts = sorted(d["shifts"])
        mid = len(shifts) // 2
        median_shift = (
            shifts[mid] if len(shifts) % 2 == 1
            else (shifts[mid - 1] + shifts[mid]) / 2
        )
        active = len(active_days_by_ride[ride_id])
        finalized[ride_id] = {
            "ll_drops_total": d["ll_drops_total"],
            "ll_drop_hours": [
                {"hour": h, "count": c}
                for h, c in sorted(d["drop_hours"].items())
            ],
            "ll_drop_dow": [
                {"dow": dow, "count": c}
                for dow, c in sorted(d["drop_dow"].items())
            ],
            "ll_typical_shift_mins": round(median_shift, 1),
            "ll_active_days": active,
            "ll_drops_per_active_day": round(
                d["ll_drops_total"] / active, 2
            ) if active > 0 else None,
        }
    return finalized


def _write_short_wait_baselines(rides_list: list, generated_at: str) -> None:
    """Emit infra/lambda/poller/baselines.json for short-wait alerts.

    Per (ride_id, hour-ET): a threshold below which we'd consider the
    current wait "short enough to ping people about." Computed as half
    the typical operating wait at that hour, capped at SHORT_WAIT_
    THRESHOLD_CAP, and only emitted when the typical wait clears
    MIN_INTERESTING_AVG_WAIT (so we don't alert on rides that are
    always short during quiet hours).

    Schema:
        {
          "generated_at": "...",
          "min_avg_wait_for_threshold": 25,
          "max_threshold": 30,
          "rides": {
            "<ride_id>": {
              "<hour>": <threshold_int_minutes>
            }
          }
        }
    """
    out: dict = {}
    for ride in rides_list:
        thresholds: dict[str, int] = {}
        for entry in ride["hourly_wait"]:
            avg = entry["wait"]
            if avg < MIN_INTERESTING_AVG_WAIT:
                continue
            threshold = min(SHORT_WAIT_THRESHOLD_CAP, round(avg * 0.5))
            # Threshold must still be a useful gate — if it rounds to 0
            # or below the floor, skip. A threshold of 5 min is fine; a
            # threshold of 1 isn't actionable.
            if threshold < 5:
                continue
            thresholds[str(entry["hour"])] = threshold
        if thresholds:
            out[ride["ride_id"]] = thresholds

    payload = {
        "generated_at": generated_at,
        "min_avg_wait_for_threshold": MIN_INTERESTING_AVG_WAIT,
        "max_threshold": SHORT_WAIT_THRESHOLD_CAP,
        "rides": out,
    }
    BASELINES_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINES_PATH.write_text(json.dumps(payload, indent=2))
    print(f"  wrote {BASELINES_PATH}")
    print(
        f"    rides with thresholds: {len(out)} / {len(rides_list)}, "
        f"size: {BASELINES_PATH.stat().st_size / 1024:.1f} KB"
    )


# ─── DDB source (M6-B Phase 4) ──────────────────────────────────────
# Parallel implementation of the four passes against DynamoDB. The
# Pi snapshot is the row-by-row source of truth; DDB stores a
# compressed representation (WAIT# = one row per OPERATING poll,
# HIST# = one row per status transition). The DDB-mode passes
# either consume the compressed shape directly (cluster detection
# walks transitions) or synthesize the missing per-poll rows from
# transition intervals (the main streaming pass synthesizes DOWN
# polls at the 2-min cadence between cluster open and close).
#
# LL drop analytics has no DDB-side data yet; --source ddb returns
# an empty dict. When LL changes start replicating to DDB (post-
# cutover follow-up), add a parallel ll_history table or HIST#-
# style sub-row and a DDB reader here.


def _ddb_table():
    """Return a boto3 Table resource for the DDB single table."""
    if boto3 is None:
        print(
            "boto3 not importable — install with: pip install boto3",
            file=sys.stderr,
        )
        sys.exit(1)
    session = boto3.Session(profile_name=DDB_PROFILE, region_name=DDB_REGION)
    return session.resource("dynamodb").Table(DDB_TABLE_NAME)


def _query_all(table, **kwargs) -> list[dict]:
    """Run a Query and paginate through all pages.

    Wraps the LastEvaluatedKey / ExclusiveStartKey loop so callers
    don't have to. Returns the full Items list. Used for per-ride
    Queries against WAIT# and HIST# subkeys — pages here are
    typically 2-5 per ride at 1MB each, so an upfront list is
    cheap and lets callers iterate normally.
    """
    items: list[dict] = []
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            return items
        kwargs["ExclusiveStartKey"] = last_key


def _scan_all(table, **kwargs) -> list[dict]:
    """Run a Scan and paginate through all pages.

    Same pattern as _query_all but for Scan operations. Used once
    per aggregator run to find STATE rows for ride metadata. A
    future GSI on (SK = 'STATE') would let us replace this with a
    cheap Query — see PROJECT.md item #2. Until then, this scans
    the full table (~5GB at current size, ~$0.30 RCU cost) once
    per run, which is fine for manual/scheduled regen but would
    be expensive on a per-hour cadence.

    Pagination is required (do NOT change to single-page) — the
    table is far past one Scan page, and a one-page scan would
    silently return zero STATE items as soon as the WAIT# rows
    push STATE off the first page. See the 2026-05-24 silent
    regression in CLAUDE.md / TESTING.md for the same category.
    """
    items: list[dict] = []
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            return items
        kwargs["ExclusiveStartKey"] = last_key


def _load_ride_meta_ddb(table) -> dict:
    """Load ride metadata from STATE rows via paginated Scan.

    Returns the same {ride_id: {name, park_id}} shape the SQLite
    path returns. STATE rows carry the canonical ride_name and
    park_id that HIST# / WAIT# rows reference.
    """
    print("  scanning DDB for STATE rows (one-time per run)…")
    t0 = time.time()
    items = _scan_all(
        table,
        FilterExpression="SK = :sk",
        ExpressionAttributeValues={":sk": "STATE"},
        ProjectionExpression="ride_id, #n, park_id",
        ExpressionAttributeNames={"#n": "name"},
    )
    print(f"    scanned in {time.time() - t0:.1f}s, found {len(items)} STATE rows")
    return {
        item["ride_id"]: {"name": item["name"], "park_id": item["park_id"]}
        for item in items
        if item.get("park_id") in PARK_IDS  # WDW-only filter
    }


# Number of concurrent boto3 Query workers used in _prefetch_ddb.
# 8 is conservative against the on-demand account-wide read throttle
# (~3000 RCU/sec). Each worker reads sequential pages for one ride;
# 8 rides in flight × ~50K items each averages well below throttle.
# Bumping to 16 would cut wall time further but risks transient
# ProvisionedThroughputExceededExceptions on cold-start.
_DDB_PREFETCH_WORKERS = 8


def _prefetch_ddb(table, rides_meta: dict) -> tuple[dict, dict]:
    """Pre-fetch all WAIT# and HIST# rows for every ride into memory.

    Replaces what would otherwise be three separate full-WAIT#
    sweeps per ride (date-range, park-hours, polls iter) plus one
    HIST# sweep — ~6,000 paginated Query calls — with a single
    sweep that holds the rows in memory for downstream passes to
    consume.

    Uses a thread pool because DDB Query is I/O-bound (each
    paginated round-trip is ~100-300ms of network latency). With
    8 workers the wall time drops from ~30-150 min (sequential)
    to ~3-5 min — same RCU cost, just no longer serialized on
    network latency.

    Memory cost: ~1.4 GB for ~2.4M WAIT# items at the projected
    shape. Acceptable on a developer machine; would be the wrong
    pattern in a Lambda. If the aggregator ever moves into a
    smaller-memory environment, replace with a streaming-per-ride
    pass that fuses all four computations into one loop.
    """
    print(f"  prefetching WAIT# + HIST# from DDB ({_DDB_PREFETCH_WORKERS} workers)…")
    t0 = time.time()
    wait_by_ride: dict[str, list[dict]] = {}
    hist_by_ride: dict[str, list[dict]] = {}

    def _pull_wait(ride_id: str) -> tuple[str, list[dict]]:
        return ride_id, _query_all(
            table,
            KeyConditionExpression=(
                Key("PK").eq(f"RIDE#{ride_id}")
                & Key("SK").begins_with("WAIT#")
            ),
            ProjectionExpression="polled_at, wait_mins",
        )

    def _pull_hist(ride_id: str) -> tuple[str, list[dict]]:
        return ride_id, _query_all(
            table,
            KeyConditionExpression=(
                Key("PK").eq(f"RIDE#{ride_id}")
                & Key("SK").begins_with("HIST#")
            ),
            ProjectionExpression="old_status, new_status, changed_at",
        )

    with ThreadPoolExecutor(max_workers=_DDB_PREFETCH_WORKERS) as ex:
        wait_futures = [ex.submit(_pull_wait, rid) for rid in rides_meta]
        hist_futures = [ex.submit(_pull_hist, rid) for rid in rides_meta]
        n_done = 0
        last_log = t0
        total = len(wait_futures) + len(hist_futures)
        for fut in as_completed(wait_futures + hist_futures):
            ride_id, items = fut.result()
            # Demultiplex by checking which dict already has this ride.
            # Order of completion is non-deterministic; first-result-per-
            # ride goes to whichever dict is empty. The two pull functions
            # produce distinguishable shapes (WAIT# items have wait_mins,
            # HIST# items have new_status), so check shape instead.
            if items and "new_status" in items[0]:
                hist_by_ride[ride_id] = items
            elif items and "wait_mins" in items[0]:
                wait_by_ride[ride_id] = items
            elif ride_id in wait_by_ride:
                # Already have WAIT# for this ride, so this must be HIST#
                hist_by_ride[ride_id] = items
            else:
                wait_by_ride[ride_id] = items
            n_done += 1
            now = time.time()
            if now - last_log > 5:
                print(
                    f"    {n_done}/{total} fetches in {now - t0:.0f}s "
                    f"(WAIT#: {len(wait_by_ride)}, HIST#: {len(hist_by_ride)})"
                )
                last_log = now

    n_wait = sum(len(v) for v in wait_by_ride.values())
    n_hist = sum(len(v) for v in hist_by_ride.values())
    print(
        f"    prefetch done in {time.time() - t0:.1f}s — "
        f"{n_wait:,} WAIT# items, {n_hist:,} HIST# items"
    )
    return wait_by_ride, hist_by_ride


def _get_date_range_from_prefetch(wait_by_ride: dict) -> dict:
    """Compute {start, end_, n} from prefetched WAIT# rows.

    Walks all in-memory WAIT# items rather than firing 285 fresh
    Queries the way the first-pass implementation did. The earlier
    implementation also did a Select=COUNT pagination sweep per
    ride; the prefetch approach gets a free count from the cached
    list length. Same numeric output, but ~0 additional network
    cost.
    """
    n = 0
    min_ts: str | None = None
    max_ts: str | None = None
    for items in wait_by_ride.values():
        n += len(items)
        for item in items:
            polled_at = item["polled_at"]
            if min_ts is None or polled_at < min_ts:
                min_ts = polled_at
            if max_ts is None or polled_at > max_ts:
                max_ts = polled_at
    return {"start": min_ts or "", "end_": max_ts or "", "n": n}


def _derive_park_hours_ddb(wait_by_ride: dict, rides_meta: dict) -> dict:
    """DDB analog of _derive_park_hours.

    Reads from prefetched WAIT# rows. WAIT# rows are OPERATING-
    only, which is what we need: park hours bound by earliest /
    latest OPERATING poll. DOWN polls (the other contributor in
    the SQLite version) are a ~1% slice — the OPERATING bounds
    are essentially identical to OPERATING∪DOWN bounds.
    Documented trade-off: marginal edge-case where a park opens
    with all rides DOWN (e.g., a system-wide outage at park open)
    won't extend the window; this should be vanishingly rare and
    conservative (excludes data we couldn't trust anyway).
    """
    park_hours: dict[tuple[str, str], list[str]] = {}
    for ride_id, items in wait_by_ride.items():
        meta = rides_meta.get(ride_id)
        if not meta:
            continue
        for item in items:
            polled_at = item["polled_at"]
            try:
                dt_et = datetime.fromisoformat(polled_at).astimezone(EASTERN)
            except ValueError:
                continue
            key = (meta["park_id"], _park_day_iso(dt_et))
            cur = park_hours.get(key)
            if cur is None:
                park_hours[key] = [polled_at, polled_at]
            else:
                if polled_at < cur[0]:
                    cur[0] = polled_at
                if polled_at > cur[1]:
                    cur[1] = polled_at
    return {k: (v[0], v[1]) for k, v in park_hours.items()}


def _detect_down_clusters_ddb(
    hist_by_ride: dict, rides_meta: dict, park_hours: dict
) -> tuple[dict, dict]:
    """DDB analog of _detect_down_clusters.

    Algorithmically simpler than the SQLite version: HIST#
    transitions ARE the cluster boundaries. We walk transitions
    per ride; a `(*→DOWN)` opens a cluster, a `(DOWN→*)` closes
    it. duration_minutes = exact delta between the two timestamps;
    poll_count is synthesized as duration / POLL_INTERVAL_MINUTES.

    No GAP_TOLERANCE handling — transitions are exact, not
    sampled. A cluster that the live poller failed to close (e.g.,
    Lambda timeout missing the final state-change emit) shows up
    here as a cluster spanning to the next non-DOWN transition,
    which may be much later than the actual recovery. Rare edge
    case; acceptable given the transition shape's other benefits.

    Park-hours filter is applied at cluster boundaries: a cluster
    whose open or close falls outside park hours is dropped. (The
    SQLite version trims at the boundary instead. Practical
    difference is small for analytics.)
    """
    fromiso = datetime.fromisoformat
    clusters_by_ride: dict = defaultdict(list)
    long_cluster_polls: dict = defaultdict(int)

    for ride_id, meta in rides_meta.items():
        items = hist_by_ride.get(ride_id, [])
        # HIST# rows come back SK-sorted = changed_at-sorted ascending.
        open_ts = None
        open_dt = None
        for item in items:
            new_status = item.get("new_status")
            changed_at = item.get("changed_at")
            if not changed_at:
                continue
            try:
                dt_et = fromiso(changed_at).astimezone(EASTERN)
            except ValueError:
                continue

            if new_status == "DOWN":
                # Open a cluster (or replace an unclosed prior open
                # — shouldn't happen with valid transition data).
                open_ts = changed_at
                open_dt = dt_et
            elif open_ts is not None:
                # Close the open cluster.
                close_ts = changed_at
                close_dt = dt_et
                duration = (close_dt - open_dt).total_seconds() / 60
                if duration >= MIN_CLUSTER_MINUTES:
                    # Park-hours filter at the open boundary.
                    park_id = meta["park_id"]
                    if _within_park_hours(
                        park_hours, park_id, open_dt, open_ts
                    ):
                        start_hour = open_dt.hour
                        start_dow_raw = (open_dt.weekday() + 1) % 7
                        heatmap_dow = (
                            (start_dow_raw - 1) % 7
                            if start_hour < PARK_DAY_BOUNDARY_HOUR
                            else start_dow_raw
                        )
                        # Poll count synthesized from duration. The
                        # SQLite version counts actual DOWN polls;
                        # since live polling is exactly every 2 min,
                        # duration/2 matches actual count modulo
                        # the boundary fencepost (off by 1 poll at
                        # most per cluster).
                        poll_count = max(
                            1, int(round(duration / POLL_INTERVAL_MINUTES))
                        )
                        clusters_by_ride[ride_id].append({
                            "start_ts": open_ts,
                            "end_ts": close_ts,
                            "duration_minutes": int(round(duration)),
                            "poll_count": poll_count,
                            "start_hour": start_hour,
                            "start_dow": heatmap_dow,
                        })
                        if duration >= LONG_CLUSTER_MINUTES:
                            # Attribute the synthesized DOWN polls
                            # across hours the cluster spans. Walk
                            # from open_dt to close_dt at the poll
                            # cadence; each step buckets into its
                            # own (heatmap_dow, hour).
                            for k in _synth_poll_keys(
                                ride_id, open_dt, close_dt
                            ):
                                long_cluster_polls[k] += 1
                open_ts = None
                open_dt = None

    return dict(clusters_by_ride), dict(long_cluster_polls)


def _synth_poll_keys(ride_id: str, start_dt: datetime, end_dt: datetime):
    """Yield (ride_id, heatmap_dow, hour) keys for every 2-min slot
    between start_dt and end_dt (inclusive of start, exclusive of
    end). Used to bucket synthesized DOWN polls into the same
    cells the SQLite path attributes real DOWN polls to."""
    step = timedelta(minutes=POLL_INTERVAL_MINUTES)
    cur = start_dt
    while cur < end_dt:
        hour = cur.hour
        dow_raw = (cur.weekday() + 1) % 7
        heatmap_dow = (
            (dow_raw - 1) % 7 if hour < PARK_DAY_BOUNDARY_HOUR else dow_raw
        )
        yield (ride_id, heatmap_dow, hour)
        cur = cur + step


def _iter_polls_ddb(
    wait_by_ride: dict, hist_by_ride: dict, rides_meta: dict
):
    """Yield rows matching the SQLite `SELECT ride_id, status,
    wait_mins, polled_at FROM wait_history` shape, reconstructed
    from prefetched DDB rows.

    For each ride:
      - WAIT# rows → yield as ('OPERATING', wait_mins, polled_at)
      - HIST# DOWN intervals → synthesize 2-min DOWN polls

    CLOSED/REFURBISHMENT polls are NOT yielded (no DDB equivalent).
    They never contributed to the SQLite path's analytics either
    — the park-hours filter dropped them — so omission is
    behaviorally equivalent for the streaming pass's bucketing.

    Streaming pass is order-agnostic (pure accumulator pattern),
    so we don't enforce a global order; per-ride WAIT# rows come
    back SK-sorted naturally.
    """
    fromiso = datetime.fromisoformat
    for ride_id in rides_meta:
        # WAIT# pass → OPERATING polls.
        for item in wait_by_ride.get(ride_id, []):
            wait_mins = item.get("wait_mins")
            yield (
                ride_id,
                "OPERATING",
                int(wait_mins) if wait_mins is not None else None,
                item["polled_at"],
            )

        # HIST# pass → synthesize DOWN polls between (*→DOWN) and
        # (DOWN→*) transition pairs. Same boundary handling as
        # _detect_down_clusters_ddb but without the cluster-
        # minimum filter (every DOWN minute counts toward
        # ride_down regardless of whether it forms a "cluster").
        hist_items = hist_by_ride.get(ride_id, [])
        open_dt = None
        for item in hist_items:
            new_status = item.get("new_status")
            changed_at = item.get("changed_at")
            if not changed_at:
                continue
            try:
                dt_et = fromiso(changed_at).astimezone(EASTERN)
            except ValueError:
                continue
            if new_status == "DOWN":
                open_dt = dt_et
            elif open_dt is not None:
                close_dt = dt_et
                step = timedelta(minutes=POLL_INTERVAL_MINUTES)
                cur = open_dt
                while cur < close_dt:
                    yield (
                        ride_id,
                        "DOWN",
                        None,
                        cur.isoformat(),
                    )
                    cur = cur + step
                open_dt = None


def _compute_ll_drop_analytics_ddb(table) -> dict[str, dict]:
    """LL drop analytics in DDB mode — returns empty.

    LL state changes don't replicate to DDB yet. The Pi captures
    LL drops via the upstream Genie+ API but the live poller
    Lambda doesn't write LL-related rows. When LL replication
    lands as a follow-up, fill this in.

    Caller treats absence as "no drop data" just like a ride
    with zero recorded drops, so rides emit no ll_drop_* fields
    in DDB mode. Acceptable for the cutover; restore once the
    upstream LL writer is in place.
    """
    return {}


if __name__ == "__main__":
    main()
