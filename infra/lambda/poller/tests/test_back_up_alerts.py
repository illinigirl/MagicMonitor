"""BACK UP (ride recovery) alerting.

Recoveries are the time-critical half of the down/up pair: missing
"it's down" costs an annoyance, missing "it's back" costs the ride,
because the queue rebuilds fast. Prior to 2026-08-27 the recovery
alert had three independent ways to get silently dropped, all of them
absent from the DOWN path:

  1. **Intermediate status.** The guard was
     `new_status == "OPERATING" and old_status == "DOWN"`. A recovery
     routed through any other state — DOWN → CLOSED → OPERATING on a
     weather hold, DOWN → REFURBISHMENT → OPERATING — never alerted.
     The DOWN branch fires from *any* prior status, so downs were
     never lost this way.
  2. **Closing buffer.** Both paths shared a cutoff of
     `close - CLOSING_BUFFER_MINS`. Since a recovery always trails its
     own outage, any outage straddling that cutoff delivered the DOWN
     and swallowed the UP.
  3. **Pushover priority.** DOWN sent at priority 1 (bypasses quiet
     hours), UP at priority 0 (held by them).

Widening (1) introduces a hazard of its own — a stale DOWN_SINCE
marker firing a phantom recovery the next morning — so the park-day
guard and the DOWN_SINCE cleanup are covered here too.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import index
import notifier

ET = ZoneInfo("America/New_York")
RIDE_ID = "tot"
PARK_KEY = "hollywood_studios"


def _attr(status, wait_mins=None):
    return {
        "id": RIDE_ID,
        "name": "Tower of Terror",
        "status": status,
        "wait_mins": wait_mins,
        "park_name": "Hollywood Studios",
        "park_key": PARK_KEY,
        "last_seen": "2026-08-26T22:00:00+00:00",
        "ll": None,
    }


def _hours_closing_in(minutes):
    """Park hours anchored to the real clock, so these tests are
    deterministic whatever time CI runs them."""
    now = datetime.now(ET)
    return (now - timedelta(hours=8), now + timedelta(minutes=minutes))


class _Poll:
    """Drives index.handler() for one ride in one park and records
    which notifier calls came out the other side."""

    def __init__(self, monkeypatch, *, old_status, new_status,
                 down_since=None, minutes_to_close=240,
                 back_up_on_cooldown=False, wait_mins=25):
        self.up_calls = []
        self.down_calls = []
        self.plan_calls = []
        self.cleared = []
        self.down_since_writes = []
        self._down_since = down_since

        mp = monkeypatch
        mp.setattr(index.wait_times, "fetch_live_data",
                   lambda pk: [_attr(new_status, wait_mins)] if pk == PARK_KEY else [])
        mp.setattr(index.wait_times, "fetch_park_hours",
                   lambda pk: _hours_closing_in(minutes_to_close))
        mp.setattr(index.db, "get_ride", lambda rid: {"status": old_status})
        mp.setattr(index.db, "upsert_ride", lambda attr: None)
        mp.setattr(index.db, "record_status_change", lambda **kw: None)
        mp.setattr(index.db, "record_wait_observation", lambda **kw: None)
        mp.setattr(index.db, "build_active_plan_ride_index",
                   lambda date_iso, now_et=None: ({}, []))
        mp.setattr(index.db, "lookup_plan_targets", lambda idx, rid, name: [])
        mp.setattr(index.db, "get_park_subscribers",
                   lambda pk: ["megan"] if pk == PARK_KEY else [])
        mp.setattr(index.db, "get_user_favorites_for_park",
                   lambda uid, pk: {RIDE_ID})
        mp.setattr(index.db, "get_user_ll_watched_rides", lambda uid, pk: set())
        mp.setattr(index.db, "get_user_profile",
                   lambda uid: {"pushover_user_key": "pushover-key"})
        mp.setattr(index, "_historically_interesting", lambda rid: False)

        mp.setattr(index.db, "get_down_since", lambda rid: self._down_since)
        mp.setattr(index.db, "clear_down_since",
                   lambda rid: self.cleared.append(rid))
        mp.setattr(index.db, "set_down_since",
                   lambda rid, when: self.down_since_writes.append(when))

        mp.setattr(index.db, "is_back_up_alert_on_cooldown",
                   lambda rid: back_up_on_cooldown)
        mp.setattr(index.db, "mark_back_up_alert_sent", lambda rid: None)
        mp.setattr(index.db, "is_down_alert_on_cooldown", lambda rid: False)
        mp.setattr(index.db, "mark_down_alert_sent", lambda rid: None)

        mp.setattr(index.notifier, "alert_ride_up",
                   lambda key, **kw: self.up_calls.append(kw) or True)
        mp.setattr(index.notifier, "alert_ride_down",
                   lambda key, **kw: self.down_calls.append(kw) or True)
        mp.setattr(index.notifier, "alert_plan_disruption",
                   lambda key, **kw: self.plan_calls.append(kw) or True)

    def run(self):
        return index.handler({}, None)


def _minutes_ago(mins):
    return datetime.now(timezone.utc) - timedelta(minutes=mins)


# ── 1. The recovery guard ────────────────────────────────────────────

class TestRecoveryReachesTheRider:
    """The guard is 'we hold a DOWN_SINCE marker', not
    'old_status == DOWN'."""

    def test_plain_down_to_operating_alerts(self, monkeypatch):
        poll = _Poll(monkeypatch, old_status="DOWN", new_status="OPERATING",
                     down_since=_minutes_ago(40))
        poll.run()
        assert len(poll.up_calls) == 1
        assert poll.up_calls[0]["ride_name"] == "Tower of Terror"

    def test_recovery_through_closed_alerts(self, monkeypatch):
        """DOWN → CLOSED → OPERATING (afternoon weather hold). This is
        the poll where CLOSED → OPERATING lands: old_status is CLOSED,
        but the ride still owes us a recovery. Fails on the old
        exact-match guard."""
        poll = _Poll(monkeypatch, old_status="CLOSED", new_status="OPERATING",
                     down_since=_minutes_ago(40))
        poll.run()
        assert len(poll.up_calls) == 1

    def test_recovery_through_refurbishment_alerts(self, monkeypatch):
        poll = _Poll(monkeypatch, old_status="REFURBISHMENT",
                     new_status="OPERATING", down_since=_minutes_ago(40))
        poll.run()
        assert len(poll.up_calls) == 1

    def test_ordinary_park_open_does_not_alert(self, monkeypatch):
        """CLOSED → OPERATING with no marker is the park opening, not a
        recovery. The widened guard must not turn every morning into a
        'back up' push."""
        poll = _Poll(monkeypatch, old_status="CLOSED", new_status="OPERATING",
                     down_since=None)
        poll.run()
        assert poll.up_calls == []

    def test_reports_actual_downtime(self, monkeypatch):
        poll = _Poll(monkeypatch, old_status="DOWN", new_status="OPERATING",
                     down_since=_minutes_ago(52))
        poll.run()
        assert poll.up_calls[0]["actual_downtime_mins"] == 52

    def test_cooldown_still_suppresses(self, monkeypatch):
        poll = _Poll(monkeypatch, old_status="DOWN", new_status="OPERATING",
                     down_since=_minutes_ago(40), back_up_on_cooldown=True)
        poll.run()
        assert poll.up_calls == []


# ── 2. The closing buffer ────────────────────────────────────────────

class TestClosingBuffer:
    """A recovery inside the last CLOSING_BUFFER_MINS is still
    rideable — guests ride to close and past it as the queue drains."""

    def test_recovery_alerts_inside_the_closing_buffer(self, monkeypatch):
        """Park closes in 10 min, buffer is 30 — the old shared cutoff
        suppressed this. It must now go out."""
        poll = _Poll(monkeypatch, old_status="DOWN", new_status="OPERATING",
                     down_since=_minutes_ago(40), minutes_to_close=10)
        poll.run()
        assert len(poll.up_calls) == 1

    def test_recovery_after_close_is_still_suppressed(self, monkeypatch):
        """Dropping the buffer must not mean alerting all night."""
        poll = _Poll(monkeypatch, old_status="DOWN", new_status="OPERATING",
                     down_since=_minutes_ago(40), minutes_to_close=-5)
        poll.run()
        assert poll.up_calls == []

    def test_down_alert_keeps_the_buffer(self, monkeypatch):
        """The buffer exists to mute the closing-time DOWN wave. That
        reasoning is specific to bad news and must survive."""
        poll = _Poll(monkeypatch, old_status="OPERATING", new_status="DOWN",
                     minutes_to_close=10, wait_mins=None)
        poll.run()
        assert poll.down_calls == []


# ── 3. Stale-marker hazard from widening the guard ───────────────────

class TestStaleMarker:
    def test_previous_park_day_marker_does_not_alert(self, monkeypatch):
        """Went down at 8pm, closed while down, reopens next morning.
        Not a recovery anyone wants pushed — and it's exactly the shape
        of every DOWN_SINCE row the old code orphaned."""
        poll = _Poll(monkeypatch, old_status="CLOSED", new_status="OPERATING",
                     down_since=datetime.now(timezone.utc) - timedelta(hours=30))
        poll.run()
        assert poll.up_calls == []

    def test_stale_marker_is_cleared_even_though_it_did_not_alert(self, monkeypatch):
        poll = _Poll(monkeypatch, old_status="CLOSED", new_status="OPERATING",
                     down_since=datetime.now(timezone.utc) - timedelta(hours=30))
        poll.run()
        assert poll.cleared == [RIDE_ID]

    def test_leaving_down_without_recovering_clears_the_marker(self, monkeypatch):
        """DOWN → CLOSED at park close. No alert (it didn't recover),
        but the marker must not leak — that leak is what made stale
        markers possible in the first place."""
        poll = _Poll(monkeypatch, old_status="DOWN", new_status="CLOSED",
                     down_since=_minutes_ago(40), wait_mins=None)
        poll.run()
        assert poll.up_calls == []
        assert poll.cleared == [RIDE_ID]

    def test_suppressed_recovery_still_clears_the_marker(self, monkeypatch):
        """A ride whose alert was cooldown-suppressed must not stay
        permanently 'owing' a recovery."""
        poll = _Poll(monkeypatch, old_status="DOWN", new_status="OPERATING",
                     down_since=_minutes_ago(40), back_up_on_cooldown=True)
        poll.run()
        assert poll.cleared == [RIDE_ID]


# ── 4. Pure helpers ──────────────────────────────────────────────────

class TestParkDay:
    """4am ET rollover, matching the analytics park-day convention."""

    def test_late_night_belongs_to_the_previous_park_day(self):
        assert index._park_day(
            datetime(2026, 8, 27, 1, 30, tzinfo=ET)
        ).isoformat() == "2026-08-26"

    def test_just_before_4am_is_previous_park_day(self):
        assert index._park_day(
            datetime(2026, 8, 27, 3, 59, tzinfo=ET)
        ).isoformat() == "2026-08-26"

    def test_4am_starts_the_new_park_day(self):
        assert index._park_day(
            datetime(2026, 8, 27, 4, 0, tzinfo=ET)
        ).isoformat() == "2026-08-27"

    def test_utc_input_is_converted_before_bucketing(self):
        # 05:00 UTC on the 27th is 01:00 ET — previous park-day.
        assert index._park_day(
            datetime(2026, 8, 27, 5, 0, tzinfo=timezone.utc)
        ).isoformat() == "2026-08-26"

    def test_long_same_day_outage_is_same_park_day(self):
        down = datetime(2026, 8, 26, 10, 0, tzinfo=ET)
        up = datetime(2026, 8, 26, 20, 30, tzinfo=ET)
        assert index._same_park_day(down, up) is True

    def test_outage_spanning_midnight_is_same_park_day(self):
        """Park-day runs to 4am, so a 11pm → 12:30am recovery counts."""
        down = datetime(2026, 8, 26, 23, 0, tzinfo=ET)
        up = datetime(2026, 8, 27, 0, 30, tzinfo=ET)
        assert index._same_park_day(down, up) is True

    def test_overnight_close_is_not_same_park_day(self):
        down = datetime(2026, 8, 26, 20, 0, tzinfo=ET)
        up = datetime(2026, 8, 27, 9, 0, tzinfo=ET)
        assert index._same_park_day(down, up) is False


class TestAlertWindow:
    def _hours(self):
        return (datetime(2026, 8, 26, 9, 0, tzinfo=ET),
                datetime(2026, 8, 26, 21, 0, tzinfo=ET))

    def test_buffer_applies_by_default(self):
        open_dt, close_dt = self._hours()
        assert index._alert_cutoff(
            close_dt, ignore_closing_buffer=False
        ) == datetime(2026, 8, 26, 20, 30, tzinfo=ET)

    def test_buffer_dropped_runs_to_close(self):
        open_dt, close_dt = self._hours()
        assert index._alert_cutoff(
            close_dt, ignore_closing_buffer=True
        ) == close_dt

    def test_recovery_window_covers_the_buffered_gap(self):
        """20:45 is inside the gap the two cutoffs disagree about —
        this is the window that was losing recoveries."""
        open_dt, close_dt = self._hours()
        now = datetime(2026, 8, 26, 20, 45, tzinfo=ET)
        assert index._within_alert_window(
            open_dt, close_dt, now, ignore_closing_buffer=False) is False
        assert index._within_alert_window(
            open_dt, close_dt, now, ignore_closing_buffer=True) is True

    def test_neither_window_covers_after_close(self):
        open_dt, close_dt = self._hours()
        now = datetime(2026, 8, 26, 21, 30, tzinfo=ET)
        assert index._within_alert_window(
            open_dt, close_dt, now, ignore_closing_buffer=True) is False

    def test_neither_window_covers_before_open(self):
        open_dt, close_dt = self._hours()
        now = datetime(2026, 8, 26, 7, 0, tzinfo=ET)
        assert index._within_alert_window(
            open_dt, close_dt, now, ignore_closing_buffer=True) is False


# ── 5. Pushover priority ─────────────────────────────────────────────

class _FakeResp:
    def raise_for_status(self):
        pass


class TestRecoveryPriority:
    """Recoveries must bypass a recipient's quiet hours / Focus mode.
    At priority 0 they were held while the priority-1 DOWN alerts
    punched through — the alert that mattered least always arrived and
    the one that mattered most never did."""

    def _capture(self, monkeypatch):
        monkeypatch.setattr(notifier, "_get_app_token", lambda: "tok")
        sent = {}
        monkeypatch.setattr(
            notifier.requests, "post",
            lambda url, data=None, timeout=None: (sent.update(data) or _FakeResp()),
        )
        return sent

    def test_ride_up_is_high_priority(self, monkeypatch):
        sent = self._capture(monkeypatch)
        notifier.alert_ride_up(
            "key", ride_name="Tower of Terror", park_name="Hollywood Studios",
            park_key="hollywood_studios", wait_mins=25, actual_downtime_mins=40,
        )
        assert sent["priority"] == 1

    def test_plan_back_up_is_high_priority(self, monkeypatch):
        sent = self._capture(monkeypatch)
        notifier.alert_plan_disruption(
            "key", ride_name="Tower of Terror", park_name="Hollywood Studios",
            park_key="hollywood_studios", disruption_type="back_up",
            plan_id="p1", wait_mins=25,
        )
        assert sent["priority"] == 1

    def test_still_down_stays_normal_priority(self, monkeypatch):
        """Only the two ride-state edges are urgent; the 45-min
        'still down' nag is informational and stays at 0."""
        sent = self._capture(monkeypatch)
        notifier.alert_still_down(
            "key", ride_name="Tower of Terror", park_name="Hollywood Studios",
            park_key="hollywood_studios", minutes_down=45,
        )
        assert sent["priority"] == 0


class TestLostMarkerFallback:
    """A DOWN_SINCE write that never landed (DDB blip, cold-start
    failure) must not cost the rider the recovery push. Pre-2026-08-27
    this case alerted with actual_downtime_mins=None; widening the
    guard must not quietly turn it into a dropped alert."""

    def test_down_to_operating_without_marker_still_alerts(self, monkeypatch):
        poll = _Poll(monkeypatch, old_status="DOWN", new_status="OPERATING",
                     down_since=None)
        poll.run()
        assert len(poll.up_calls) == 1
        assert poll.up_calls[0]["actual_downtime_mins"] is None

    def test_no_marker_and_no_down_history_stays_silent(self, monkeypatch):
        """The fallback is scoped to old_status == DOWN — it must not
        make every CLOSED → OPERATING park open into a recovery."""
        poll = _Poll(monkeypatch, old_status="CLOSED", new_status="OPERATING",
                     down_since=None)
        poll.run()
        assert poll.up_calls == []
