"""
Tests for weather.py — storm-shift detection and window formatting.

These are pure functions (no network, no AWS) so they run fast and
deterministic. Catches the classes of bug that matter for the
plan-weather-shift alert path:

  - False positives: alerting when there's no real shift (e.g., storm
    was already in the prior forecast)
  - False negatives: missing a genuine new storm appearance
  - Edge cases: None prior forecast (fresh deploy, TTL'd snapshot),
    storm clearing (we deliberately don't alert on that for v1)
  - Window phrase: must be human-readable for the Pushover body
"""

import weather


# ── Fixtures (plain dicts, no fancy framework) ──

CLEAR_FORECAST = {
    "next_6h": [
        {"time": "2026-05-12T15:00", "weather_code": 1,  "precipitation_chance_pct": 10},
        {"time": "2026-05-12T16:00", "weather_code": 2,  "precipitation_chance_pct": 20},
        {"time": "2026-05-12T17:00", "weather_code": 3,  "precipitation_chance_pct": 30},
    ],
}

STORM_FORECAST = {
    "next_6h": [
        {"time": "2026-05-12T15:00", "weather_code": 2,  "precipitation_chance_pct": 30},
        {"time": "2026-05-12T16:00", "weather_code": 80, "precipitation_chance_pct": 70},
        {"time": "2026-05-12T17:00", "weather_code": 95, "precipitation_chance_pct": 90},
        {"time": "2026-05-12T18:00", "weather_code": 95, "precipitation_chance_pct": 90},
    ],
}


class TestDetectStormShift:
    """Verify the storm-shift detection logic that gates plan-weather alerts."""

    def test_clear_to_clear_no_shift(self):
        """No storm in either snapshot → no alert."""
        assert weather.detect_storm_shift(CLEAR_FORECAST, CLEAR_FORECAST) is None

    def test_clear_to_storm_detects_shift(self):
        """The killer case — storm newly enters the forecast."""
        shift = weather.detect_storm_shift(CLEAR_FORECAST, STORM_FORECAST)
        assert shift is not None
        # Two storm codes in next_6h (the 95s; 80 is heavy rain, not storm).
        assert shift["next_6h_hit_count"] == 2
        assert shift["first_storm_code"] == 95

    def test_storm_to_storm_no_re_alert(self):
        """Storm was already known → don't re-alert. Deliberate asymmetry."""
        assert weather.detect_storm_shift(STORM_FORECAST, STORM_FORECAST) is None

    def test_none_prior_treats_storm_as_new(self):
        """Fresh deploy / TTL'd snapshot: no prior = treat storm as new.
        Accepts at most one spurious alert per cold-start, contained by
        the per-plan cooldown. Documented in weather.py."""
        shift = weather.detect_storm_shift(None, STORM_FORECAST)
        assert shift is not None
        assert shift["first_storm_code"] == 95

    def test_storm_to_clear_no_alert(self):
        """v1 doesn't alert on storm clearing — only on storm appearing.
        Tracks the design decision documented in weather.py."""
        assert weather.detect_storm_shift(STORM_FORECAST, CLEAR_FORECAST) is None

    def test_none_current_returns_none(self):
        """Defensive: failed weather fetch → no shift, don't crash."""
        assert weather.detect_storm_shift(STORM_FORECAST, None) is None

    def test_includes_hours_until_storm(self):
        """The Pushover body uses hours_until — verify it's surfaced."""
        shift = weather.detect_storm_shift(CLEAR_FORECAST, STORM_FORECAST)
        assert "hours_until_storm" in shift
        assert isinstance(shift["hours_until_storm"], int)


class TestFormatStormWindow:
    """Verify the human-readable window phrase used in Pushover bodies."""

    def test_non_empty(self):
        shift = weather.detect_storm_shift(CLEAR_FORECAST, STORM_FORECAST)
        phrase = weather.format_storm_window(shift)
        assert phrase
        assert isinstance(phrase, str)

    def test_includes_clock_time(self):
        """Should surface an AM/PM clock reference."""
        shift = weather.detect_storm_shift(CLEAR_FORECAST, STORM_FORECAST)
        phrase = weather.format_storm_window(shift)
        # Either "AM" or "PM" should appear in the phrase since the
        # storm hour parses successfully.
        assert "AM" in phrase or "PM" in phrase

    def test_handles_missing_time_gracefully(self):
        """Defensive: bogus input shouldn't crash the alert path."""
        phrase = weather.format_storm_window({"first_storm_at": None})
        assert isinstance(phrase, str)
