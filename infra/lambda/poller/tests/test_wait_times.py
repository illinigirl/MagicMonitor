"""Tests for fetch_park_hours window selection in wait_times.py.

Focus: the 2026-06-11 fix for after-midnight operating hours. themeparks
keys a park-day that closes past midnight (e.g. a 1am close for a party /
extended evening) to its OPENING date — so just after midnight the
in-progress window lives under YESTERDAY's schedule entry. The old code
filtered to today's date only, so it selected the (not-yet-open) new day
and suppressed alerts while the park was genuinely open.

requests.get is stubbed (no network) and datetime is frozen so "now" is
deterministic.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import wait_times

_ET = ZoneInfo("America/New_York")


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _patch(monkeypatch, schedule, now_et):
    monkeypatch.setattr(
        wait_times.requests, "get", lambda *a, **k: _FakeResp({"schedule": schedule})
    )

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return now_et.astimezone(tz) if tz is not None else now_et.replace(tzinfo=None)

    monkeypatch.setattr(wait_times, "datetime", _Frozen)


# A park-day that opens 6/10 09:00 and closes 6/11 01:00 (after midnight),
# keyed by its opening date, plus the next day's normal 09:00–22:00.
_LATE_CLOSE_SCHEDULE = [
    {"date": "2026-06-10", "type": "OPERATING",
     "openingTime": "2026-06-10T09:00:00-04:00",
     "closingTime": "2026-06-11T01:00:00-04:00"},
    {"date": "2026-06-11", "type": "OPERATING",
     "openingTime": "2026-06-11T09:00:00-04:00",
     "closingTime": "2026-06-11T22:00:00-04:00"},
]


def test_after_midnight_selects_the_in_progress_window(monkeypatch):
    # 00:30 ET on 6/11 — still inside the 6/10 park-day (closes 01:00).
    now = datetime(2026, 6, 11, 0, 30, tzinfo=_ET)
    _patch(monkeypatch, _LATE_CLOSE_SCHEDULE, now)
    hours = wait_times.fetch_park_hours("magic_kingdom")
    assert hours is not None
    open_dt, close_dt = hours
    # The window that actually contains "now" — yesterday's late-close one.
    assert open_dt <= now <= close_dt
    assert close_dt.hour == 1  # closes 01:00, not 22:00


def test_daytime_selects_todays_window(monkeypatch):
    now = datetime(2026, 6, 11, 14, 0, tzinfo=_ET)
    _patch(monkeypatch, _LATE_CLOSE_SCHEDULE, now)
    open_dt, close_dt = wait_times.fetch_park_hours("magic_kingdom")
    assert open_dt.day == 11 and open_dt.hour == 9
    assert close_dt.day == 11 and close_dt.hour == 22


def test_before_open_reports_todays_hours(monkeypatch):
    # 07:00 ET, before the 09:00 open — returns today's window so the
    # caller's open<=now<=close check correctly reads "not open yet".
    now = datetime(2026, 6, 11, 7, 0, tzinfo=_ET)
    _patch(monkeypatch, _LATE_CLOSE_SCHEDULE, now)
    open_dt, close_dt = wait_times.fetch_park_hours("magic_kingdom")
    assert open_dt.day == 11 and open_dt.hour == 9
    assert not (open_dt <= now <= close_dt)


def test_no_entry_today_returns_none(monkeypatch):
    # Park closed today (no entry) → None → caller fails open. The lone
    # late-close-from-yesterday entry doesn't contain a midday "now".
    now = datetime(2026, 6, 12, 14, 0, tzinfo=_ET)
    _patch(monkeypatch, _LATE_CLOSE_SCHEDULE, now)
    assert wait_times.fetch_park_hours("magic_kingdom") is None
