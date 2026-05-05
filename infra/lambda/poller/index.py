"""
Disney poller Lambda — entrypoint.

Triggered by EventBridge every 2 minutes. For each park:
  1. Fetch live wait/status data from themeparks.wiki
  2. Diff each attraction against the stored STATE row in DynamoDB
  3. Persist the new state and append to history if status changed
  4. For each status transition that warrants an alert, fan out
     Pushover messages to every subscriber of that park

Multi-user-ready from day 1: subscribers are read from DynamoDB
PARK#<key> / USER#<id> rows, and each user's Pushover user_key comes
from their USER#<id> / PROFILE row. M2 will add a UI to manage these
rows; M1 ships with rows seeded manually (see README's seed step).
"""

import os
import time
from datetime import datetime, timezone, timedelta

import wait_times
import db
import notifier

PARK_KEYS = os.environ["PARK_KEYS"].split(",")
SECOND_ALERT_MINS = int(os.environ.get("SECOND_ALERT_MINS", "45"))

# Suppress alerts within this many minutes of park close — rides
# routinely shut down for the night around then and we don't want
# the closing-time wave of "X is DOWN" pings.
CLOSING_BUFFER_MINS = int(os.environ.get("CLOSING_BUFFER_MINS", "30"))


def handler(event, context):
    """EventBridge invokes this with no meaningful payload — the schedule
    *is* the trigger. We process every park on every invocation."""
    started = time.time()
    print(f"[poller] Starting poll of {len(PARK_KEYS)} parks: {PARK_KEYS}")

    # Cache subscribers per park so we don't re-query DynamoDB for
    # every ride event.
    subscribers_by_park: dict[str, list[str]] = {}
    profile_cache: dict[str, dict] = {}

    # Cache park hours per park (one schedule call per invocation).
    # Returns True if alerts should fire for this park right now,
    # False during closed hours / closing buffer / when schedule
    # fetch fails (fail-open: see comment in _alerts_allowed).
    alerts_allowed_cache: dict[str, bool] = {}

    def alerts_allowed(park_key: str) -> bool:
        if park_key in alerts_allowed_cache:
            return alerts_allowed_cache[park_key]
        hours = wait_times.fetch_park_hours(park_key)
        if hours is None:
            # Fail-open: if the schedule API is broken or returns
            # nothing, prefer to alert (data we miss is worse than
            # noise). This matches the Pi version's implicit behavior.
            print(f"[poller] No schedule for {park_key} — defaulting to alerts ON")
            alerts_allowed_cache[park_key] = True
            return True
        open_dt, close_dt = hours
        now_eastern = datetime.now(open_dt.tzinfo)
        cutoff = close_dt - timedelta(minutes=CLOSING_BUFFER_MINS)
        allowed = open_dt <= now_eastern <= cutoff
        if not allowed:
            print(
                f"[poller] {park_key}: alerts SUPPRESSED — "
                f"now={now_eastern.strftime('%H:%M')}, "
                f"open={open_dt.strftime('%H:%M')}, "
                f"close-buffer={cutoff.strftime('%H:%M')}"
            )
        alerts_allowed_cache[park_key] = allowed
        return allowed

    def get_subscribers(park_key: str) -> list[str]:
        if park_key not in subscribers_by_park:
            subscribers_by_park[park_key] = db.get_park_subscribers(park_key)
        return subscribers_by_park[park_key]

    def get_user_key(user_id: str) -> str | None:
        if user_id not in profile_cache:
            profile = db.get_user_profile(user_id) or {}
            profile_cache[user_id] = profile
        return profile_cache.get(user_id, {}).get("pushover_user_key")

    # Track rides currently down across all parks for the post-loop
    # "second alert" sweep (rides down >= SECOND_ALERT_MINS get a
    # follow-up notification).
    currently_down: list[tuple[str, dict]] = []  # (ride_id, attraction)

    total_changes = 0
    total_alerts = 0

    for park_key in PARK_KEYS:
        try:
            attractions = wait_times.fetch_live_data(park_key)
        except Exception as e:
            print(f"[poller] ERROR fetching {park_key}: {e}")
            continue

        print(f"[poller] {park_key}: {len(attractions)} attractions")
        subscribers = get_subscribers(park_key)
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()

        for attr in attractions:
            ride_id = attr["id"]
            new_status = attr["status"]
            new_wait = attr["wait_mins"]

            existing = db.get_ride(ride_id)
            old_status = existing["status"] if existing else None

            db.upsert_ride(attr)

            # First time we've seen this ride — record state, no alert.
            if old_status is None:
                continue

            # Track down state regardless of alert fanout, so the
            # "back up" duration and "still down" sweep work even if
            # nobody was subscribed at the time it went down.
            if new_status == "DOWN":
                currently_down.append((ride_id, attr))

            # No status change → nothing more to do for this ride.
            if new_status == old_status:
                continue

            total_changes += 1
            db.record_status_change(
                ride_id=ride_id,
                ride_name=attr["name"],
                park_name=attr["park_name"],
                park_key=park_key,
                old_status=old_status,
                new_status=new_status,
                wait_mins=new_wait,
                changed_at=now_iso,
            )

            # ── DOWN: just went out of service ─────────────────────
            if new_status == "DOWN":
                db.set_down_since(ride_id, now_dt)

                # Park-hours gate: still record the change above (so
                # analytics + DOWN_SINCE tracking work), but don't
                # send the alert if we're outside operating hours or
                # within the closing buffer.
                if not alerts_allowed(park_key):
                    continue

                # Cooldown check: don't re-alert if we already pinged
                # for this ride in the last DOWN_ALERT_COOLDOWN_SECS.
                # (Themeparks.wiki occasionally flaps a ride
                # OPERATING→DOWN→OPERATING within minutes.)
                if db.is_down_alert_on_cooldown(ride_id):
                    print(f"[poller] Skipping DOWN alert for {attr['name']} (cooldown)")
                    continue

                db.mark_down_alert_sent(ride_id)
                total_alerts += _fanout(
                    subscribers, get_user_key,
                    notifier.alert_ride_down,
                    ride_name=attr["name"],
                    park_name=attr["park_name"],
                    park_key=park_key,
                )

            # ── BACK UP: was DOWN, now OPERATING ───────────────────
            elif new_status == "OPERATING" and old_status == "DOWN":
                went_down = db.get_down_since(ride_id)
                actual_mins = None
                if went_down:
                    actual_mins = round((now_dt - went_down).total_seconds() / 60)
                db.clear_down_since(ride_id)

                # Park-hours gate also applies to "back up" alerts —
                # if we suppressed the DOWN alert at park-close, the
                # matching UP alert at park-open the next morning
                # would be confusing context-free noise.
                if not alerts_allowed(park_key):
                    continue

                total_alerts += _fanout(
                    subscribers, get_user_key,
                    notifier.alert_ride_up,
                    ride_name=attr["name"],
                    park_name=attr["park_name"],
                    park_key=park_key,
                    wait_mins=new_wait,
                    actual_downtime_mins=actual_mins,
                )

            # CLOSED transitions intentionally don't alert — too noisy
            # at park closing time.

    # ── Second alert sweep: rides down >= SECOND_ALERT_MINS ───────
    # Runs once per invocation after all parks processed.
    for ride_id, attr in currently_down:
        # Park-hours gate applies here too — no "still down" pings
        # at 11pm just because the ride went down at park-close.
        if not alerts_allowed(attr["park_key"]):
            continue
        went_down = db.get_down_since(ride_id)
        if not went_down:
            continue
        elapsed_mins = (datetime.now(timezone.utc) - went_down).total_seconds() / 60
        if elapsed_mins < SECOND_ALERT_MINS:
            continue

        # Use a separate cooldown key so the still-down alert doesn't
        # collide with the initial DOWN cooldown.
        cooldown_pk = f"RIDE#{ride_id}"
        cooldown_sk = "COOLDOWN#STILL_DOWN"
        # Reuse the same TTL helper by writing directly — small enough
        # to inline here without bloating db.py.
        from db import _table  # type: ignore
        existing_cooldown = _table.get_item(Key={"PK": cooldown_pk, "SK": cooldown_sk}).get("Item")
        if existing_cooldown:
            continue
        expire_ts = int(time.time()) + (SECOND_ALERT_MINS * 60)
        _table.put_item(Item={
            "PK": cooldown_pk,
            "SK": cooldown_sk,
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "ttl": expire_ts,
        })

        subscribers = get_subscribers(attr["park_key"])
        total_alerts += _fanout(
            subscribers, get_user_key,
            notifier.alert_still_down,
            ride_name=attr["name"],
            park_name=attr["park_name"],
            park_key=attr["park_key"],
            minutes_down=int(elapsed_mins),
        )

    elapsed = time.time() - started
    print(
        f"[poller] Done. {total_changes} status changes, "
        f"{total_alerts} alerts sent, {elapsed:.1f}s elapsed"
    )
    return {
        "status": "ok",
        "parks_polled": len(PARK_KEYS),
        "changes": total_changes,
        "alerts_sent": total_alerts,
        "elapsed_secs": round(elapsed, 1),
    }


def _fanout(subscribers: list[str], get_user_key, alert_fn, **kwargs) -> int:
    """Send the same alert to every subscriber. Returns count sent."""
    sent = 0
    for user_id in subscribers:
        user_key = get_user_key(user_id)
        if not user_key:
            print(f"[poller] No pushover_user_key for user {user_id} — skipping")
            continue
        if alert_fn(user_key, **kwargs):
            sent += 1
    return sent
