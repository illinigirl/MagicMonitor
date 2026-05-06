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
from datetime import datetime, timezone
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
OUT_PATH = ROOT / "web" / "src" / "data" / "analytics-snapshot.ts"

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

    # --- stream wait_history, bucket in Python ---
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
    # Per-(park_id, dow ET, hour ET) accumulators for the heatmap
    pdh_wait_sum = defaultdict(int)
    pdh_wait_n = defaultdict(int)
    pdh_active = defaultdict(int)

    print("  streaming poll rows…")
    t_stream = time.time()
    n_seen = 0
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

        ride_total[ride_id] += 1

        is_operating = status == "OPERATING"
        is_active = is_operating or status == "DOWN"

        if is_active:
            ride_active[ride_id] += 1
            rh_active[(ride_id, hour)] += 1
            pdh_active[(meta["park_id"], dow, hour)] += 1
            if status == "DOWN":
                ride_down[ride_id] += 1
                rh_down[(ride_id, hour)] += 1

        if is_operating and wait_mins is not None:
            ride_wait_sum[ride_id] += wait_mins
            ride_wait_n[ride_id] += 1
            rh_wait_sum[(ride_id, hour)] += wait_mins
            rh_wait_n[(ride_id, hour)] += 1
            pdh_wait_sum[(meta["park_id"], dow, hour)] += wait_mins
            pdh_wait_n[(meta["park_id"], dow, hour)] += 1
            current_max = ride_max_wait[ride_id]
            if current_max is None or wait_mins > current_max:
                ride_max_wait[ride_id] = wait_mins

        if n_seen % 1_000_000 == 0:
            elapsed = time.time() - t_stream
            print(f"    {n_seen:,} rows in {elapsed:.1f}s")

    print(f"  streamed {n_seen:,} rows in {time.time() - t_stream:.1f}s")

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
        "rides": rides_list,
        "heatmaps": heatmaps,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    json_text = json.dumps(snapshot, indent=2, ensure_ascii=False)
    OUT_PATH.write_text(
        f"""// AUTO-GENERATED by tools/aggregate-analytics.py — do not edit by hand.
// Source: .scratch/disney-pi-snapshot.db
// Regenerate by re-snapshotting the Pi and running:
//   python3 tools/aggregate-analytics.py
//
// {len(rides_list)} rides × hourly + per-park hour×day heatmaps,
// aggregated from {snapshot['total_polls']:,} polls spanning
// {snapshot['date_range']['start'][:10]} → {snapshot['date_range']['end'][:10]}.

import type {{ AnalyticsSnapshot }} from "@/lib/analytics";

export const ANALYTICS_SNAPSHOT: AnalyticsSnapshot = {json_text};
"""
    )

    # Side-output: short-wait baselines for the poller Lambda.
    _write_short_wait_baselines(rides_list, snapshot["generated_at"])

    print(f"  total: {time.time() - t0:.1f}s")
    print(f"  wrote {OUT_PATH}")
    print(f"    rides: {len(rides_list)}")
    print(f"    heatmap cells: {sum(len(v) for v in heatmaps.values())}")
    print(f"    output size: {OUT_PATH.stat().st_size / 1024:.1f} KB")
    con.close()


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
