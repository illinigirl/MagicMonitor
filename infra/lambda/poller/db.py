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
# DynamoDB auto-clears it when the cooldown expires — eventually.

def _cooldown_active(resp: dict) -> bool:
    """True iff the get_item response holds a cooldown row that has NOT
    expired yet, comparing the stored ttl to now.

    DynamoDB's TTL reaper is best-effort — AWS documents that deletion can
    lag expiry by up to ~48h, and expired-but-undeleted items are still
    returned by GetItem. So presence alone is not "on cooldown": trusting
    it silently stretches a 15-min cooldown to however long the reaper
    lags, suppressing a legitimate alert for a second, distinct outage of
    the same ride later the same day. Compare ttl to now instead.
    """
    item = resp.get("Item")
    if not item:
        return False
    ttl = item.get("ttl")
    if ttl is None:
        return True  # legacy row without ttl — fall back to presence
    return int(ttl) > int(time.time())


def is_down_alert_on_cooldown(ride_id: str) -> bool:
    """Return True if a DOWN alert was sent for this ride recently."""
    resp = _table.get_item(Key={"PK": f"RIDE#{ride_id}", "SK": "COOLDOWN#DOWN"})
    return _cooldown_active(resp)


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
    return _cooldown_active(resp)


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
    return _cooldown_active(resp)


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


# ─── Still-down (second-alert) cooldown ─────────────────────────────
# Separate SK from the initial DOWN cooldown so the "still down after N
# minutes" alert doesn't collide with it. The cooldown window equals the
# second-alert interval, which is index.py config, so it's passed in.

def is_still_down_alert_on_cooldown(ride_id: str) -> bool:
    resp = _table.get_item(Key={"PK": f"RIDE#{ride_id}", "SK": "COOLDOWN#STILL_DOWN"})
    return _cooldown_active(resp)


def mark_still_down_alert_sent(ride_id: str, cooldown_secs: int) -> None:
    _table.put_item(
        Item={
            "PK":      f"RIDE#{ride_id}",
            "SK":      "COOLDOWN#STILL_DOWN",
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "ttl":     int(time.time()) + cooldown_secs,
        }
    )


# ─── Subscriptions ──────────────────────────────────────────────────
# PARK#<key> / USER#<id> rows let us fanout alerts efficiently — one
# Query per park returns every subscriber, no scan needed.

def get_park_subscribers(park_key: str) -> list[str]:
    """Return the user IDs subscribed to alerts for this park.

    Paginated: a park's subscriber rows could in principle exceed a single
    1MB Query page, and a partial subscriber list silently drops alerts —
    the project's signature data-growth failure class.
    """
    out: list[str] = []
    kwargs = {
        "KeyConditionExpression": Key("PK").eq(f"PARK#{park_key}") & Key("SK").begins_with("USER#"),
    }
    while True:
        resp = _table.query(**kwargs)
        out.extend(item["SK"].removeprefix("USER#") for item in resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return out
        kwargs["ExclusiveStartKey"] = lek


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
    out: set[str] = set()
    kwargs = {
        "KeyConditionExpression": Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with("FAV_RIDE#"),
        "FilterExpression": "park_key = :park",
        "ExpressionAttributeValues": {":park": park_key},
    }
    while True:
        resp = _table.query(**kwargs)
        out.update(item["SK"].removeprefix("FAV_RIDE#") for item in resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return out
        kwargs["ExclusiveStartKey"] = lek


def get_user_ll_watched_rides(user_id: str, park_key: str) -> set[str]:
    """Return the ride_ids in this park the user opted into LL-watching.

    A favorite is LL-watched only when its FAV_RIDE# row carries
    `ll_watch = true` (set via the /me favorites toggle or the MCP
    watch_ll tool). Absent/false → not watched, so the LL-improvement
    fanout skips it. Plan rides are watched independently of this
    (always-on for the active-plan party), so this covers ONLY the
    favorites-opt-in half.

    Paginated + FilterExpression on both park_key and ll_watch so a
    grown table can't silently drop the user's rows (the 2026-05-24
    class); same shape as get_user_favorites_for_park.
    """
    out: set[str] = set()
    kwargs = {
        "KeyConditionExpression": Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with("FAV_RIDE#"),
        "FilterExpression": "park_key = :park AND ll_watch = :on",
        "ExpressionAttributeValues": {":park": park_key, ":on": True},
    }
    while True:
        resp = _table.query(**kwargs)
        out.update(item["SK"].removeprefix("FAV_RIDE#") for item in resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return out
        kwargs["ExclusiveStartKey"] = lek


# ─── Active plan index (M9 bridge: plan-aware DOWN/UP alerts) ───────
# USER#<id> / PLAN#<iso_ts> rows are written by the MCP record_plan
# tool when a user accepts a plan. The poller needs to know which
# plans are active TODAY so it can ping users when a ride in their
# plan transitions DOWN or BACK UP.
#
# Implementation note: this Queries the sparse `planned_for_date-index`
# GSI (see disney-stack.ts) — one small partition (today's date), not a
# table scan. It used to be a full-table Scan + FilterExpression; that
# walked the whole table (~632MB / ~3M WAIT# rows by 2026-06-03) to find
# a handful of PLAN# rows and hit its 50-page cap at ~8% coverage, so an
# activated plan's row likely sat beyond the cap and never fired alerts.
# The GSI is sparse (planned_for_date lives only on PLAN# rows), so the
# Query returns just today's plans regardless of how large WAIT# grows.
# active / outcome_recorded / window filtering is applied below (and in
# Python too, so the stub-table tests cover it).

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
    """Query today's active (outcome_recorded=false) plans via the sparse
    planned_for_date GSI and return two derived views from a single query:

      ride_index: {ride_identifier: [(user_id, plan_id), ...]}
        Used by the per-ride DOWN/UP plan-aware fanout. `ride_identifier`
        is the ride_id when ride_sequence entries carry one, falling
        back to the lowercased ride_name otherwise — the handler's
        lookup tries both forms so plans recorded before the ride_id
        field landed still match.

      active_plans: list of {user_id, plan_id, park_key, park_name}
        Used by the plan-weather-shift fanout, which is plan-scoped
        rather than ride-scoped. We need park info to compose the
        alert body — extracted from the same query so we don't pay for
        a second DDB pass.

    Called once per poll invocation, cached in the local handler
    scope. Returns ({}, []) on failure rather than raising — plan
    alerts are bonus, not load-bearing for the M1 alert path.
    """
    # Query the sparse planned_for_date GSI — one partition (today),
    # not a table scan. Server-side FilterExpression narrows to active +
    # not-yet-recorded; the same gates are re-applied in Python below so
    # the stub-table tests (which don't parse FilterExpression) cover
    # them. Paginate defensively, though a single date partition holds
    # only a handful of plans.
    index: dict = {}
    active_plans: list[dict] = []
    last_evaluated_key = None
    page_count = 0
    while True:
        kwargs = {
            "IndexName": "planned_for_date-index",
            "KeyConditionExpression": "planned_for_date = :d",
            "FilterExpression": (
                # Missing outcome_recorded counts as not-recorded (DDB
                # equality is false on a missing attribute, which would
                # WRONGLY exclude a legacy row the Python re-guard below
                # includes via item.get(...)). Match the two so the filter
                # and the re-guard agree.
                "(attribute_not_exists(outcome_recorded) OR outcome_recorded = :false) "
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
            },
        }
        if last_evaluated_key:
            kwargs["ExclusiveStartKey"] = last_evaluated_key
        try:
            resp = _table.query(**kwargs)
        except Exception as e:
            print(f"[poller] build_active_plan_ride_index query failed (page {page_count}): {e}")
            # Whatever we built so far is better than nothing. This also
            # covers the brief GSI-backfill window right after the index
            # is first created (query 4xx until it goes ACTIVE).
            return index, active_plans
        page_count += 1
        for item in resp.get("Items", []):
            user_id = item.get("PK", "").removeprefix("USER#")
            plan_id = item.get("SK", "").removeprefix("PLAN#")
            if not user_id or not plan_id:
                continue
            # Defensive guards re-applied in Python (the stub query()
            # doesn't parse Key/Filter expressions): the GSI is sparse to
            # PLAN# rows, but enforce the row shape + date + gates here too.
            if not item.get("SK", "").startswith("PLAN#"):
                continue
            if item.get("planned_for_date") != today_date_iso:
                continue
            if item.get("outcome_recorded"):
                continue
            # Activation + window gates (M5). active=False → dormant, no
            # alerts until activated. Outside the plan's window → skip
            # (fail-open if no/unparseable window). Missing `active`
            # (legacy rows) counts as active.
            if item.get("active") is False:
                continue
            if now_et is not None and not _plan_window_contains(
                item.get("plan_window"), now_et
            ):
                continue
            # Alert recipients (2026-07-03): the partition owner is always
            # implicit, plus any opted-in family members from the row's
            # alert_subscribers String Set (ids with USER#<id>/PROFILE
            # rows — see set_plan_alert_subscription in the MCP). Absent
            # attribute = owner-only, the pre-feature behavior. Each
            # recipient gets their own index/active_plans entries; the
            # weather path's per-user dedup + per-(user, plan) cooldowns
            # already handle the rest.
            subscribers = item.get("alert_subscribers") or set()
            recipients = [user_id] + sorted(
                s for s in subscribers if s and s != user_id
            )
            # Rides dropped OR marked done via /replan leave the watch set
            # (atomic dropped_ride_ids / completed_ride_ids — never mutate
            # ride_sequence, so the MCP planner's view is intact).
            dropped = (item.get("dropped_ride_ids") or set()) | (
                item.get("completed_ride_ids") or set()
            )
            # Remaining planned rides with their at-plan-time predictions,
            # for the plan-drift check (current waits vs what the plan
            # assumed). Only rides carrying a numeric predicted_wait_min
            # are comparable; others are skipped by the drift math.
            plan_rides = [
                {
                    "ride_id": r.get("ride_id"),
                    "ride_name": r.get("ride_name"),
                    "predicted_wait_min": r.get("predicted_wait_min"),
                }
                for r in (item.get("ride_sequence") or [])
                if r.get("ride_id") and r.get("ride_id") not in dropped
            ]
            # Held Lightning Lanes for this plan (ride_id → return ISO),
            # set via the MCP set_held_ll tool. Used by the LL-earlier
            # precision check (only alert when a slot beats what you hold).
            held_ll = item.get("ll_holds") or {}
            for recipient in recipients:
                active_plans.append({
                    "user_id":   recipient,
                    "plan_id":   plan_id,
                    "park_key":  item.get("park_key"),
                    # Same rides/holds for every recipient of a plan; the
                    # drift + LL checks dedupe by plan_id before using them.
                    "rides":     plan_rides,
                    "ll_holds":  dict(held_ll),
                })
            for ride in item.get("ride_sequence", []) or []:
                ride_id = ride.get("ride_id")
                if ride_id and ride_id in dropped:
                    continue
                ride_name = ride.get("ride_name")
                for key in filter(None, (ride_id, (ride_name or "").lower())):
                    for recipient in recipients:
                        index.setdefault(key, []).append((recipient, plan_id))
        last_evaluated_key = resp.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
        # Belt-and-braces cap. We Query one date partition now (sparse
        # index → at most a handful of plans for a single day), so this
        # should never trigger — it's pure defense-in-depth against an
        # unexpected explosion of same-day plans. A cap hit here would be
        # a real anomaly worth investigating, unlike the old 50-page Scan
        # cap that data growth made routine.
        if page_count >= 10:
            print(f"[poller] build_active_plan_ride_index hit page cap (10) — unexpected for a single-day query, stopping early")
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
    return _cooldown_active(resp)


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


# Plan-drift ("day running lighter/heavier than planned") cooldown —
# per (user, plan). Longer window than a ride event: it's a gentle
# once-in-a-while nudge, not a reactive ping. Default 3h.
PLAN_DRIFT_COOLDOWN_SECS = int(os.environ.get("PLAN_DRIFT_COOLDOWN_SECS", "10800"))


def is_plan_drift_on_cooldown(user_id: str, plan_id: str) -> bool:
    resp = _table.get_item(
        Key={"PK": f"USER#{user_id}", "SK": f"COOLDOWN#DRIFT#{plan_id}"}
    )
    return _cooldown_active(resp)


def mark_plan_drift_sent(user_id: str, plan_id: str) -> None:
    expire_ts = int(time.time()) + PLAN_DRIFT_COOLDOWN_SECS
    _table.put_item(
        Item={
            "PK":      f"USER#{user_id}",
            "SK":      f"COOLDOWN#DRIFT#{plan_id}",
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "ttl":     expire_ts,
        }
    )
