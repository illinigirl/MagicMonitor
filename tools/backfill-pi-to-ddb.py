#!/usr/bin/env python3
"""
Backfill Pi SQLite wait_history → DynamoDB WAIT# rows.

One-shot migration script. Reads `.scratch/disney-pi-snapshot.db`
(the Pi's wait_history table, ~5M OPERATING rows spanning ~2 months)
and writes one DDB WAIT# row per operating poll, matching the shape
the live poller's db.record_wait_observation() writes.

After running this against the full dataset, DDB contains the full
operational-wait history the analytics aggregator needs. The
aggregator can then be modified (M6-B Phase 4) to read DDB only,
the SQLite path can be dropped, and the Pi can be unplugged.

Scope this script covers:
  - WAIT# rows (one per OPERATING poll with non-null wait_mins) —
    same shape as live poller writes. Bulk of the data.
  - HIST# rows (one per status transition) — derived by walking
    each ride's poll sequence and emitting a row whenever status
    changes between consecutive polls. Required so the aggregator
    can reconstruct downtime windows from DDB alone.

Scope this script intentionally does NOT cover:
  - CLOSED / REFURBISHMENT / DOWN polls as WAIT# rows. The live
    poller doesn't write these and mirroring its shape keeps the
    DDB WAIT# contract consistent across native and backfilled
    data. Downtime is captured via HIST# transitions instead.

HIST# TTL: the live poller defaults HISTORY_RETENTION_DAYS=90 (set
in `infra/lib/disney-stack.ts`). Backfilled HIST# rows are stamped
with a 1825-day (5-year) TTL so the analytics aggregator has years
of downtime history. **Before running --mode hist or --mode both
in production, bump the CDK env var to 1825 and redeploy** —
otherwise new live HIST# rows continue expiring at 90 days and a
~3-month gap will appear in the historical record once the early
live rows age out.

Cost:
  - WAIT# pass: ~$6.30 for the full 5M-row write (5M × $1.25/M
    on-demand).
  - HIST# pass: tens of thousands of transitions; well under $0.10.

Idempotency: DDB put_item is upsert-by-(PK,SK). Re-running the
script on the same data is safe — same items, same content, no
duplicates. Use this to resume after an interrupted run.

Safety:
  - Defaults to --dry-run. Pass --execute to actually write.
  - --mode wait (default) | hist | both — selects which row type
    to backfill. Default is wait-only to preserve prior behavior;
    HIST# is opt-in.
  - --ride-id <id> filters to a single ride for testing.
  - --limit N caps the row count for testing.

Typical sequence:
    # Inspect schema + count what would be written (WAIT#):
    python3 tools/backfill-pi-to-ddb.py

    # Sanity-test against one ride (small write, ~$0.05):
    python3 tools/backfill-pi-to-ddb.py \\
        --ride-id 6fd1e225-53a0-4a80-a577-4bbc9a471075 --execute

    # Full WAIT# backfill (~5M rows, ~$6.30, ~1-3 hours runtime):
    python3 tools/backfill-pi-to-ddb.py --execute

    # HIST# backfill after CDK retention bump + deploy:
    python3 tools/backfill-pi-to-ddb.py --mode hist --execute
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / ".scratch" / "disney-pi-snapshot.db"

# Mirrors PARK_IDS in tools/aggregate-analytics.py. Single source of
# truth would be nicer, but the aggregator's import surface is dense
# enough that duplicating four KV pairs is cheaper than introducing
# a shared module just for this.
PARK_IDS = {
    "75ea578a-adc8-4116-a54d-dccb60765ef9": "magic_kingdom",
    "47f90d2c-e191-4239-a466-5892ef59a88b": "epcot",
    "288747d1-8b4f-4a64-867e-ea7c9b27bad8": "hollywood_studios",
    "1c84a229-8862-4648-9c71-378ddd2c7693": "animal_kingdom",
}

# Production table. The poller writes here; we're appending matching
# rows. There is no separate "test" partition — the DDB single-table
# pattern uses (PK, SK) for isolation. Backfilled WAIT# rows occupy
# (RIDE#<id>, WAIT#<old-timestamp>) keyspace, distinct from any
# live-poll keyspace, so they cannot collide.
TABLE_NAME = "DisneyData"
REGION = "us-east-2"
PROFILE = "watchtower"

# Mirrors WAIT_OBSERVATION_RETENTION_DAYS in infra/lambda/poller/db.py.
# Hard-coding here so the backfill works without importing the
# Lambda's db module (which assumes Lambda env vars are present).
# When the live poller's retention changes, update this too.
RETENTION_DAYS = 365

# HIST# retention. Live poller defaults to 90 days (see
# HISTORY_RETENTION_DAYS in infra/lambda/poller/db.py — alert-debug
# window). Backfilled history is for analytics: the aggregator's
# downtime reconstruction needs years, not weeks. Stamp 5 years on
# every backfilled HIST# row. **Bump the CDK env var to match
# before running this in execute mode**, otherwise new live HIST#
# rows age out at 90 days and a gap appears in the historical
# record. See module docstring.
HIST_RETENTION_DAYS = 1825


def _load_ride_meta(con: sqlite3.Connection) -> dict[str, dict]:
    """Map ride_id → {park_id, park_key, park_name, ride_name}. Rides
    outside the four WDW parks are dropped (the PARK_IDS map is the
    canonical filter). ride_name + park_name come from the rides
    table and are needed for HIST# enrichment (live poller writes
    them; aggregator + alert routing read them)."""
    meta = {}
    skipped = 0
    for ride_id, park_id, park_name, ride_name in con.execute(
        "SELECT id, park_id, park_name, name FROM rides"
    ):
        park_key = PARK_IDS.get(park_id)
        if not park_key:
            skipped += 1
            continue
        meta[ride_id] = {
            "park_id":   park_id,
            "park_key":  park_key,
            "park_name": park_name,
            "ride_name": ride_name,
        }
    print(f"  loaded {len(meta)} rides ({skipped} skipped: non-WDW park)")
    return meta


def _iter_wait_rows(
    con: sqlite3.Connection,
    ride_id_filter: str | None,
    limit: int | None,
):
    """Stream the OPERATING rows from wait_history.

    Filter pushes ride_id selection into SQLite (efficient) rather
    than walking the full 8.8M and filtering in Python. Status +
    wait_mins filters live in SQL for the same reason.
    """
    sql = (
        "SELECT ride_id, wait_mins, polled_at FROM wait_history "
        "WHERE status = 'OPERATING' AND wait_mins IS NOT NULL"
    )
    params: tuple = ()
    if ride_id_filter:
        sql += " AND ride_id = ?"
        params = (ride_id_filter,)
    if limit:
        sql += f" LIMIT {int(limit)}"
    return con.execute(sql, params)


def _count_target_rows(
    con: sqlite3.Connection,
    ride_id_filter: str | None,
    limit: int | None,
) -> int:
    """Cheap upfront count so the progress meter has a denominator
    and the user sees the expected total before any writes happen."""
    sql = (
        "SELECT COUNT(*) FROM wait_history "
        "WHERE status = 'OPERATING' AND wait_mins IS NOT NULL"
    )
    params: tuple = ()
    if ride_id_filter:
        sql += " AND ride_id = ?"
        params = (ride_id_filter,)
    n = con.execute(sql, params).fetchone()[0]
    if limit:
        n = min(n, limit)
    return n


def _build_item(
    ride_id: str, wait_mins: int, polled_at: str, park_key: str, ttl: int
) -> dict:
    """Build one DDB Item. Shape MUST match the live poller's
    db.record_wait_observation() — anything that consumes WAIT#
    rows assumes this exact set of attributes."""
    return {
        "PK":        f"RIDE#{ride_id}",
        "SK":        f"WAIT#{polled_at}",
        "wait_mins": int(wait_mins),
        "park_key":  park_key,
        "polled_at": polled_at,
        "ttl":       ttl,
    }


def _iter_status_transitions(
    con: sqlite3.Connection,
    rides: dict[str, dict],
    ride_id_filter: str | None,
    limit: int | None,
):
    """Stream status transitions for HIST# backfill.

    Walks `wait_history` ordered by (ride_id, polled_at) — index
    `idx_wait_history_ride` makes this cheap. For each consecutive
    pair of rows on the same ride, emits one transition tuple when
    status changes:

        (ride_id, old_status, new_status, wait_mins_at_change, changed_at)

    The first observed row per ride emits nothing — there's no
    prior state to transition from. This mirrors the live poller,
    which writes HIST# only when an observed status differs from
    the last one it saw, not on initial ride discovery.

    `limit` caps the number of emitted transitions (useful for
    dry-run sampling), not the number of source rows walked.
    """
    sql = (
        "SELECT ride_id, status, wait_mins, polled_at FROM wait_history "
        "WHERE status IS NOT NULL"
    )
    params: tuple = ()
    if ride_id_filter:
        sql += " AND ride_id = ?"
        params = (ride_id_filter,)
    sql += " ORDER BY ride_id, polled_at"

    last_status_per_ride: dict[str, str] = {}
    emitted = 0
    for ride_id, status, wait_mins, polled_at in con.execute(sql, params):
        if ride_id not in rides:
            continue
        prev = last_status_per_ride.get(ride_id)
        if prev is not None and prev != status:
            yield (ride_id, prev, status, wait_mins, polled_at)
            emitted += 1
            if limit is not None and emitted >= limit:
                return
        last_status_per_ride[ride_id] = status


def _build_hist_item(
    ride_id: str,
    ride_name: str,
    park_name: str,
    park_key: str,
    old_status: str,
    new_status: str,
    wait_mins: int | None,
    changed_at: str,
    ttl: int,
) -> dict:
    """Build one HIST# DDB Item. Shape MUST match the live poller's
    db.record_status_change() — the aggregator's downtime
    reconstruction reads these attributes by name."""
    return {
        "PK":         f"RIDE#{ride_id}",
        "SK":         f"HIST#{changed_at}",
        "ride_id":    ride_id,
        "ride_name":  ride_name,
        "park_name":  park_name,
        "park_key":   park_key,
        "old_status": old_status,
        "new_status": new_status,
        "wait_mins":  wait_mins,
        "changed_at": changed_at,
        "ttl":        ttl,
    }


def _print_sample_items(items: list[dict], n: int = 3) -> None:
    """Show the first few items in dry-run mode so the user can
    eyeball the shape before authorizing a real write."""
    print(f"\n  sample items (first {min(n, len(items))} of {len(items)}):")
    for item in items[:n]:
        print(f"    PK={item['PK']}  SK={item['SK']}")
        print(
            f"      wait_mins={item['wait_mins']}, park_key={item['park_key']!r}, "
            f"ttl={item['ttl']} ({_ttl_to_iso(item['ttl'])})"
        )


def _ttl_to_iso(ttl: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ttl, tz=timezone.utc).isoformat()


def _backfill(
    con: sqlite3.Connection,
    rides: dict[str, dict],
    ride_id_filter: str | None,
    limit: int | None,
    execute: bool,
    sample_every: int,
) -> None:
    """Drive the backfill. Streams rows, writes in batches of 25
    via boto3's batch_writer context manager (which handles
    unprocessed-items retry internally)."""
    total = _count_target_rows(con, ride_id_filter, limit)
    print(f"  target rows: {total:,}")
    if total == 0:
        print("  nothing to write.")
        return

    # Same +RETENTION_DAYS from-now expiry the live poller uses. The
    # backfilled rows therefore live for at least a year regardless of
    # how old the polled_at timestamps are — that's intentional, so
    # the analytics aggregator has a full year to consume them before
    # any TTL pressure appears.
    ttl = int(time.time()) + (RETENTION_DAYS * 86400)
    print(f"  TTL for all items: {ttl} ({_ttl_to_iso(ttl)})")

    if not execute:
        sample = []
        for i, (ride_id, wait_mins, polled_at) in enumerate(
            _iter_wait_rows(con, ride_id_filter, limit)
        ):
            ride_meta = rides.get(ride_id)
            if not ride_meta:
                continue
            sample.append(_build_item(
                ride_id, wait_mins, polled_at, ride_meta["park_key"], ttl
            ))
            if len(sample) >= 5:
                break
        _print_sample_items(sample)
        print(
            "\n  DRY RUN — no writes performed. Pass --execute to "
            "actually write to DDB."
        )
        return

    # Real write path.
    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    table = session.resource("dynamodb").Table(TABLE_NAME)

    print("  writing to DDB…")
    t0 = time.time()
    written = 0
    skipped = 0
    last_log = t0
    # batch_writer accumulates 25-item BatchWriteItem calls and
    # handles UnprocessedItems retries with backoff internally. This
    # is the right primitive for a long bulk-load — manual retry
    # logic here would just duplicate what boto3 already does.
    with table.batch_writer() as batch:
        for ride_id, wait_mins, polled_at in _iter_wait_rows(
            con, ride_id_filter, limit
        ):
            ride_meta = rides.get(ride_id)
            if not ride_meta:
                skipped += 1
                continue
            batch.put_item(Item=_build_item(
                ride_id, wait_mins, polled_at, ride_meta["park_key"], ttl
            ))
            written += 1
            now = time.time()
            if written % sample_every == 0 and now - last_log > 5:
                elapsed = now - t0
                rate = written / elapsed if elapsed else 0
                eta_sec = (total - written) / rate if rate else 0
                print(
                    f"    {written:,}/{total:,} "
                    f"({100.0 * written / total:.1f}%) in {elapsed:.0f}s "
                    f"@ {rate:,.0f}/s — ETA {eta_sec / 60:.1f} min"
                )
                last_log = now

    elapsed = time.time() - t0
    rate = written / elapsed if elapsed else 0
    print(
        f"\n  wrote {written:,} items in {elapsed:.1f}s @ {rate:,.0f}/s "
        f"({skipped:,} skipped: ride not in WDW parks)"
    )


def _backfill_hist(
    con: sqlite3.Connection,
    rides: dict[str, dict],
    ride_id_filter: str | None,
    limit: int | None,
    execute: bool,
    sample_every: int,
) -> None:
    """Drive the HIST# backfill. Streams transitions from
    `_iter_status_transitions` and writes them as HIST# rows.

    No upfront total — counting transitions requires walking the
    full 8.79M rows anyway, so we just stream and report rate.
    """
    print("\n--- HIST# pass ---")
    ttl = int(time.time()) + (HIST_RETENTION_DAYS * 86400)
    print(
        f"  TTL for all HIST# items: {ttl} ({_ttl_to_iso(ttl)})  "
        f"[{HIST_RETENTION_DAYS} days]"
    )

    if not execute:
        sample = []
        for tup in _iter_status_transitions(
            con, rides, ride_id_filter, limit=5
        ):
            ride_id, old_status, new_status, wait_mins, changed_at = tup
            meta = rides[ride_id]
            sample.append(_build_hist_item(
                ride_id, meta["ride_name"], meta["park_name"],
                meta["park_key"], old_status, new_status,
                wait_mins, changed_at, ttl,
            ))
        print(f"\n  sample transitions (first {len(sample)}):")
        for item in sample:
            print(f"    PK={item['PK']}  SK={item['SK']}")
            print(
                f"      {item['old_status']} → {item['new_status']}  "
                f"wait_mins={item['wait_mins']}  "
                f"park_key={item['park_key']!r}"
            )
        print(
            "\n  DRY RUN — no writes performed. Pass --execute to "
            "actually write to DDB."
        )
        return

    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    table = session.resource("dynamodb").Table(TABLE_NAME)

    print("  writing transitions to DDB…")
    t0 = time.time()
    written = 0
    last_log = t0
    with table.batch_writer() as batch:
        for tup in _iter_status_transitions(
            con, rides, ride_id_filter, limit
        ):
            ride_id, old_status, new_status, wait_mins, changed_at = tup
            meta = rides[ride_id]
            batch.put_item(Item=_build_hist_item(
                ride_id, meta["ride_name"], meta["park_name"],
                meta["park_key"], old_status, new_status,
                wait_mins, changed_at, ttl,
            ))
            written += 1
            now = time.time()
            if written % sample_every == 0 and now - last_log > 5:
                elapsed = now - t0
                rate = written / elapsed if elapsed else 0
                print(
                    f"    {written:,} transitions in {elapsed:.0f}s "
                    f"@ {rate:,.0f}/s"
                )
                last_log = now

    elapsed = time.time() - t0
    rate = written / elapsed if elapsed else 0
    print(
        f"\n  wrote {written:,} HIST# rows in {elapsed:.1f}s @ "
        f"{rate:,.0f}/s"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually write to DDB. Default is dry-run (sample only).",
    )
    parser.add_argument(
        "--mode", choices=("wait", "hist", "both"), default="wait",
        help=(
            "Which row type(s) to backfill. wait (default) = WAIT# "
            "observations (one per OPERATING poll); hist = HIST# "
            "status transitions; both = both. Default is wait so "
            "prior single-mode invocations keep their behavior — "
            "HIST# is opt-in."
        ),
    )
    parser.add_argument(
        "--ride-id", type=str, default=None,
        help="Filter to a single ride for testing.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help=(
            "Cap row count for testing. In wait mode caps source "
            "rows; in hist mode caps emitted transitions. Defaults "
            "to no limit."
        ),
    )
    parser.add_argument(
        "--sample-every", type=int, default=50_000,
        help=(
            "Progress log frequency. Applies to wait mode (rows) "
            "and hist mode (transitions). Default: 50,000."
        ),
    )
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"Snapshot not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    print(f"Pi snapshot: {DB_PATH} ({DB_PATH.stat().st_size / 1e9:.2f} GB)")
    print(f"Target table: {TABLE_NAME} (region {REGION}, profile {PROFILE})")
    if args.ride_id:
        print(f"Ride filter: {args.ride_id}")
    if args.limit:
        print(f"Row limit: {args.limit:,}")
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}  ({args.mode})")
    print()

    con = sqlite3.connect(str(DB_PATH))
    try:
        rides = _load_ride_meta(con)
        if args.mode in ("wait", "both"):
            _backfill(
                con,
                rides=rides,
                ride_id_filter=args.ride_id,
                limit=args.limit,
                execute=args.execute,
                sample_every=args.sample_every,
            )
        if args.mode in ("hist", "both"):
            _backfill_hist(
                con,
                rides=rides,
                ride_id_filter=args.ride_id,
                limit=args.limit,
                execute=args.execute,
                sample_every=args.sample_every,
            )
    except ClientError as e:
        print(f"\nDDB error: {e}", file=sys.stderr)
        sys.exit(2)
    finally:
        con.close()


if __name__ == "__main__":
    main()
