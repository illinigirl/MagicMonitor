"""
Tests for `_compute_load_vs_forecast` in mcp/server.py.

This is the live "today is running X% above/below forecast" signal
the agentic planner uses to scale cost-of-delay reasoning. Same
pre-computation pattern as `_compute_calibration_summary` — the
data plane does the math (wait-weighted ratio across operating
rides), the LLM narrates the interpretation.

The function uses datetime.now() internally to find the current ET
hour, so test fixtures build forecast entries dynamically using
runtime "now" — ensures tests are deterministic regardless of when
they run.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import server

_ET = ZoneInfo("America/New_York")


def _current_hour_forecast_entry(predicted_wait: int) -> dict:
    """Build a forecast entry pinned to the current ET hour so
    `_compute_load_vs_forecast` finds it when scanning forecast lists."""
    now = datetime.now(_ET).replace(minute=0, second=0, microsecond=0)
    return {
        "time": now.isoformat(),
        "wait_mins": predicted_wait,
    }


def _ride(
    name: str,
    status: str,
    wait_mins: int | None,
    predicted_wait: int | None,
) -> dict:
    """Construct a ride dict in the shape `_compute_load_vs_forecast`
    expects from the rides_out list inside `get_planning_context`."""
    forecast = (
        [_current_hour_forecast_entry(predicted_wait)]
        if predicted_wait is not None else None
    )
    return {
        "ride_name": name,
        "status": status,
        "wait_mins": wait_mins,
        "forecast": forecast,
    }


# ── Park ratio math ──

class TestParkRatio:
    def test_above_forecast(self):
        """Three rides, all running higher than predicted → ratio > 1.
        Wait-weighted sum: actual 75+45+60=180, predicted 50+30+40=120 → 1.5
        n>=3 so the function reaches the above/below interpretation branch
        (n<3 short-circuits to a 'directional only' message)."""
        rides = [
            _ride("Big Thunder", "OPERATING", 75, 50),
            _ride("Pirates",     "OPERATING", 45, 30),
            _ride("Space Mtn",   "OPERATING", 60, 40),
        ]
        result = server._compute_load_vs_forecast(rides)
        assert result is not None
        assert result["park_load_ratio"] == 1.5
        # "ABOVE forecast" must appear in the interpretation
        assert "ABOVE forecast" in result["interpretation"]

    def test_below_forecast(self):
        """Crowds lighter than predicted → ratio < 1. Same n>=3 reason."""
        rides = [
            _ride("Big Thunder", "OPERATING", 20, 50),
            _ride("Pirates",     "OPERATING", 15, 30),
            _ride("Space Mtn",   "OPERATING", 16, 40),
        ]
        result = server._compute_load_vs_forecast(rides)
        assert result["park_load_ratio"] < 1.0
        assert "BELOW forecast" in result["interpretation"]

    def test_close_to_forecast(self):
        """Ratio within 10% of 1.0 → 'running close to forecast'."""
        rides = [
            _ride("Big Thunder", "OPERATING", 50, 50),
            _ride("Pirates",     "OPERATING", 30, 30),
            _ride("Space Mtn",   "OPERATING", 40, 40),
        ]
        result = server._compute_load_vs_forecast(rides)
        assert result["park_load_ratio"] == 1.0
        assert "close to forecast" in result["interpretation"]


# ── Exclusions ──

class TestExclusions:
    def test_excludes_down_rides(self):
        """DOWN rides have no comparable forecast; they're dropped from
        the ratio calculation entirely."""
        rides = [
            _ride("Big Thunder", "OPERATING", 75, 50),
            _ride("Splash",      "DOWN",      None, None),
            _ride("Pirates",     "OPERATING", 45, 30),
        ]
        result = server._compute_load_vs_forecast(rides)
        # Two rides survive, both operating, weighted ratio 1.5
        assert result["rides_sampled"] == 2
        assert result["park_load_ratio"] == 1.5

    def test_excludes_low_predicted_rides_as_noise(self):
        """Predicted wait <10 min is in the noise zone — a 5-min wait
        reporting 15 min is a 3x ratio on tiny numbers. Filtered."""
        rides = [
            _ride("Big Thunder", "OPERATING", 60, 50),
            _ride("People Mover", "OPERATING", 15, 5),  # tiny → filter
            _ride("Carousel",    "OPERATING", 12, 4),   # tiny → filter
        ]
        result = server._compute_load_vs_forecast(rides)
        assert result["rides_sampled"] == 1

    def test_excludes_rides_without_forecast(self):
        """Some rides never have forecasts (walk-ups, transport).
        They should be silently skipped, not crash the call."""
        rides = [
            _ride("Big Thunder",     "OPERATING", 60, 50),
            _ride("Liberty Square",  "OPERATING", 10, None),  # no forecast
        ]
        result = server._compute_load_vs_forecast(rides)
        # Only Big Thunder counts.
        assert result["rides_sampled"] == 1


# ── Confidence labeling ──

class TestConfidence:
    def test_low_confidence_below_3_samples(self):
        """<3 rides sampled → confidence=low, direction-only."""
        rides = [
            _ride("Big Thunder", "OPERATING", 75, 50),
            _ride("Pirates",     "OPERATING", 45, 30),
        ]
        result = server._compute_load_vs_forecast(rides)
        assert result["confidence"] == "low"
        assert "directional" in result["interpretation"]

    def test_high_confidence_at_5_samples_with_close_ratio(self):
        """Close-to-forecast at any n>=3 reads 'high' because the
        signal is 'no adjustment needed' — not sensitive to n."""
        rides = [
            _ride("Big Thunder", "OPERATING", 50, 50),
            _ride("Pirates",     "OPERATING", 30, 30),
            _ride("Space Mtn",   "OPERATING", 40, 40),
        ]
        result = server._compute_load_vs_forecast(rides)
        assert result["confidence"] == "high"

    def test_medium_confidence_with_3to4_samples_and_meaningful_delta(self):
        """3-4 samples + meaningful ratio shift = medium, not high."""
        rides = [
            _ride("Big Thunder", "OPERATING", 75, 50),
            _ride("Pirates",     "OPERATING", 45, 30),
            _ride("Space Mtn",   "OPERATING", 60, 40),
        ]
        # Three rides, all running high → ratio ~1.5 with n=3
        result = server._compute_load_vs_forecast(rides)
        assert result["confidence"] == "medium"


# ── Edge cases ──

class TestEdgeCases:
    def test_returns_none_when_no_rides_qualify(self):
        """Every ride either DOWN, has no forecast, or has tiny
        predicted wait → no sample, no result."""
        rides = [
            _ride("Splash",       "DOWN",      None, None),
            _ride("Walk-up Meet", "OPERATING", 5, None),
            _ride("People Mover", "OPERATING", 10, 4),
        ]
        assert server._compute_load_vs_forecast(rides) is None

    def test_returns_none_for_empty_input(self):
        assert server._compute_load_vs_forecast([]) is None

    def test_per_ride_breakdown_included_in_response(self):
        """The response carries per-ride detail alongside the park
        rollup — the planner uses this for ride-specific reasoning."""
        rides = [
            _ride("Big Thunder", "OPERATING", 75, 50),
            _ride("Pirates",     "OPERATING", 45, 30),
        ]
        result = server._compute_load_vs_forecast(rides)
        assert "per_ride" in result
        assert len(result["per_ride"]) == 2
        bt = next(r for r in result["per_ride"] if r["ride_name"] == "Big Thunder")
        assert bt["actual_wait_mins"] == 75
        assert bt["predicted_wait_this_hour"] == 50
        assert bt["ratio"] == 1.5
