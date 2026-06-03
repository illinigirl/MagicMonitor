"""
DynamoDB single-table access layer.

Replaces the SQLite db.py from the Pi version. Schema documented in
disney-stack.ts; in summary:

    PK / SK
    RIDE#<id>     / STATE              — current ride state
    RIDE#<id>     / HIST#<iso_ts>      — change history (TTL)
    RIDE#<id>     / DOWN_SINCE         — track down duration
    RIDE#<id>     / COOLDOWN#DOWN      — alert dedup (TTL)
    USER#<id>     / PROFILE            — name, pushover_user_key
    PARK#<key>    / USER#<id>          — subscription (fanout)
"""

import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key

TABLE_NAME = os.environ["DISNEY_TABLE_NAME"]
HISTORY_RETENTION_DAYS = int(os.environ.get("HISTORY_RETENTION_DAYS", "90"))
DOWN_ALERT_COOLDOWN_SECS = int(os.environ.get("DOWN_ALERT_COOLDOWN_SECS", "900"))
# BACK UP alerts also need a cooldown — themeparks.wiki occasionally
# flaps a ride OPERATING→DOWN→OPERATING→DOWN→OPERATING within minutes
# during glitchy reporting periods. Without this, every UP transition
# fires a fresh BACK UP push (since DOWN_SINCE is set on every DOWN
# regardless of whether the DOWN cooldown suppressed the DOWN alert).
# 15 min matches the DOWN cooldown so flap pings dedup symmetrically.
BACK_UP_ALERT_COOLDOWN_SECS = int(os.environ.get("BACK_UP_ALERT_COOLDOWN_SECS", "900"))
# Short-wait alerts get a longer cooldown — the low-wait window for a
# ride often persists 30-60 min, and we don't want to spam-ping during
# the same trough. 90 min default; configurable via env.
LOW_WAIT_ALERT_COOLDOWN_SECS = int(os.environ.get("LOW_WAIT_ALERT_COOLDOWN_SECS", "5400"))
# Forecast snapshots aren't useful past ~1 day for accuracy work but
# 7 days lets us spot weekly recurrence in forecast-vs-actual analysis
# without bloating the table. Tune via env if Phase C wants longer.
FORECAST_RETENTION_DAYS = int(os.environ.get("FORECAST_RETENTION_DAYS", "7"))
# Raw per-poll wait observations (M6-B Phase 1). Mirrors the Pi's
# collection pattern in DDB so analytics can eventually source from
# AWS instead of the Pi snapshot. 1-year default retention bounds
# storage cost (~5 GB at 1 year of accumulated writes) while giving
# the aggregator a full seasonal window. Tune via env.
WAIT_OBSERVATION_RETENTION_DAYS = int(os.environ.get("WAIT_OBSERVATION_RETENTION_DAYS", "365"))
# Plan-weather-shift cooldown — one alert per (user, plan) per hour
# default. Storm forecasts persist for a few hours once they appear,
# so 60 min prevents a single weather event from generating multiple
# pings while still letting a second, distinct storm window later in
# the day re-alert.
WEATHER_SHIFT_COOLDOWN_SECS = int(os.environ.get("WEATHER_SHIFT_COOLDOWN_SECS", "3600"))
# Weather snapshots auto-expire after 2 days. The poller only needs
# "what did we last see" — anything older than yesterday is just
# debug-trail cruft.
WEATHER_SNAPSHOT_TTL_SECS = int(os.environ.get("WEATHER_SNAPSHOT_TTL_SECS", "172800"))

# Module-level resource — reused across warm invocations to avoid
# reconnecting on every poll. Lambda freezes/thaws this between calls.
_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(TABLE_NAME)


# ─── Ride state ─────────────────────────────────────────────────────

def get_ride(ride_id: str) -> Optional[dict]:
    """Fetch the current STATE row for a ride, or None if never seen."""
    resp = _table.get_item(Key={"PK": f"RIDE#{ride_id}", "SK": "STATE"})
    return resp.get("Item")


def upsert_ride(attraction: dict) -> None:
    """Write/update the current STATE row for a ride.

    Uses PutItem (full overwrite) — the attraction dict is the source
    of truth on every poll. Attributes preserved across writes (like
    DOWN_SINCE) live in their own SK rows.

    `last_forecast_at` is set when the upstream /live response includes
    a forecast for this ride and left unset (== removed on overwrite)
    otherwise. That lets Phase C derive forecast-availability signals
    ("Space Mountain hasn't had a forecast in 4 hours") without
    storing 5K+ empty rows/day. The full forecast snapshots live in
    FORECAST# sub-rows written by record_forecast.
    """
    item = {
        "PK":         f"RIDE#{attraction['id']}",
        "SK":         "STATE",
        "ride_id":    attraction["id"],
        "park_key":   attraction["park_key"],
        "park_id":    attraction["park_id"],
        "park_name":  attraction["park_name"],
        "name":       attraction["name"],
        "status":     attraction["status"],
        "wait_mins":  attraction["wait_mins"],
        "ll":         attraction.get("ll"),
        "ll_state":   attraction.get("ll_state"),
        "last_seen":  attraction["last_seen"],
    }
    if attraction.get("forecast"):
        item["last_forecast_at"] = attraction["last_seen"]
    _table.put_item(Item=item)


def record_status_change(
    ride_id: str,
    ride_name: str,
    park_name: str,
    park_key: str,
    old_status: Optional[str],
    new_status: str,
    wait_mins: Optional[int],
    changed_at: str,
) -> None:
    """Append a HIST row recording this status transition. Auto-expires
    after HISTORY_RETENTION_DAYS via DynamoDB TTL."""
    expire_ts = int(time.time()) + (HISTORY_RETENTION_DAYS * 86400)
    _table.put_item(
        Item={
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
            "ttl":        expire_ts,
        }
    )


# ─── Forecast snapshots ─────────────────────────────────────────────
# Append-only per-poll snapshots of the upstream forecast array. One
# row per (ride, poll), TTL'd after FORECAST_RETENTION_DAYS. Sets up
# Phase C (forecast-vs-actual accuracy) once a few days of data
# accumulate. No-op when forecast is None/empty — see upsert_ride
# for the cheap forecast-presence signal we keep on STATE rows
# instead of writing empty FORECAST rows for rides that never have one.

# ─── Raw wait observations (M6-B Phase 1) ──────────────────────────
# One row per (operating ride, poll). Mirrors the Pi's SQLite
# collection pattern in DDB so the analytics aggregator script can
# eventually source from AWS instead of (or in addition to) the
# Pi snapshot. Append-only, TTL'd after WAIT_OBSERVATION_RETENTION_DAYS.
#
# Design decisions (captured here so the rationale outlives the commit):
#  - Per-poll raw observations, not pre-aggregated hourly buckets. The
#    Pi pattern is raw; mirroring it keeps `tools/aggregate-analytics.py`
#    almost-unchanged when the source switch happens (same shape from
#    DDB as from SQLite). Cost difference is ~$2.50/mo vs ~$0.10/mo —
#    well within the project's <$5/mo budget. Raw also preserves
#    resolution for any future per-poll analysis that bucketing loses.
#  - Skip DOWN rides + rides with wait_mins=None. The aggregator only
#    cares about operating-ride wait observations; storing nulls would
#    inflate the table without analytic value.
#  - Per-call try/except in the caller so a write failure can't break
#    the alert path. Same defensive pattern as record_forecast.

def record_wait_observation(
    ride_id: str,
    park_key: str,
    wait_mins: int,
    polled_at: str,
) -> None:
    """Persist one operating-ride wait observation.

    Schema:
        PK = RIDE#<id>, SK = WAIT#<polled_at iso>
        wait_mins, park_key, polled_at, ttl

    Caller is responsible for filtering: only call for operating
    rides with a non-null wait_mins. The helper doesn't re-validate
    to keep the hot path lean.
    """
    expire_ts = int(time.time()) + (WAIT_OBSERVATION_RETENTION_DAYS * 86400)
    _table.put_item(
        Item={
            "PK":         f"RIDE#{ride_id}",
            "SK":         f"WAIT#{polled_at}",
            "wait_mins":  wait_mins,
            "park_key":   park_key,
            "polled_at":  polled_at,
            "ttl":        expire_ts,
        }
    )


def record_forecast(ride_id: str, polled_at: str, forecast: list[dict]) -> None:
    """Persist one poll's forecast for a ride.

    `polled_at` is the ISO-8601 UTC timestamp from the poll (matches
    the attraction's last_seen). `forecast` is the normalized list
    from wait_times._normalize_forecast — must be non-empty; callers
    should skip the call entirely when no forecast was returned.
    """
    expire_ts = int(time.time()) + (FORECAST_RETENTION_DAYS * 86400)
    _table.put_item(
        Item={
            "PK":        f"RIDE#{ride_id}",
            "SK":        f"FORECAST#{polled_at}",
            "polled_at": polled_at,
            "forecast":  forecast,
            "ttl":       expire_ts,
        }
    )


# ─── Down-since tracking ────────────────────────────────────────────
# Replaces the in-memory _down_since dict from monitor.py. Persisting
# to DynamoDB means a Lambda restart doesn't lose track of which rides
# are currently down (important — Lambda recycles unpredictably).

def set_down_since(ride_id: str, when: datetime) -> None:
    _table.put_item(
        Item={
            "PK":         f"RIDE#{ride_id}",
            "SK":         "DOWN_SINCE",
            "down_since": when.isoformat(),
        }
    )


def get_down_since(ride_id: str) -> Optional[datetime]:
    resp = _table.get_item(Key={"PK": f"RIDE#{ride_id}", "SK": "DOWN_SINCE"})
    item = resp.get("Item")
    if not item:
        return None
    return datetime.fromisoformat(item["down_since"])


def clear_down_since(ride_id: str) -> None:
    _table.delete_item(Key={"PK": f"RIDE#{ride_id}", "SK": "DOWN_SINCE"})


# ─── Alert cooldown ─────────────────────────────────────────────────
# Replaces the in-memory _down_alerted_at dict. TTL on the item means
# DynamoDB auto-clears it when the cooldown expires — no cleanup code.

def is_down_alert_on_cooldown(ride_id: str) -> bool:
    """Return True if a DOWN alert was sent for this ride recently."""
    resp = _table.get_item(Key={"PK": f"RIDE#{ride_id}", "SK": "COOLDOWN#DOWN"})
    return "Item" in resp


def mark_down_alert_sent(ride_id: str) -> None:
    """Record that a DOWN alert was sent. Item auto-expires after
    DOWN_ALERT_COOLDOWN_SECS via DynamoDB TTL."""
    expire_ts = int(time.time()) + DOWN_ALERT_COOLDOWN_SECS
    _table.put_item(
        Item={
            "PK":      f"RIDE#{ride_id}",
            "SK":      "COOLDOWN#DOWN",
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "ttl":     expire_ts,
        }
    )


# ─── BACK UP cooldown — mirrors DOWN cooldown ────────────────────────
# Prevents flap-induced spam where themeparks.wiki reports a ride
# OPERATING→DOWN→OPERATING repeatedly within minutes. Every BACK UP
# transition produces an alert without this gate; one OK-the-ride-is-
# fixed notification per ride per cooldown window is the right
# semantics.

def is_back_up_alert_on_cooldown(ride_id: str) -> bool:
    """Return True if a BACK UP alert was sent for this ride recently."""
    resp = _table.get_item(Key={"PK": f"RIDE#{ride_id}", "SK": "COOLDOWN#BACK_UP"})
    return "Item" in resp


def mark_back_up_alert_sent(ride_id: str) -> None:
    """Record that a BACK UP alert was sent. Item auto-expires after
    BACK_UP_ALERT_COOLDOWN_SECS via DynamoDB TTL."""
    expire_ts = int(time.time()) + BACK_UP_ALERT_COOLDOWN_SECS
    _table.put_item(
        Item={
            "PK":      f"RIDE#{ride_id}",
            "SK":      "COOLDOWN#BACK_UP",
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "ttl":     expire_ts,
        }
    )


# ─── Low-wait cooldown ─────────────────────────────────────────────
# Mirrors the DOWN cooldown pattern — same shape, different SK and a
# longer default TTL because the low-wait window itself usually lasts
# 30-60 min and we don't want to re-alert during the same trough.

def is_low_wait_alert_on_cooldown(ride_id: str) -> bool:
    resp = _table.get_item(Key={"PK": f"RIDE#{ride_id}", "SK": "COOLDOWN#LOW_WAIT"})
    return "Item" in resp


def mark_low_wait_alert_sent(ride_id: str) -> None:
    expire_ts = int(time.time()) + LOW_WAIT_ALERT_COOLDOWN_SECS
    _table.put_item(
        Item={
            "PK":      f"RIDE#{ride_id}",
            "SK":      "COOLDOWN#LOW_WAIT",
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "ttl":     expire_ts,
        }
    )


# ─── Subscriptions ──────────────────────────────────────────────────
# PARK#<key> / USER#<id> rows let us fanout alerts efficiently — one
# Query per park returns every subscriber, no scan needed.

def get_park_subscribers(park_key: str) -> list[str]:
    """Return the user IDs subscribed to alerts for this park."""
    resp = _table.query(
        KeyConditionExpression=Key("PK").eq(f"PARK#{park_key}") & Key("SK").begins_with("USER#"),
    )
    return [item["SK"].removeprefix("USER#") for item in resp.get("Items", [])]


def get_user_profile(user_id: str) -> Optional[dict]:
    """Fetch a user's profile (name + pushover_user_key)."""
    resp = _table.get_item(Key={"PK": f"USER#{user_id}", "SK": "PROFILE"})
    return resp.get("Item")


# ─── Favorite rides (M3 Phase 2) ────────────────────────────────────
# USER#<id> / FAV_RIDE#<ride_id> rows with denormalized park_key let
# the poller answer "which of this user's favorites are in this park?"
# with a single Query + FilterExpression. The denormalized park_key
# makes the filter cheap (no second lookup against RIDE# state).
#
# Access pattern is per-user, per-park, once per invocation — cached
# in index.py so we don't re-query within a poll.

def get_user_favorites_for_park(user_id: str, park_key: str) -> set[str]:
    """Return the set of ride_ids the user has favorited in this park.

    Empty set means: user has no favorites in this park, so M3 Phase 2's
    fanout filter will skip them entirely. That's the intended behavior
    (matches Phase 3's "default zero rides → zero alerts" spec).
    """
    resp = _table.query(
        KeyConditionExpression=Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with("FAV_RIDE#"),
        FilterExpression="park_key = :park",
        ExpressionAttributeValues={":park": park_key},
    )
    return {item["SK"].removeprefix("FAV_RIDE#") for item in resp.get("Items", [])}


# ─── Active plan index (M9 bridge: plan-aware DOWN/UP alerts) ───────
# USER#<id> / PLAN#<iso_ts> rows are written by the MCP record_plan
# tool when a user accepts a plan. The poller needs to know which
# plans are active TODAY so it can ping users when a ride in their
# plan transitions DOWN or BACK UP.
#
# Implementation note: this is a scan with FilterExpression. At
# single-digit-user scale, the DisneyData table is small enough that
# a full scan reads only a few hundred items (~125 RCU, well under
# free tier). Cost: ~$0.025/day at current poll cadence. If the user
# count grows past ~100, this should move to a GSI on
# `(planned_for_date, outcome_recorded)` so we don't scan inactive
# plans + historical USER#* / RIDE#* partitions to find a few rows.

def _plan_window_contains(plan_window, now_et) -> bool:
    """True if `now_et` is inside the plan's [open, close] window.

    Fail-open: returns True when there's no usable window (missing field,
    unparseable times) — a slightly-early disruption alert is far better
    than silently suppressing one. `plan_window` open/close are concrete
    ISO datetimes resolved at activation (from get_planning_context's park
    hours), normally tz-aware, comparing cleanly against the tz-aware
    now_et.
    """
    if not isinstance(plan_window, dict):
        return True
    open_s, close_s = plan_window.get("open"), plan_window.get("close")
    if not open_s or not close_s:
        return True
    try:
        open_dt = datetime.fromisoformat(open_s)
        close_dt = datetime.fromisoformat(close_s)
    except (ValueError, TypeError):
        return True
    cmp = now_et
    # Reconcile naive/aware so the comparison can't raise.
    if open_dt.tzinfo is None and now_et.tzinfo is not None:
        cmp = now_et.replace(tzinfo=None)
    elif open_dt.tzinfo is not None and now_et.tzinfo is None:
        return True  # can't compare reliably → fail open
    try:
        return open_dt <= cmp <= close_dt
    except TypeError:
        return True


def build_active_plan_ride_index(
    today_date_iso: str, now_et=None
) -> tuple[dict, list[dict]]:
    """Scan today's active (outcome_recorded=false) plans and return two
    derived views from a single scan:

      ride_index: {ride_identifier: [(user_id, plan_id), ...]}
        Used by the per-ride DOWN/UP plan-aware fanout. `ride_identifier`
        is the ride_id when ride_sequence entries carry one, falling
        back to the lowercased ride_name otherwise — the handler's
        lookup tries both forms so plans recorded before the ride_id
        field landed still match.

      active_plans: list of {user_id, plan_id, park_key, park_name}
        Used by the plan-weather-shift fanout, which is plan-scoped
        rather than ride-scoped. We need park info to compose the
        alert body — extracted from the same scan so we don't pay for
        a second DDB pass.

    Called once per poll invocation, cached in the local handler
    scope. Returns ({}, []) on failure rather than raising — plan
    alerts are bonus, not load-bearing for the M1 alert path.
    """
    # Paginate the scan — DDB returns 1MB pages max, and on a table
    # with many RIDE# rows the PLAN# rows may not land in the first
    # page (DDB scans don't guarantee any partition ordering). Without
    # pagination this silently returns zero matches.
    index: dict = {}
    active_plans: list[dict] = []
    last_evaluated_key = None
    page_count = 0
    while True:
        kwargs = {
            "FilterExpression": (
                "planned_for_date = :d AND outcome_recorded = :false "
                "AND begins_with(SK, :plan_prefix) AND begins_with(PK, :user_prefix) "
                # Activation gate (M5): a future trip day is written DORMANT
                # (active=false) and fires no alerts until activate_plan flips
                # it on its day. Legacy rows predate the field, so missing
                # `active` counts as active (back-compat). `#active` aliased
                # to dodge any reserved-word risk.
                "AND (attribute_not_exists(#active) OR #active = :true)"
            ),
            "ExpressionAttributeNames": {"#active": "active"},
            "ExpressionAttributeValues": {
                ":d": today_date_iso,
                ":false": False,
                ":true": True,
                ":plan_prefix": "PLAN#",
                ":user_prefix": "USER#",
            },
        }
        if last_evaluated_key:
            kwargs["ExclusiveStartKey"] = last_evaluated_key
        try:
            resp = _table.scan(**kwargs)
        except Exception as e:
            print(f"[poller] build_active_plan_ride_index scan failed (page {page_count}): {e}")
            # Whatever we built so far is better than nothing.
            return index, active_plans
        page_count += 1
        for item in resp.get("Items", []):
            user_id = item.get("PK", "").removeprefix("USER#")
            plan_id = item.get("SK", "").removeprefix("PLAN#")
            if not user_id or not plan_id:
                continue
            # Activation + window gates (M5), enforced in Python too so the
            # stub-table tests (which don't parse FilterExpression) cover
            # them. active=False → dormant, no alerts until activated.
            # Outside the plan's window → skip (fail-open if no/unparseable
            # window). Missing `active` (legacy rows) counts as active.
            if item.get("active") is False:
                continue
            if now_et is not None and not _plan_window_contains(
                item.get("plan_window"), now_et
            ):
                continue
            active_plans.append({
                "user_id":   user_id,
                "plan_id":   plan_id,
                "park_key":  item.get("park_key"),
                # park_name isn't stored on the plan row, but it's
                # derivable from park_key in the handler via the same
                # PARK_NAME lookup the notifier uses. Leaving the slot
                # here for clarity.
            })
            for ride in item.get("ride_sequence", []) or []:
                ride_id = ride.get("ride_id")
                ride_name = ride.get("ride_name")
                for key in filter(None, (ride_id, (ride_name or "").lower())):
                    index.setdefault(key, []).append((user_id, plan_id))
        last_evaluated_key = resp.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
        # Belt-and-braces guard against runaway pagination if the table
        # grows unexpectedly. At ~2K rows + ~1MB pages we expect 2-3
        # pages today; 50 pages = ~50MB of scan, well beyond any
        # realistic state of the table.
        if page_count >= 50:
            print(f"[poller] build_active_plan_ride_index hit page cap (50), stopping early")
            break
    return index, active_plans


def lookup_plan_targets(
    index: dict, ride_id: str, ride_name: str
) -> list:
    """Resolve plan-alert targets for a given ride using either
    identifier (the MCP tool prefers ride_id but older plans may only
    carry ride_name).

    Returns deduped list of (user_id, plan_id). Same (user, plan)
    pair appearing under both keys collapses to one entry.
    """
    seen = set()
    out = []
    for key in filter(None, (ride_id, (ride_name or "").lower())):
        for entry in index.get(key, []):
            if entry in seen:
                continue
            seen.add(entry)
            out.append(entry)
    return out


# ─── Weather snapshot (plan-weather-shift alert path) ───────────────
# One row per WDW (single lat/lon serves all four parks). The poller
# reads the prior on each invocation, compares to the freshly-fetched
# forecast, and writes the new snapshot back. Compared payload is
# pruned to just the keys the shift detector consumes — full Open-
# Meteo response would bloat the row for no operational gain.
#
# Row: WEATHER#WDW / SNAPSHOT, TTL = WEATHER_SNAPSHOT_TTL_SECS.

_WEATHER_PK = "WEATHER#WDW"
_WEATHER_SK = "SNAPSHOT"


def get_prior_weather_snapshot() -> Optional[dict]:
    """Return the last persisted weather snapshot, or None if absent.

    Treat None as "first invocation after a deploy / TTL expired" —
    the shift detector handles that case as "no prior, treat any
    storm in the new forecast as new." A spurious post-deploy alert
    is contained by the per-plan cooldown.
    """
    try:
        resp = _table.get_item(Key={"PK": _WEATHER_PK, "SK": _WEATHER_SK})
    except Exception as e:
        print(f"[poller] get_prior_weather_snapshot failed: {e}")
        return None
    return resp.get("Item", {}).get("payload")


def put_weather_snapshot(snapshot: dict) -> None:
    """Persist the latest fetched forecast for next poll's comparison.

    Wrapped in a defensive try — a weather-write failure should never
    abort the rest of the alert pipeline. Worst case is we lose the
    snapshot for one poll and re-trigger the same shift in 2 min,
    which the cooldown will suppress.
    """
    expire_ts = int(time.time()) + WEATHER_SNAPSHOT_TTL_SECS
    try:
        _table.put_item(
            Item={
                "PK":      _WEATHER_PK,
                "SK":      _WEATHER_SK,
                "payload": snapshot,
                "ttl":     expire_ts,
            }
        )
    except Exception as e:
        print(f"[poller] put_weather_snapshot failed: {e}")


# ─── Plan-weather cooldown (per-user, per-plan) ─────────────────────
# Mirrors the existing per-ride cooldown pattern but keyed on the plan
# instead of the ride — a weather shift is a plan-scoped event, not a
# ride-scoped one. Without this, a storm sitting in the 6-hour window
# for an hour would generate ~30 pings.

def is_weather_alert_on_cooldown(user_id: str, plan_id: str) -> bool:
    resp = _table.get_item(
        Key={
            "PK": f"USER#{user_id}",
            "SK": f"COOLDOWN#WEATHER#{plan_id}",
        }
    )
    return "Item" in resp


def mark_weather_alert_sent(user_id: str, plan_id: str) -> None:
    expire_ts = int(time.time()) + WEATHER_SHIFT_COOLDOWN_SECS
    _table.put_item(
        Item={
            "PK":      f"USER#{user_id}",
            "SK":      f"COOLDOWN#WEATHER#{plan_id}",
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "ttl":     expire_ts,
        }
    )
