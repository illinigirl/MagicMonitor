"""Tests for `_park_day_window_utc` in mcp/_tool_impls.py — the pure
function that maps days_back to a [start, end] UTC window covering one
4am-ET-to-4am-ET park-day. It backs get_ride_downtime_today.

The bug these pin (2026-06-11): the function never shifted the anchor
date before the 4am boundary, so between midnight and 4am ET, days_back=0
built a window entirely in the FUTURE — the downtime query returned
nothing and the tool reported "0 down today" after an evening of
breakdowns. The current park-day was only reachable as days_back=1,
mislabeled "yesterday".

The function calls datetime.now(_EASTERN) internally, so we freeze it by
swapping _tool_impls.datetime for a subclass with a pinned now().
"""

from datetime import datetime, timezone

import _tool_impls
from _tool_impls import _EASTERN


def _freeze_et(monkeypatch, y, mo, d, h, mi=0):
    """Pin datetime.now(_EASTERN) to a fixed ET wall-clock instant."""
    fixed = datetime(y, mo, d, h, mi, tzinfo=_EASTERN)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz is not None else fixed.replace(tzinfo=None)

    monkeypatch.setattr(_tool_impls, "datetime", _Frozen)


def test_before_4am_today_window_is_the_in_progress_park_day(monkeypatch):
    # 2am ET on June 10 → "today" (days_back=0) must be the park-day that
    # OPENED June 9 at 4am ET and runs until June 10 4am ET — i.e. the
    # window we're currently inside, NOT a future June-10 4am→June-11 4am.
    _freeze_et(monkeypatch, 2026, 6, 10, 2)
    start, end, label = _tool_impls._park_day_window_utc(0)

    assert label == "2026-06-09"
    # 4am ET == 08:00 UTC (EDT, UTC-4).
    assert start == datetime(2026, 6, 9, 8, 0, tzinfo=timezone.utc)
    assert end.astimezone(timezone.utc).hour == 7  # one microsecond before 8am UTC
    assert end.date().isoformat() == "2026-06-10"

    # The frozen "now" (2am ET June 10 == 06:00 UTC) falls INSIDE the
    # window — the whole point: the query can actually match today's rows.
    now_utc = datetime(2026, 6, 10, 6, 0, tzinfo=timezone.utc)
    assert start <= now_utc <= end


def test_after_4am_today_window_is_the_calendar_day(monkeypatch):
    # 5pm ET on June 10 → "today" is the June 10 4am→June 11 4am park-day.
    _freeze_et(monkeypatch, 2026, 6, 10, 17)
    start, _, label = _tool_impls._park_day_window_utc(0)
    assert label == "2026-06-10"
    assert start == datetime(2026, 6, 10, 8, 0, tzinfo=timezone.utc)


def test_days_back_counts_park_days_not_calendar_days(monkeypatch):
    # At 2am ET June 10, days_back=1 ("yesterday") is the park-day before
    # the in-progress one: June 8 4am → June 9 4am.
    _freeze_et(monkeypatch, 2026, 6, 10, 2)
    _, _, label = _tool_impls._park_day_window_utc(1)
    assert label == "2026-06-08"


def test_just_after_boundary_does_not_shift(monkeypatch):
    # 4:00am ET is the start of the new park-day — no shift.
    _freeze_et(monkeypatch, 2026, 6, 10, 4)
    _, _, label = _tool_impls._park_day_window_utc(0)
    assert label == "2026-06-10"
