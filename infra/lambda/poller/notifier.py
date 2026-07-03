"""
Pushover notification sender.

Reads credentials from SSM Parameter Store on cold start and caches them
for the lifetime of the Lambda container.

Pushover API docs: https://pushover.net/api

The shape mirrors the Pi version's notifier.py — same alert types
(ride down, ride up, still down, low wait) so the demo prose lines
up with the existing app.
"""

import os
import time
from typing import Optional

import boto3
import requests

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
PUSHOVER_APP_TOKEN_PARAM = os.environ["PUSHOVER_APP_TOKEN_PARAM"]

# Park emoji prefixes for visual scanning in Pushover. Matches the Pi
# version's notifier.py so the family's existing alert format is
# preserved.
PARK_EMOJI = {
    "magic_kingdom":     "🏰",
    "epcot":             "🌐",
    "hollywood_studios": "🎬",
    "animal_kingdom":    "🐘",
}

_ssm = boto3.client("ssm")
_app_token: Optional[str] = None


def _get_app_token() -> str:
    """Lazily fetch + cache the Pushover app token from SSM."""
    global _app_token
    if _app_token is None:
        resp = _ssm.get_parameter(Name=PUSHOVER_APP_TOKEN_PARAM, WithDecryption=True)
        _app_token = resp["Parameter"]["Value"]
    return _app_token


def _send(user_key: str, title: str, message: str, priority: int = 0) -> bool:
    """POST a single Pushover message. Returns True on success.

    Priority: 0 = normal, -1 = quiet (no sound), 1 = high (bypass quiet
    hours). DOWN alerts use priority 1 so they punch through; LOW WAIT
    and back-up alerts use priority 0.
    """
    try:
        # _get_app_token() is inside the try on purpose: it hits SSM lazily
        # on the first alert a container sends, and an SSM throttle/timeout
        # there must be CONTAINED to this one send (return False), not
        # raised out of the per-attraction loop — which would abort the
        # whole poll after the DOWN cooldown was already marked, losing the
        # alert for the full cooldown window on the EventBridge retry.
        payload = {
            "token":    _get_app_token(),
            "user":     user_key,
            "title":    title,
            "message":  message,
            "priority": priority,
        }
        resp = requests.post(PUSHOVER_URL, data=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[notifier] alert send failed for user={user_key[:6]}…: {e}")
        return False


# ─── Public alert functions ─────────────────────────────────────────
# Each takes the recipient's Pushover user_key explicitly so the caller
# (handler) controls fanout. Returns True/False per recipient.

def alert_ride_down(
    user_key: str,
    ride_name: str,
    park_name: str,
    park_key: str,
    avg_downtime_mins: Optional[int] = None,
) -> bool:
    emoji = PARK_EMOJI.get(park_key, "🎢")
    title = f"{emoji} {ride_name} is DOWN"
    body = f"{park_name}\n{ride_name} just went down."
    if avg_downtime_mins:
        body += f"\nTypical outage: ~{avg_downtime_mins} min."
    return _send(user_key, title, body, priority=1)


def alert_ride_up(
    user_key: str,
    ride_name: str,
    park_name: str,
    park_key: str,
    wait_mins: Optional[int],
    actual_downtime_mins: Optional[int],
) -> bool:
    emoji = PARK_EMOJI.get(park_key, "🎢")
    title = f"{emoji} {ride_name} is back up"
    body = f"{park_name}\n{ride_name} is operating again."
    if actual_downtime_mins is not None:
        body += f"\nDown for {actual_downtime_mins} min."
    if wait_mins is not None:
        body += f"\nCurrent wait: {wait_mins} min."
    return _send(user_key, title, body, priority=0)


def alert_still_down(
    user_key: str,
    ride_name: str,
    park_name: str,
    park_key: str,
    minutes_down: int,
) -> bool:
    emoji = PARK_EMOJI.get(park_key, "🎢")
    title = f"{emoji} {ride_name} still down"
    body = f"{park_name}\n{ride_name} has been down for {minutes_down} min."
    return _send(user_key, title, body, priority=0)


def alert_low_wait(
    user_key: str,
    ride_name: str,
    park_name: str,
    park_key: str,
    wait_mins: int,
    typical_wait_mins: int | None = None,
    forecast_wait_mins: int | None = None,
) -> bool:
    """Fire when a ride's current wait beats one of two baselines:
    historical (typical for this hour) or today's forecast.

    Body text adapts to which baseline(s) triggered — the LOW_WAIT
    and LOW_VS_FORECAST signals share this notifier + a single
    cooldown row, so a ride gets one low-wait-class push per window
    regardless of which condition tripped. At least one of
    `typical_wait_mins` / `forecast_wait_mins` must be provided.

    Lower priority than DOWN — it's an opportunity, not a breakdown
    — but worth opening Pushover for.
    """
    emoji = PARK_EMOJI.get(park_key, "🎢")
    title = f"{emoji} {ride_name} — low wait now"
    # Order the comparisons: typical first (the all-time anchor),
    # forecast second (the today-specific add-on). Both null is a
    # caller bug — render the minimal sentence and let logs surface it.
    parts: list[str] = [
        f"{park_name}",
        f"{ride_name} is at {wait_mins} min.",
    ]
    if typical_wait_mins is not None:
        parts.append(f"Typical for this hour: ~{typical_wait_mins} min.")
    if forecast_wait_mins is not None:
        parts.append(f"Today's forecast: {forecast_wait_mins} min.")
    body = "\n".join(parts[:1]) + "\n" + " ".join(parts[1:])
    return _send(user_key, title, body, priority=0)


def alert_plan_low_wait(
    user_key: str,
    ride_name: str,
    park_name: str,
    park_key: str,
    wait_mins: int,
    typical_wait_mins: int | None = None,
    forecast_wait_mins: int | None = None,
    plan_id: Optional[str] = None,
) -> bool:
    """Plan-aware sibling of alert_low_wait: the ride with the unusually
    short wait is in the recipient's ACTIVE plan today (still in
    ride_sequence — not yet ridden), so this is directly actionable:
    jump to it now and ride it cheaper than planned.

    Same signal + cooldown as alert_low_wait (one low-wait-class push
    per ride per window); only the framing differs. Priority 0 —
    an opportunity, not a disruption.
    """
    emoji = PARK_EMOJI.get(park_key, "🎢")
    title = f"{emoji} Plan opportunity — {ride_name} low wait"
    parts: list[str] = [
        f"{ride_name} is at {wait_mins} min right now and it's in your "
        f"plan today.",
    ]
    if typical_wait_mins is not None:
        parts.append(f"Typical for this hour: ~{typical_wait_mins} min.")
    if forecast_wait_mins is not None:
        parts.append(f"Today's forecast: {forecast_wait_mins} min.")
    parts.append("Good time to jump to it if you're close.")
    body = f"{park_name}\n" + " ".join(parts)
    return _send(user_key, title, body, priority=0)


def alert_plan_disruption(
    user_key: str,
    ride_name: str,
    park_name: str,
    park_key: str,
    disruption_type: str,
    plan_id: Optional[str] = None,
    wait_mins: Optional[int] = None,
) -> bool:
    """Fire when a ride in the recipient's active plan for today
    transitions DOWN or BACK UP. Separate from the favoriter-based
    DOWN/UP alerts: this one fires regardless of favorites because
    being in TODAY's plan is a stronger "you care about this ride
    right now" signal than having favorited it generically.

    disruption_type:
      "went_down" — ride in plan just transitioned to DOWN.
      "back_up"   — ride in plan just came back operating.

    Priority 1 for went_down (it's actionable — replan), 0 for
    back_up (good news, less urgent).
    """
    emoji = PARK_EMOJI.get(park_key, "🎢")
    if disruption_type == "went_down":
        title = f"{emoji} Plan disruption — {ride_name} DOWN"
        body = (
            f"{park_name}\n"
            f"{ride_name} just went down and it's in your plan today. "
            f"Check with Claude when you can — you may want to "
            f"re-sequence the rest of the day."
        )
        priority = 1
    elif disruption_type == "back_up":
        wait_blurb = f" Current wait: {wait_mins} min." if wait_mins is not None else ""
        title = f"{emoji} Plan update — {ride_name} back up"
        body = (
            f"{park_name}\n"
            f"{ride_name} is operating again and it's in your plan today."
            f"{wait_blurb} Let Claude know if you want to slot it back in."
        )
        priority = 0
    else:
        # Defensive: unknown disruption_type. Don't crash the poller —
        # log + skip rather than throw on a typo.
        print(f"[notifier] Unknown plan disruption_type: {disruption_type!r} (ride={ride_name})")
        return False
    return _send(user_key, title, body, priority=priority)


def alert_plan_weather_shift(
    user_key: str,
    park_name: str,
    park_key: str,
    window_phrase: str,
    plan_id: Optional[str] = None,
) -> bool:
    """Fire when a thunderstorm appears in the next-6-hour forecast that
    wasn't there on the previous poll, and the recipient has an active
    plan for the affected park today.

    Sibling of `alert_plan_disruption` — fires on the second axis of
    the agentic-loop story ("the system noticed something that
    invalidates your plan"). DOWN/UP transitions are the per-ride
    axis; this is the park-wide axis.

    Priority 1: actionable — Disney pauses outdoor rides for
    lightning, so re-sequencing the day before the storm hits is the
    play. Sticks with the same priority convention as went_down
    (also priority 1).

    `window_phrase` comes from weather.format_storm_window — keeps the
    body wording consistent with what the log line printed and avoids
    locale assumptions in the notifier.
    """
    emoji = PARK_EMOJI.get(park_key, "🎢")
    title = f"{emoji} Storm forecast — plan may shift"
    body = (
        f"{park_name}\n"
        f"Thunderstorm now in the forecast {window_phrase}. "
        f"Disney pauses outdoor rides for lightning — re-check with "
        f"Claude when you can to slot indoor rides ahead of the storm."
    )
    return _send(user_key, title, body, priority=1)
