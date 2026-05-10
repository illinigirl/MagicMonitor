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

import json
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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

# Per-ride data with fewer than this many active polls is dropped —
# rides that closed for refurb during the data window or were rare
# event-only entities. 100 polls ≈ 3.3 hours of activity, low bar.
MIN_RIDE_POLLS = 100
# Heatmap cell needs at least this many active polls to be shown,
# else we treat it as "park closed at this hour" and leave the cell
# empty in the UI.
MIN_HEATMAP_CELL_POLLS = 20
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


def main() -> None:
    if not DB_PATH.exists():
        print(f"Snapshot not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    print(f"Reading {DB_PATH}")
    t0 = time.time()
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row

    # --- ride metadata ---
    rides_meta = {}  # ride_id → {name, park_id}
    for row in con.execute("SELECT id, name, park_id FROM rides"):
        rides_meta[row["id"]] = {"name": row["name"], "park_id": row["park_id"]}
    print(f"  rides: {len(rides_meta)}")

    # --- date range (one cheap query) ---
    range_row = con.execute(
        "SELECT MIN(polled_at) AS start, MAX(polled_at) AS end_, COUNT(*) AS n "
        "FROM wait_history"
    ).fetchone()
    print(
        f"  span: {range_row['start'][:10]} → {range_row['end_'][:10]}  "
        f"({range_row['n']:,} rows)"
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
    park_hours = _derive_park_hours(con, rides_meta)
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
    clusters_by_ride, long_cluster_polls = _detect_down_clusters(
        con, rides_meta, park_hours
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
    ll_drops_by_ride = _compute_ll_drop_analytics(con)
    n_rides_with_drops = len(ll_drops_by_ride)
    n_drops_total = sum(d["ll_drops_total"] for d in ll_drops_by_ride.values())
    print(
        f"  LL drop analytics: {n_drops_total:,} drops across "
        f"{n_rides_with_drops} rides"
    )

    # --- pass 2: stream wait_history, bucket in Python ---
    # Per-ride accumulators
    ride_total = defaultdict(int)
    ride_down = defaultdict(int)
    ride_active = defaultdict(int)
    ride_max_wait = defaultdict(lambda: None)
    ride_wait_sum = defaultdict(int)
    ride_wait_n = defaultdict(int)
    # Per-(ride, hour ET) accumulators
    rh_wait_sum = defaultdict(int)
    rh_wait_n = defaultdict(int)
    rh_down = defaultdict(int)
    rh_active = defaultdict(int)
    # Per-(ride, dow ET, hour ET) — fuller breakdown for "how does
    # this ride behave on Sundays?" type questions. Same dow shift
    # as the park heatmap so weekday/weekend semantics match.
    rdh_wait_sum = defaultdict(int)
    rdh_wait_n = defaultdict(int)
    rdh_down = defaultdict(int)
    rdh_active = defaultdict(int)
    # Per-(park_id, dow ET, hour ET) accumulators for the heatmap
    pdh_wait_sum = defaultdict(int)
    pdh_wait_n = defaultdict(int)
    pdh_active = defaultdict(int)

    print("  streaming poll rows…")
    t_stream = time.time()
    n_seen = 0
    n_closed_filtered = 0  # how many polls dropped by park-hours filter
    # tz-aware parser cache: most polls happen at the same minute-second
    # offsets, so caching by date prefix doesn't help much, but datetime
    # parsing in 3.11+ is fast enough at this scale.
    fromiso = datetime.fromisoformat
    for ride_id, status, wait_mins, polled_at in con.execute(
        "SELECT ride_id, status, wait_mins, polled_at FROM wait_history"
    ):
        n_seen += 1
        meta = rides_meta.get(ride_id)
        if not meta:
            continue

        # Convert UTC ISO → ET. fromisoformat handles "+00:00" suffix.
        # astimezone is the only place we pay tz-conversion cost; doing
        # it in pure Python (vs SQLite's datetime()) is dramatically
        # faster in practice because SQLite's parser is per-row Python.
        try:
            dt_utc = fromiso(polled_at)
            dt_et = dt_utc.astimezone(EASTERN)
        except ValueError:
            continue
        hour = dt_et.hour
        # weekday(): Mon=0..Sun=6. Convert to SQLite-style Sun=0..Sat=6
        # so the JS-side reads match what disney_dashboard.py emits.
        dow = (dt_et.weekday() + 1) % 7
        # The heatmap shows park-day flow, not calendar-day flow: a
        # 1am poll on calendar-Friday belongs to Thursday's park
        # evening (parks stay open past midnight for events / EEH).
        # Per-ride hourly buckets use the raw `hour` (no dow concern);
        # only the heatmap (dow, hour) accumulator reassigns.
        heatmap_dow = (dow - 1) % 7 if hour < PARK_DAY_BOUNDARY_HOUR else dow

        # Park-hours gate. If this poll falls outside the park's
        # derived operating window for its park-day, skip it entirely.
        # Doesn't affect total_polls (we still see the ride existed);
        # does affect everything that means "ride was active" or
        # "wait was reported."
        if not _within_park_hours(
            park_hours, meta["park_id"], dt_et, polled_at
        ):
            n_closed_filtered += 1
            continue

        ride_total[ride_id] += 1

        is_operating = status == "OPERATING"
        is_active = is_operating or status == "DOWN"

        if is_active:
            ride_active[ride_id] += 1
            rh_active[(ride_id, hour)] += 1
            rdh_active[(ride_id, heatmap_dow, hour)] += 1
            pdh_active[(meta["park_id"], heatmap_dow, hour)] += 1
            if status == "DOWN":
                ride_down[ride_id] += 1
                rh_down[(ride_id, hour)] += 1
                rdh_down[(ride_id, heatmap_dow, hour)] += 1

        if is_operating and wait_mins is not None:
            ride_wait_sum[ride_id] += wait_mins
            ride_wait_n[ride_id] += 1
            rh_wait_sum[(ride_id, hour)] += wait_mins
            rh_wait_n[(ride_id, hour)] += 1
            rdh_wait_sum[(ride_id, heatmap_dow, hour)] += wait_mins
            rdh_wait_n[(ride_id, heatmap_dow, hour)] += 1
            pdh_wait_sum[(meta["park_id"], heatmap_dow, hour)] += wait_mins
            pdh_wait_n[(meta["park_id"], heatmap_dow, hour)] += 1
            current_max = ride_max_wait[ride_id]
            if current_max is None or wait_mins > current_max:
                ride_max_wait[ride_id] = wait_mins

        if n_seen % 1_000_000 == 0:
            elapsed = time.time() - t_stream
            print(f"    {n_seen:,} rows in {elapsed:.1f}s")

    print(
        f"  streamed {n_seen:,} rows in {time.time() - t_stream:.1f}s "
        f"({n_closed_filtered:,} filtered out as outside park hours, "
        f"{100.0 * n_closed_filtered / max(n_seen, 1):.1f}%)"
    )

    # --- shape outputs ---
    rides_list = []
    for ride_id, meta in rides_meta.items():
        active = ride_active[ride_id]
        if active < MIN_RIDE_POLLS:
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
            ha = rh_active[(ride_id, h)]
            if ha >= MIN_HEATMAP_CELL_POLLS:
                hourly_downtime.append(
                    {"hour": h, "pct": round(100.0 * rh_down[(ride_id, h)] / ha, 1)}
                )

        # Per-(dow, hour) breakdown for this ride. Same MIN_HEATMAP_CELL_POLLS
        # gate as the park-level heatmap so cells with thin samples
        # don't show up as confident numbers.
        dow_hourly = []
        for dow in range(7):
            for h in range(24):
                key = (ride_id, dow, h)
                ha = rdh_active[key]
                if ha < MIN_HEATMAP_CELL_POLLS:
                    continue
                wn = rdh_wait_n[key]
                n_down = rdh_down[key]
                cell = {
                    "dow": dow,
                    "hour": h,
                    "downtime_pct": round(100.0 * n_down / ha, 1),
                    "n_active": ha,
                }
                # Only include `wait` when there's at least one operating
                # poll with a wait_mins value — wait isn't meaningful
                # when the ride was 100% DOWN at that (dow, hour).
                if wn > 0:
                    cell["wait"] = round(rdh_wait_sum[key] / wn)
                # `recurring_down_fraction`: of the DOWN polls in this
                # bucket, what fraction belonged to a long (>=2h) cluster?
                # 1.0 means every DOWN poll was part of a sustained
                # recurring-looking pattern; 0.0 means all DOWN polls
                # were flap-style. Omit when n_down=0 (no DOWN polls
                # to characterize).
                if n_down > 0:
                    long_count = long_cluster_polls.get(key, 0)
                    cell["recurring_down_fraction"] = round(
                        long_count / n_down, 2
                    )
                dow_hourly.append(cell)

        ll_drops = ll_drops_by_ride.get(ride_id, {})
        rides_list.append(
            {
                "ride_id": ride_id,
                "ride_name": meta["name"],
                "park_key": park_key,
                "total_polls": ride_total[ride_id],
                "downtime_pct": round(100.0 * ride_down[ride_id] / active, 1),
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
    for (park_id, dow, hour), n in pdh_active.items():
        if n < MIN_HEATMAP_CELL_POLLS:
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
                "n": n,
            }
        )
    for pk in heatmaps:
        heatmaps[pk].sort(key=lambda d: (d["dow"], d["hour"]))

    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "date_range": {"start": range_row["start"], "end": range_row["end_"]},
        "total_polls": range_row["n"],
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
    con.close()


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


if __name__ == "__main__":
    main()
