"""Tests for tools/aggregate-analytics.py.

Focus: _within_park_hours, the per-slice operating-window filter that
gates the active-minutes path (heatmap cells + downtime cells). A
timezone bug there silently blanked every pre-noon hour: the derived
bounds are UTC (`...+00:00`) but the active-minutes path tests an
ET-offset timestamp (`...-04:00`), and a raw ISO *string* compare
ranked a 9am-ET slice below a 9am-as-UTC open bound, dropping the
morning. These pin the timezone-aware-instant comparison so the
regression can't come back.

The module filename is hyphenated, so it's loaded via importlib.
"""

import importlib.util
import pathlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_MOD_PATH = pathlib.Path(__file__).resolve().parents[1] / "aggregate-analytics.py"
_spec = importlib.util.spec_from_file_location("aggregate_analytics", _MOD_PATH)
agg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agg)

EASTERN = ZoneInfo("America/New_York")


def _bounds_for(dt_et, open_utc, close_utc, park_id="MK"):
    """Build a park_hours dict keyed exactly like the aggregator does
    (by the derived park-day), with UTC string bounds like the real
    _derive_park_hours_ddb output."""
    return {(park_id, agg._park_day_iso(dt_et)): (open_utc, close_utc)}


def test_morning_et_slice_counts_against_utc_bounds():
    # Park open 9:00am ET (= 13:00 UTC), close 11:00pm ET (= 03:00 UTC next day).
    dt_et = datetime(2026, 3, 10, 10, 0, tzinfo=EASTERN)  # 10am ET — clearly open
    bounds = _bounds_for(dt_et, "2026-03-10T13:00:00+00:00", "2026-03-11T03:00:00+00:00")
    # The slice's ISO string is "...T10:00:00-04:00", which a raw string
    # compare ranks BELOW the "...T13:...+00:00" open bound. The fix accepts it.
    assert agg._within_park_hours(bounds, "MK", dt_et, dt_et.isoformat()) is True


def test_pre_open_hour_is_excluded():
    dt_et = datetime(2026, 3, 10, 6, 0, tzinfo=EASTERN)  # 6am ET (= 10:00 UTC) — before open
    bounds = _bounds_for(dt_et, "2026-03-10T13:00:00+00:00", "2026-03-11T03:00:00+00:00")
    assert agg._within_park_hours(bounds, "MK", dt_et, dt_et.isoformat()) is False


def test_late_evening_hour_within_window():
    dt_et = datetime(2026, 3, 10, 21, 0, tzinfo=EASTERN)  # 9pm ET (= 01:00 UTC next day)
    bounds = _bounds_for(dt_et, "2026-03-10T13:00:00+00:00", "2026-03-11T03:00:00+00:00")
    assert agg._within_park_hours(bounds, "MK", dt_et, dt_et.isoformat()) is True


def test_no_bounds_for_park_day_returns_false():
    dt_et = datetime(2026, 3, 10, 10, 0, tzinfo=EASTERN)
    assert agg._within_park_hours({}, "MK", dt_et, dt_et.isoformat()) is False


def test_as_instant_handles_naive_and_aware():
    aware = agg._as_instant("2026-03-10T13:00:00+00:00")
    naive = agg._as_instant("2026-03-10T13:00:00")  # assumed UTC
    assert aware.tzinfo is not None and naive.tzinfo is not None
    assert aware == naive  # same instant once naive is treated as UTC


def test_utc_and_et_strings_for_same_instant_compare_equal_via_as_instant():
    # 13:00 UTC and 09:00-04:00 are the SAME moment — string compare would
    # call them different; _as_instant must not.
    assert agg._as_instant("2026-03-10T13:00:00+00:00") == agg._as_instant("2026-03-10T09:00:00-04:00")
