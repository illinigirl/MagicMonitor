"""LOW_VS_FORECAST signal: catches rides beating today's forecast on
heavier-than-forecast days.

Companion to the existing LOW_WAIT alert. Where LOW_WAIT compares
the current wait to a *historical* per-(ride, hour) baseline from
`baselines.json`, this module compares against today's *forecast*
for the same ride — normalized against the park-wide today_vs_forecast
ratio so the signal stays meaningful on quiet days too.

Why two baselines instead of one:

- LOW_WAIT (historical): "this ride is anomalously low for this
  hour, all-time." Catches end-of-day Pirates, fireworks-time
  Carousel of Progress. Misses opportunities on heavy-crowd days
  because absolute waits are still above the half-typical floor.
- LOW_VS_FORECAST (today-aware): "this ride is beating today's
  specific expectation." Catches the heavy-crowd day where Big
  Thunder hits 40 min when today's forecast said 65.

The killer case is `today_vs_forecast > 1.15` days. On those days
LOW_WAIT essentially never fires; LOW_VS_FORECAST is what catches
genuinely-better-than-today's-load moments.

Design choices:

- Park-wide normalization, not raw `current < forecast`. A naive
  per-ride rule would over-fire on uniformly-quiet days when
  *everything* is below forecast. The condition we fire on is
  "this ride is meaningfully better than the park-wide load this
  hour" — `ride_ratio <= 0.75 * park_ratio` AND
  `current_wait <= forecast - 15`.

- Sample-size floor (n >= 5 rides sampled in the park) before
  firing anything. Early morning with 3-4 rides operating, the
  park_ratio is too noisy to trust. Mirrors the MCP planner's
  same noise-floor exclusion.

- Quiet-day suppression: `park_ratio < 0.9` blocks all firings.
  On those days everything is below forecast and the alert
  would spam.

- All thresholds env-var configurable. We have ~13 days of
  FORECAST# data as of 2026-05-25 (Phase A2 shipped 2026-05-10),
  so the defaults are first-pass tuning. Plan to revisit after
  ~30 days of observation.

Same shape as the MCP server's `_compute_load_vs_forecast` so the
two implementations stay aligned; this is a second copy living in
Lambda runtime where importing the MCP server isn't an option,
same trade-off as the showtimes classifier dual-impl.
"""

import os
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

_EASTERN = ZoneInfo("America/New_York")

# ─── Tunable thresholds ─────────────────────────────────────────────
# Env-var configurable because the data plane is young (~13 days of
# FORECAST# rows). Tune from real observation, not bench guesses.

# Suppress firings on uniformly-quiet days. park_ratio < this means
# the park is broadly running below forecast — per-ride alerts on
# those days are spam, not signal.
MIN_PARK_RATIO = float(
    os.environ.get("LOW_VS_FORECAST_MIN_PARK_RATIO", "0.9")
)

# A ride must be beating park-wide load by at least (1 - this) to
# fire. 0.75 means "ride_ratio at most 75% of park_ratio" — a 25%
# gap relative to the rest of the park.
RIDE_RATIO_MULT = float(
    os.environ.get("LOW_VS_FORECAST_RIDE_RATIO_MULT", "0.75")
)

# Absolute-minutes floor. Without this, a forecast of 12 min with
# current of 8 would satisfy the ratio test but isn't a worth-
# opening-Pushover-for opportunity. Require a real gap.
MIN_ABS_GAP_MINS = int(
    os.environ.get("LOW_VS_FORECAST_MIN_ABS_GAP", "15")
)

# Sample-size floor for trusting park_ratio. With <5 rides sampled
# the ratio is too noisy (one outlier dominates). Mirrors the MCP
# planner's confidence labelling.
MIN_RIDES_SAMPLED = int(
    os.environ.get("LOW_VS_FORECAST_MIN_RIDES_SAMPLED", "5")
)

# Per-ride forecast noise floor — predicted waits under this are
# excluded from both park_ratio and per-ride firing decisions. A
# predicted=5 reporting actual=20 is a 4x ratio on tiny numbers;
# that's noise, not crowds.
MIN_PREDICTED_WAIT = int(
    os.environ.get("LOW_VS_FORECAST_MIN_PREDICTED_WAIT", "10")
)


def find_forecast_for_hour(
    forecast: Optional[list[dict]], now_et: datetime
) -> Optional[int]:
    """Return today's forecast wait for the current ET hour, or None.

    Matches by date AND hour so overnight queries don't accidentally
    pick a previous day's same-hour entry. Returns None when forecast
    is missing, malformed, or doesn't include the current hour.
    """
    if not forecast:
        return None
    today_iso = now_et.date().isoformat()
    current_hour = now_et.hour
    for entry in forecast:
        try:
            t = datetime.fromisoformat(entry["time"])
        except (KeyError, ValueError, TypeError):
            continue
        t_et = t.astimezone(_EASTERN)
        if t_et.date().isoformat() == today_iso and t_et.hour == current_hour:
            wait = entry.get("wait_mins")
            return int(wait) if isinstance(wait, (int, float)) else None
    return None


def compute_park_load_ratio(
    attractions: list[dict], now_et: Optional[datetime] = None
) -> tuple[Optional[float], int]:
    """Return (park_ratio, n_sampled) for the given park's attractions.

    park_ratio = sum(actual) / sum(predicted) across operating rides
    that have both a current wait and a forecast entry for the
    current ET hour, excluding noise-floor cases (predicted < 10).

    The aggregate form is equivalent to a wait-weighted mean of
    per-ride ratios — weighting matters so the signal is pulled
    toward high-traffic rides that actually drive park-wide load.

    Returns (None, 0) when not computable (no qualifying rides or
    zero total predicted).
    """
    if now_et is None:
        now_et = datetime.now(_EASTERN)
    total_actual = 0
    total_predicted = 0
    n = 0
    for attr in attractions:
        if attr.get("status") != "OPERATING":
            continue
        actual = attr.get("wait_mins")
        if not isinstance(actual, (int, float)):
            continue
        predicted = find_forecast_for_hour(attr.get("forecast"), now_et)
        if predicted is None or predicted < MIN_PREDICTED_WAIT:
            continue
        total_actual += actual
        total_predicted += predicted
        n += 1
    if n == 0 or total_predicted == 0:
        return None, 0
    return round(total_actual / total_predicted, 3), n


def should_fire_low_vs_forecast(
    current_wait: int,
    forecast_wait: Optional[int],
    park_ratio: Optional[float],
    rides_sampled: int,
) -> bool:
    """Apply the three-gate threshold test for the per-ride alert.

    Gates:
      1. park_ratio is computable AND rides_sampled >= MIN_RIDES_SAMPLED.
      2. park_ratio >= MIN_PARK_RATIO (suppress on uniformly-quiet days).
      3. forecast for the current hour exists AND >= MIN_PREDICTED_WAIT
         (noise floor) AND current_wait <= forecast - MIN_ABS_GAP_MINS
         (meaningful absolute gap).
      4. ride_ratio (current / forecast) <= park_ratio * RIDE_RATIO_MULT
         (this ride beats park-wide load by the configured margin).

    Each gate is independent; all must pass.
    """
    if park_ratio is None or rides_sampled < MIN_RIDES_SAMPLED:
        return False
    if park_ratio < MIN_PARK_RATIO:
        return False
    if forecast_wait is None or forecast_wait < MIN_PREDICTED_WAIT:
        return False
    if current_wait > forecast_wait - MIN_ABS_GAP_MINS:
        return False
    ride_ratio = current_wait / forecast_wait
    return ride_ratio <= park_ratio * RIDE_RATIO_MULT
