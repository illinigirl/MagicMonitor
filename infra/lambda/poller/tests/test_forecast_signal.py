"""Tests for the LOW_VS_FORECAST signal module.

Covers two surfaces:

1. `compute_park_load_ratio` — the wait-weighted park-wide ratio
   computation. Verifies aggregation math, noise-floor exclusion,
   handling of missing forecasts, and short-circuit on unsampled
   parks.
2. `should_fire_low_vs_forecast` — the per-ride three-gate
   threshold test. Verifies each gate independently fires/blocks,
   plus the killer case (heavy day + ride beating park-wide load
   by ≥25% with ≥15 min absolute gap).

Tests pin the math, not the env-var defaults. Threshold values are
provided directly in test cases so future tuning (env var changes)
doesn't break tests on inputs that should still trip the rule.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import forecast_signal as fs

_ET = ZoneInfo("America/New_York")


def _attr(
    *,
    status: str = "OPERATING",
    wait_mins: int | None = 30,
    forecast_for_current_hour: int | None = 50,
    now_et: datetime | None = None,
) -> dict:
    """Build a poller-shape attraction dict with an in-band forecast
    entry timestamped to the current ET hour."""
    if now_et is None:
        now_et = datetime.now(_ET)
    forecast = None
    if forecast_for_current_hour is not None:
        forecast = [
            {
                "time": now_et.replace(minute=0, second=0, microsecond=0).isoformat(),
                "wait_mins": forecast_for_current_hour,
                "percentage": None,
            }
        ]
    return {
        "status": status,
        "wait_mins": wait_mins,
        "forecast": forecast,
    }


class TestFindForecastForHour:
    def test_returns_current_hour_value(self):
        now = datetime(2026, 5, 25, 14, 30, tzinfo=_ET)
        forecast = [
            {"time": "2026-05-25T13:00:00-04:00", "wait_mins": 50},
            {"time": "2026-05-25T14:00:00-04:00", "wait_mins": 65},
            {"time": "2026-05-25T15:00:00-04:00", "wait_mins": 80},
        ]
        assert fs.find_forecast_for_hour(forecast, now) == 65

    def test_returns_none_when_hour_missing(self):
        now = datetime(2026, 5, 25, 22, 30, tzinfo=_ET)
        forecast = [
            {"time": "2026-05-25T13:00:00-04:00", "wait_mins": 50},
        ]
        assert fs.find_forecast_for_hour(forecast, now) is None

    def test_returns_none_for_empty_forecast(self):
        now = datetime(2026, 5, 25, 14, 30, tzinfo=_ET)
        assert fs.find_forecast_for_hour(None, now) is None
        assert fs.find_forecast_for_hour([], now) is None

    def test_skips_malformed_entries(self):
        now = datetime(2026, 5, 25, 14, 30, tzinfo=_ET)
        forecast = [
            {"time": "not-a-real-time", "wait_mins": 999},
            {"time": "2026-05-25T14:00:00-04:00", "wait_mins": 65},
        ]
        assert fs.find_forecast_for_hour(forecast, now) == 65

    def test_does_not_match_yesterday_same_hour(self):
        # Date match guard — overnight queries shouldn't pick up
        # yesterday's entry at the same clock hour.
        now = datetime(2026, 5, 25, 2, 30, tzinfo=_ET)
        forecast = [
            {"time": "2026-05-24T02:00:00-04:00", "wait_mins": 99},
        ]
        assert fs.find_forecast_for_hour(forecast, now) is None


class TestComputeParkLoadRatio:
    def test_simple_aggregate(self):
        now = datetime(2026, 5, 25, 14, 30, tzinfo=_ET)
        # Wait-weighted: (40 + 30 + 50) / (60 + 40 + 50) = 120/150 = 0.80
        attrs = [
            _attr(wait_mins=40, forecast_for_current_hour=60, now_et=now),
            _attr(wait_mins=30, forecast_for_current_hour=40, now_et=now),
            _attr(wait_mins=50, forecast_for_current_hour=50, now_et=now),
            _attr(wait_mins=20, forecast_for_current_hour=30, now_et=now),
            _attr(wait_mins=15, forecast_for_current_hour=20, now_et=now),
        ]
        # 40+30+50+20+15 = 155 ; 60+40+50+30+20 = 200 ; ratio = 0.775
        ratio, n = fs.compute_park_load_ratio(attrs, now)
        assert ratio == 0.775
        assert n == 5

    def test_excludes_non_operating(self):
        now = datetime(2026, 5, 25, 14, 30, tzinfo=_ET)
        attrs = [
            _attr(status="OPERATING", wait_mins=40, forecast_for_current_hour=60, now_et=now),
            _attr(status="DOWN", wait_mins=None, forecast_for_current_hour=60, now_et=now),
            _attr(status="CLOSED", wait_mins=None, forecast_for_current_hour=60, now_et=now),
        ]
        ratio, n = fs.compute_park_load_ratio(attrs, now)
        # Only the operating ride counts: 40/60
        assert n == 1
        assert ratio is not None and abs(ratio - 40 / 60) < 0.01

    def test_excludes_below_noise_floor(self):
        now = datetime(2026, 5, 25, 14, 30, tzinfo=_ET)
        # Predicted < 10 is noise — both excluded.
        attrs = [
            _attr(wait_mins=5, forecast_for_current_hour=5, now_et=now),
            _attr(wait_mins=8, forecast_for_current_hour=8, now_et=now),
        ]
        ratio, n = fs.compute_park_load_ratio(attrs, now)
        assert ratio is None
        assert n == 0

    def test_excludes_missing_forecast(self):
        now = datetime(2026, 5, 25, 14, 30, tzinfo=_ET)
        attrs = [
            _attr(wait_mins=40, forecast_for_current_hour=None, now_et=now),
            _attr(wait_mins=30, forecast_for_current_hour=50, now_et=now),
        ]
        ratio, n = fs.compute_park_load_ratio(attrs, now)
        assert n == 1
        assert ratio == round(30 / 50, 3)

    def test_returns_none_when_no_qualifying_rides(self):
        now = datetime(2026, 5, 25, 14, 30, tzinfo=_ET)
        attrs = [
            _attr(status="DOWN", wait_mins=None, forecast_for_current_hour=50, now_et=now),
        ]
        ratio, n = fs.compute_park_load_ratio(attrs, now)
        assert ratio is None
        assert n == 0


class TestShouldFireLowVsForecast:
    def test_killer_case_fires(self):
        # Heavy day (park_ratio=1.15), ride at 40 vs forecast 65,
        # ride_ratio = 0.615 < 0.75 * 1.15 = 0.8625, gap = 25 ≥ 15.
        # All gates pass.
        assert fs.should_fire_low_vs_forecast(
            current_wait=40, forecast_wait=65, park_ratio=1.15, rides_sampled=5
        )

    def test_quiet_day_suppressed(self):
        # park_ratio < MIN_PARK_RATIO blocks regardless of ride
        # signal strength. On a uniformly-quiet day everything is
        # below forecast → would spam.
        assert not fs.should_fire_low_vs_forecast(
            current_wait=40, forecast_wait=65, park_ratio=0.85, rides_sampled=5
        )

    def test_low_sample_size_suppressed(self):
        # park_ratio is real but n=3 < MIN_RIDES_SAMPLED. Early-
        # morning state where only a few rides are operating.
        assert not fs.should_fire_low_vs_forecast(
            current_wait=40, forecast_wait=65, park_ratio=1.15, rides_sampled=3
        )

    def test_park_ratio_none_suppressed(self):
        # Park has no qualifying rides at all — can't normalize, so
        # we don't fire.
        assert not fs.should_fire_low_vs_forecast(
            current_wait=40, forecast_wait=65, park_ratio=None, rides_sampled=0
        )

    def test_absolute_gap_too_small(self):
        # ride_ratio is fine but gap < MIN_ABS_GAP_MINS. A ride at
        # 55 vs forecast 65 is "5 min ahead of forecast" — not
        # alert-worthy as an opportunity push.
        assert not fs.should_fire_low_vs_forecast(
            current_wait=55, forecast_wait=65, park_ratio=1.15, rides_sampled=5
        )

    def test_ride_ratio_marginal(self):
        # park_ratio=1.0, threshold = 0.75; ride_ratio = 50/65 ≈ 0.77.
        # Just above the 25%-better-than-park bar — don't fire.
        # Also: absolute gap = 15 satisfies the gap floor; this
        # isolates the ratio gate.
        assert not fs.should_fire_low_vs_forecast(
            current_wait=50, forecast_wait=65, park_ratio=1.0, rides_sampled=5
        )

    def test_forecast_missing_suppressed(self):
        # No forecast for current hour — fall back to LOW_WAIT only.
        assert not fs.should_fire_low_vs_forecast(
            current_wait=40, forecast_wait=None, park_ratio=1.15, rides_sampled=5
        )

    def test_forecast_below_noise_floor(self):
        # Predicted under MIN_PREDICTED_WAIT — exclude even if the
        # ratio + gap look impressive on tiny numbers.
        assert not fs.should_fire_low_vs_forecast(
            current_wait=1, forecast_wait=8, park_ratio=1.15, rides_sampled=5
        )
