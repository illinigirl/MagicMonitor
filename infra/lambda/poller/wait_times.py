"""
Live wait-time fetching from themeparks.wiki.

Ported from Pi/Python/disney/wait_times.py — same API, same normalization.
The function returns a list of attraction dicts ready to compare against
the previous DynamoDB state.
"""

import requests
from datetime import datetime, timezone
from typing import Optional, Tuple

# Disney World is in Eastern Time. Used to date-filter the multi-day
# schedule response down to "today's hours."
try:
    from zoneinfo import ZoneInfo
    _FLORIDA_TZ = ZoneInfo("America/New_York")
except ImportError:
    _FLORIDA_TZ = timezone.utc

# Park entity IDs from themeparks.wiki — discoverable via
#   GET https://api.themeparks.wiki/v1/destinations
# Pinned here because the IDs are stable and we don't want a runtime
# lookup on every poll.
PARK_IDS = {
    "magic_kingdom":     "75ea578a-adc8-4116-a54d-dccb60765ef9",
    "epcot":             "47f90d2c-e191-4239-a466-5892ef59a88b",
    "hollywood_studios": "288747d1-8b4f-4a64-867e-ea7c9b27bad8",
    "animal_kingdom":    "1c84a229-8862-4648-9c71-378ddd2c7693",
    "typhoon_lagoon":    "b070cbc5-feaa-4b87-a8c1-f94cca037a18",
    "blizzard_beach":    "ead53ea5-22e5-4095-9a83-8c29300d7c63",
}

BASE_URL = "https://api.themeparks.wiki/v1"


def fetch_live_data(park_key: str) -> list[dict]:
    """
    Fetch live wait time data for a single park.

    Returns a list of attraction dicts shaped like:
        {
            "id":        "<themeparks.wiki entity id>",
            "park_key":  "magic_kingdom",
            "park_id":   "<park entity id>",
            "park_name": "Magic Kingdom Park",
            "name":      "Space Mountain",
            "status":    "OPERATING" | "DOWN" | "CLOSED" | "REFURBISHMENT",
            "wait_mins": 35,                 # or None if not reported
            "ll":        {...} | None,       # current Lightning Lane offer
            "ll_state":  {...} | None,       # full LL state (for drop tracking)
            "forecast":  [{...}, ...] | None,  # hourly predictions, see below
            "last_seen": "<iso8601 utc>",
        }

    The "forecast" field — when themeparks.wiki provides one — is a list
    of dicts each shaped {time, wait_mins, percentage}. `time` is the
    raw upstream ISO-8601 string with offset (e.g. "2026-05-10T10:00:00-04:00")
    so DST transitions stay accurate; `wait_mins` is the predicted wait
    in minutes; `percentage` is upstream's relative load metric (semantics
    not officially documented). The forecast covers current-hour through
    park close, ~14 entries early, fewer as the day progresses.

    Forecasts are absent for: DOWN rides, walk-up character meets,
    no-queue attractions (transportation), and some shows. ~77% of
    attractions have one at any given time. We return None (not [])
    so the poller can cheaply skip writes — callers must check.
    """
    park_id = PARK_IDS.get(park_key)
    if not park_id:
        raise ValueError(f"Unknown park key: {park_key!r}. Valid keys: {list(PARK_IDS)}")

    url = f"{BASE_URL}/entity/{park_id}/live"
    response = requests.get(url, timeout=15)
    response.raise_for_status()

    data = response.json()
    now = datetime.now(timezone.utc).isoformat()
    park_name = data.get("name", park_key)

    attractions = []
    for entry in data.get("liveData", []):
        if entry.get("entityType") != "ATTRACTION":
            continue

        queue = entry.get("queue", {})
        standby = queue.get("STANDBY", {})
        wait_mins = standby.get("waitTime")  # None if not reported

        # Lightning Lane info — paid (Genie+/ILL) takes precedence over
        # free (Virtual Queue / standard return time).
        ll = queue.get("RETURN_TIME", {})
        paid_ll = queue.get("PAID_RETURN_TIME", {})
        ll_info = None
        if paid_ll and paid_ll.get("state") == "AVAILABLE":
            price = paid_ll.get("price", {})
            ll_info = {
                "type": "paid",
                "price": price.get("formatted", ""),
                "return_start": paid_ll.get("returnStart"),
            }
        elif ll and ll.get("state") == "AVAILABLE":
            ll_info = {
                "type": "free",
                "return_start": ll.get("returnStart"),
            }

        # Full LL state for analytics + drop tracking (M4). For M1 we
        # store it but don't act on it.
        ll_state = None
        if paid_ll:
            ll_state = {
                "type": "paid",
                "state": paid_ll.get("state", ""),
                "return_start": paid_ll.get("returnStart"),
                "price": (paid_ll.get("price") or {}).get("formatted", ""),
            }
        elif ll:
            ll_state = {
                "type": "free",
                "state": ll.get("state", ""),
                "return_start": ll.get("returnStart"),
            }

        attractions.append({
            "id":        entry["id"],
            "park_key":  park_key,
            "park_id":   park_id,
            "park_name": park_name,
            "name":      entry.get("name", "Unknown"),
            "status":    _normalize_status(entry.get("status", "")),
            "wait_mins": wait_mins,
            "ll":        ll_info,
            "ll_state":  ll_state,
            "forecast":  _normalize_forecast(entry.get("forecast")),
            "last_seen": now,
        })

    return attractions


def _normalize_forecast(raw: Optional[list]) -> Optional[list[dict]]:
    """Normalize the upstream forecast array, or return None if absent.

    Renames `waitTime` → `wait_mins` for codebase consistency; keeps
    `time` and `percentage` unchanged. Drops malformed entries silently
    rather than failing the whole poll — a single bad forecast row
    isn't worth dropping a status update for.
    """
    if not raw:
        return None
    out: list[dict] = []
    for entry in raw:
        try:
            out.append({
                "time":       entry["time"],
                "wait_mins":  entry.get("waitTime"),
                "percentage": entry.get("percentage"),
            })
        except (KeyError, TypeError):
            continue
    return out or None


def _normalize_status(raw: str) -> str:
    """Map themeparks.wiki status strings to our internal constants."""
    mapping = {
        "OPERATING":     "OPERATING",
        "DOWN":          "DOWN",
        "CLOSED":        "CLOSED",
        "REFURBISHMENT": "REFURBISHMENT",
    }
    return mapping.get(raw.upper(), raw.upper())


def fetch_park_hours(park_key: str) -> Optional[Tuple[datetime, datetime]]:
    """
    Fetch today's open/close window for a park.

    Returns (open_dt, close_dt) as timezone-aware datetimes, or None if
    the park is closed today (no OPERATING entry).

    The /schedule endpoint returns a multi-day array; we filter to
    today's entries (in the park's local time), then merge OPERATING
    + EXTRA_HOURS (early entry / extended evening hours) into a single
    window that spans the earliest open to the latest close. This is
    what users actually care about for "is the park open right now".
    """
    park_id = PARK_IDS.get(park_key)
    if not park_id:
        return None

    url = f"{BASE_URL}/entity/{park_id}/schedule"
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
    except Exception as e:
        print(f"[wait_times] schedule fetch failed for {park_key}: {e}")
        return None

    data = response.json()

    # Today in Eastern time (Disney World is in Florida; all 4 parks
    # use the same TZ). Avoids the "is it midnight UTC yet" confusion
    # at the date-rollover boundary.
    today_local = datetime.now(_FLORIDA_TZ).date().isoformat()

    open_dt: Optional[datetime] = None
    close_dt: Optional[datetime] = None

    for entry in data.get("schedule", []):
        if entry.get("date") != today_local:
            continue
        # Treat EXTRA_HOURS (Early Entry, Extended Evening) as part
        # of the operating window — rides do go down during EEH and
        # users do care about it.
        if entry.get("type") not in ("OPERATING", "EXTRA_HOURS"):
            continue

        try:
            o = datetime.fromisoformat(entry["openingTime"])
            c = datetime.fromisoformat(entry["closingTime"])
        except (KeyError, ValueError):
            continue

        if open_dt is None or o < open_dt:
            open_dt = o
        if close_dt is None or c > close_dt:
            close_dt = c

    if open_dt is None or close_dt is None:
        return None
    return (open_dt, close_dt)


