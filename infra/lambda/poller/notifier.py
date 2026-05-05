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
    payload = {
        "token":    _get_app_token(),
        "user":     user_key,
        "title":    title,
        "message":  message,
        "priority": priority,
    }
    try:
        resp = requests.post(PUSHOVER_URL, data=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[notifier] Pushover send failed for user={user_key[:6]}…: {e}")
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
