"""
Weather fetch + shift detection for the poller's plan-aware alert path.

Why this lives in the poller (and duplicates mcp/server.py's
_fetch_weather_forecast) instead of being shared: the MCP server and
the Lambda run in different Python runtimes with different dep sets,
and the function is small enough that a verbatim copy is cheaper than
plumbing a shared module across the deploy boundary.

The shape of the fetched payload here intentionally matches the slice
of fields the MCP planner consumes — keeps debugging "what did the
poller see when it fired this alert?" easy to cross-reference against
what get_planning_context surfaces to Claude.

Trigger model (intentionally narrow for v1):
  Fire a plan-weather-shift alert when the new forecast contains a
  thunderstorm code (weather_code >= 95) anywhere in the next 6 hours
  AND the prior forecast did not. Storm = lightning hold = Disney
  pauses outdoor rides — a real operational shift that warrants
  re-planning. Florida afternoon rain (precip_chance jumps without
  storm risk) is everyday noise and intentionally excluded here.
"""

import os
from typing import Optional

import requests

# Walt Disney World's geographic center. Same constants used by
# mcp/server.py for parity — all four parks are <6km from this point,
# so one weather forecast covers all of them.
_WDW_LAT = 28.3852
_WDW_LON = -81.5639

# Open-Meteo WMO weather codes that indicate a thunderstorm. From the
# legend Open-Meteo publishes: 95 = thunderstorm, 96 = thunderstorm
# with slight hail, 99 = thunderstorm with heavy hail. Anything in
# this set means Disney will halt outdoor rides for lightning.
_STORM_CODES = frozenset({95, 96, 99})

# A prior snapshot older than this can't suppress a new-storm alert. The
# snapshot only updates while a plan is active, so on a multi-day trip it
# freezes overnight — a 12h-old prior still showing yesterday's storm
# would otherwise classify a genuinely new next-day storm as "already
# known" and suppress it. 20 min is far beyond the 2-min active-poll
# cadence, so consecutive-poll comparisons are unaffected; the per-plan
# weather cooldown backstops any over-eager re-alert.
_PRIOR_FRESHNESS_SECS = 20 * 60

# Bound the comparison window. Open-Meteo returns 6 hours by default;
# we use the same window for shift detection so the alert reflects
# something within the user's plan day.
FETCH_TIMEOUT_SECS = float(os.environ.get("WEATHER_FETCH_TIMEOUT_SECS", "5"))


def fetch_forecast() -> Optional[dict]:
    """Return current conditions + 6-hour forecast from Open-Meteo.

    Returns the trimmed dict shape the rest of the module operates on,
    or None on any failure. Callers must treat None as "skip weather
    work this poll, no shift detection possible" — fail-quiet so the
    weather path can never break the rest of the alert pipeline.

    Schema:
      {
        "fetched_at": "<iso utc>",
        "current": {"weather_code": int | None, "temp_f": float | None,
                    "precipitation_inches": float | None},
        "next_6h": [
          {"time": "<iso local>", "weather_code": int | None,
           "temp_f": float | None, "precipitation_chance_pct": int | None},
          ...
        ],
      }
    """
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={_WDW_LAT}&longitude={_WDW_LON}"
        "&current=temperature_2m,precipitation,weather_code"
        "&hourly=temperature_2m,precipitation_probability,weather_code"
        "&temperature_unit=fahrenheit&precipitation_unit=inch"
        "&forecast_hours=6&timezone=America/New_York"
    )
    try:
        r = requests.get(url, timeout=FETCH_TIMEOUT_SECS)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[weather] fetch failed: {e}")
        return None

    from datetime import datetime, timezone
    current = data.get("current") or {}
    hourly = data.get("hourly") or {}
    times = hourly.get("time", []) or []
    temps = hourly.get("temperature_2m", []) or []
    precip = hourly.get("precipitation_probability", []) or []
    codes = hourly.get("weather_code", []) or []

    next_hours = [
        {
            "time":                       t,
            "temp_f":                     temps[i]  if i < len(temps)  else None,
            "precipitation_chance_pct":   precip[i] if i < len(precip) else None,
            "weather_code":               codes[i]  if i < len(codes)  else None,
        }
        for i, t in enumerate(times)
    ]

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "current": {
            "temp_f":               current.get("temperature_2m"),
            "precipitation_inches": current.get("precipitation"),
            "weather_code":         current.get("weather_code"),
        },
        "next_6h": next_hours,
    }


def _storm_hours(forecast: Optional[dict]) -> list[dict]:
    """Return the entries in forecast.next_6h whose weather_code is a
    thunderstorm code. Includes the current-conditions hour if it is
    already storming. Empty list if forecast is None or has none."""
    if not forecast:
        return []
    hits: list[dict] = []
    for entry in forecast.get("next_6h", []) or []:
        if entry.get("weather_code") in _STORM_CODES:
            hits.append(entry)
    return hits


def _prior_is_stale(prior: Optional[dict]) -> bool:
    """True if the prior snapshot is too old to be a meaningful baseline
    (see _PRIOR_FRESHNESS_SECS). A stale prior is treated as "no baseline"
    so a new storm still fires rather than being suppressed by yesterday's."""
    if not prior:
        return False
    fetched_at = prior.get("fetched_at")
    if not fetched_at:
        # No timestamp → can't prove staleness. Behave as a valid baseline
        # (suppress) rather than re-alert; production snapshots always set
        # fetched_at, so this only guards malformed/legacy rows.
        return False
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(fetched_at)
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - dt).total_seconds()
    return age > _PRIOR_FRESHNESS_SECS


def detect_storm_shift(
    prior: Optional[dict], current: Optional[dict]
) -> Optional[dict]:
    """Compare two forecasts; return a shift descriptor if a NEW
    thunderstorm risk emerged in the 6-hour window, else None.

    "New" means: prior forecast had no storm codes in next_6h (or no
    prior at all), AND current forecast has at least one storm code in
    next_6h. The asymmetry is deliberate — when prior already showed
    storm risk we've either already alerted on it or the user knew
    going in; re-alerting on every poll while a storm is still in the
    forecast would be classic noise.

    Returns:
        None when no shift, else a dict like:
        {
            "first_storm_at": "<iso local time>",
            "first_storm_code": 95,
            "hours_until_storm": 2,  # rounded, integer
            "next_6h_hit_count": 3,
        }

    The prior == None case (very first invocation after a fresh deploy)
    is treated as "we knew nothing, so a storm forecast IS new" —
    accepts a single false positive per cold-start at most, with the
    cooldown catching any repeat within the hour. Trade: we'd rather
    alert once spuriously after a deploy than miss the real first
    detection.
    """
    if not current:
        return None
    current_storms = _storm_hours(current)
    if not current_storms:
        return None
    # A prior that still shows storms suppresses the alert — UNLESS the
    # prior is stale (frozen overnight on a multi-day trip), in which case
    # it's not a valid baseline and a current storm counts as new.
    prior_storms = _storm_hours(prior)
    if prior_storms and not _prior_is_stale(prior):
        return None

    first = current_storms[0]
    hours_until = _estimate_hours_until(current, first)
    return {
        "first_storm_at":    first.get("time"),
        "first_storm_code":  first.get("weather_code"),
        "hours_until_storm": hours_until,
        "next_6h_hit_count": len(current_storms),
    }


def _estimate_hours_until(forecast: dict, target_entry: dict) -> int:
    """Hours from now until `target_entry["time"]`. Open-Meteo returns
    `time` as a naive "YYYY-MM-DDTHH:00" string in the timezone we
    requested (America/New_York). We compare it against the same
    timezone's current hour to keep the math simple.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    t = target_entry.get("time")
    if not t:
        return 0
    try:
        eastern = ZoneInfo("America/New_York")
        target = datetime.fromisoformat(t).replace(tzinfo=eastern)
        now = datetime.now(eastern)
        delta = (target - now).total_seconds() / 3600.0
        return max(0, round(delta))
    except Exception:
        return 0


def format_storm_window(shift: dict) -> str:
    """Build the short human-readable phrase the Pushover body uses.
    Centralized here so the alert text and the log line agree."""
    when = shift.get("first_storm_at") or "in the next few hours"
    # Open-Meteo's local timestamps are "YYYY-MM-DDTHH:00"; show just
    # HH:MM for body brevity. Fall back to the raw string if parse fails.
    try:
        from datetime import datetime
        clock = datetime.fromisoformat(when).strftime("%-I:%M %p")
    except Exception:
        clock = when
    hrs = shift.get("hours_until_storm")
    if hrs and hrs > 0:
        return f"around {clock} (~{hrs}h from now)"
    return f"around {clock}"
