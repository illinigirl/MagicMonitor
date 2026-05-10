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
# Short-wait alerts get a longer cooldown — the low-wait window for a
# ride often persists 30-60 min, and we don't want to spam-ping during
# the same trough. 90 min default; configurable via env.
LOW_WAIT_ALERT_COOLDOWN_SECS = int(os.environ.get("LOW_WAIT_ALERT_COOLDOWN_SECS", "5400"))
# Forecast snapshots aren't useful past ~1 day for accuracy work but
# 7 days lets us spot weekly recurrence in forecast-vs-actual analysis
# without bloating the table. Tune via env if Phase C wants longer.
FORECAST_RETENTION_DAYS = int(os.environ.get("FORECAST_RETENTION_DAYS", "7"))

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
