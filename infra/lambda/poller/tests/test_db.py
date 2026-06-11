"""
Tests for db.py — cooldown helpers and the weather-snapshot round-trip.

Stubs the DynamoDB Table boto3 returns by monkeypatching the module's
_table reference before exercising the helpers. This is lighter than
running moto and keeps tests fast + dependency-free.

What we're verifying:
  - Cooldowns set + read symmetrically (no false negatives on read)
  - Different (user, plan) cooldown keys don't collide
  - The weather snapshot round-trips through put + get
  - The active-plan summary view is correctly extracted alongside
    the ride-index view from a single scan (the 2026-05-12 refactor)
"""

import time
from datetime import datetime, timedelta, timezone

import db


class _StubTable:
    """In-memory stand-in for the boto3 DDB Table resource. Captures
    just enough behavior — get_item, put_item, delete_item, scan —
    to exercise the helpers under test. TTL is honored advisorily
    only; tests don't simulate clock passage."""

    def __init__(self):
        self.items: dict[tuple, dict] = {}

    def get_item(self, Key):
        k = (Key["PK"], Key["SK"])
        if k in self.items:
            return {"Item": self.items[k]}
        return {}

    def put_item(self, Item):
        k = (Item["PK"], Item["SK"])
        self.items[k] = Item

    def delete_item(self, Key):
        self.items.pop((Key["PK"], Key["SK"]), None)

    def scan(self, **kwargs):
        # Minimal scan implementation: returns all items whose
        # PK/SK match the FilterExpression begins_with patterns.
        # Doesn't actually parse the FilterExpression — the tests
        # that exercise scan are restricted to known fixture data.
        return {"Items": list(self.items.values())}

    def query(self, **kwargs):
        # Minimal GSI query stub. Ignores IndexName / KeyConditionExpression
        # / FilterExpression — the production code re-applies every gate
        # (date, active, outcome_recorded, window) in Python, so the tests
        # exercise that logic against known fixtures. Optionally paginates
        # via self.page_size so a test can drive the ExclusiveStartKey loop.
        all_items = list(self.items.values())
        start = 0
        esk = kwargs.get("ExclusiveStartKey")
        if esk is not None:
            keys = [(it["PK"], it["SK"]) for it in all_items]
            marker = (esk["PK"], esk["SK"])
            start = keys.index(marker) + 1 if marker in keys else 0
        page_size = getattr(self, "page_size", None)
        if page_size is None:
            return {"Items": all_items[start:]}
        end = start + page_size
        resp = {"Items": all_items[start:end]}
        if end < len(all_items):
            last = all_items[end - 1]
            resp["LastEvaluatedKey"] = {"PK": last["PK"], "SK": last["SK"]}
        return resp


def _swap_in_stub(stub):
    """Replace db._table with the provided stub for the test's scope."""
    db._table = stub


# ── Weather snapshot round-trip ──

class TestWeatherSnapshot:
    def setup_method(self):
        self.stub = _StubTable()
        _swap_in_stub(self.stub)

    def test_initial_snapshot_is_none(self):
        """Fresh table → no prior snapshot → returns None.
        This is the case the storm-shift detector handles as 'no prior,
        any storm is new'."""
        assert db.get_prior_weather_snapshot() is None

    def test_put_and_get_round_trip(self):
        """Snapshot survives the round trip — payload preserved exactly."""
        snapshot = {
            "fetched_at": "2026-05-12T14:30:00+00:00",
            "current": {"temp_f": 82, "weather_code": 1},
            "next_6h": [{"time": "2026-05-12T15:00", "weather_code": 95}],
        }
        db.put_weather_snapshot(snapshot)
        assert db.get_prior_weather_snapshot() == snapshot


# ── Weather alert cooldown (per user, per plan) ──

class TestWeatherCooldown:
    def setup_method(self):
        self.stub = _StubTable()
        _swap_in_stub(self.stub)

    def test_no_cooldown_initially(self):
        assert db.is_weather_alert_on_cooldown("megan", "PLAN-1") is False

    def test_cooldown_set_after_mark(self):
        db.mark_weather_alert_sent("megan", "PLAN-1")
        assert db.is_weather_alert_on_cooldown("megan", "PLAN-1") is True

    def test_different_plan_id_independent(self):
        """Per-(user, plan) cooldown means a second plan isn't gated by
        the first. Matches the design — two distinct plans can each
        legitimately receive a weather-shift alert."""
        db.mark_weather_alert_sent("megan", "PLAN-1")
        assert db.is_weather_alert_on_cooldown("megan", "PLAN-2") is False

    def test_different_user_independent(self):
        """Mark on one user's plan doesn't affect another user."""
        db.mark_weather_alert_sent("megan", "PLAN-1")
        assert db.is_weather_alert_on_cooldown("mark", "PLAN-1") is False


# ── Per-ride DOWN cooldown ──

class TestDownCooldown:
    """The original cooldown pattern; verifies the cooldowns predating
    the 2026-05-11 BACK_UP fix still work as designed."""

    def setup_method(self):
        self.stub = _StubTable()
        _swap_in_stub(self.stub)

    def test_no_down_cooldown_initially(self):
        assert db.is_down_alert_on_cooldown("ride-123") is False

    def test_down_cooldown_set_after_mark(self):
        db.mark_down_alert_sent("ride-123")
        assert db.is_down_alert_on_cooldown("ride-123") is True

    def test_down_cooldown_is_per_ride(self):
        db.mark_down_alert_sent("ride-123")
        assert db.is_down_alert_on_cooldown("ride-456") is False


# ── Cooldown TTL expiry — the 2026-06-11 fix ──

class TestCooldownTtlExpiry:
    """DynamoDB's TTL reaper is best-effort: an expired row can linger and
    still be returned by GetItem. The cooldown check must compare ttl to
    now, not just presence — otherwise a 15-min cooldown silently stretches
    to however long the reaper lags, suppressing a later distinct alert."""

    def setup_method(self):
        self.stub = _StubTable()
        _swap_in_stub(self.stub)

    def test_expired_but_present_row_reads_as_inactive(self):
        # Simulate an expired-but-undeleted cooldown row (ttl in the past).
        self.stub.put_item(Item={
            "PK": "RIDE#ride-123", "SK": "COOLDOWN#DOWN",
            "sent_at": "2026-06-11T00:00:00+00:00",
            "ttl": int(time.time()) - 60,  # expired a minute ago
        })
        assert db.is_down_alert_on_cooldown("ride-123") is False

    def test_unexpired_row_reads_as_active(self):
        self.stub.put_item(Item={
            "PK": "RIDE#ride-123", "SK": "COOLDOWN#DOWN",
            "sent_at": "2026-06-11T00:00:00+00:00",
            "ttl": int(time.time()) + 600,
        })
        assert db.is_down_alert_on_cooldown("ride-123") is True

    def test_legacy_row_without_ttl_falls_back_to_presence(self):
        self.stub.put_item(Item={"PK": "RIDE#r", "SK": "COOLDOWN#DOWN"})
        assert db.is_down_alert_on_cooldown("r") is True

    def test_still_down_helpers_roundtrip_and_expire(self):
        assert db.is_still_down_alert_on_cooldown("r") is False
        db.mark_still_down_alert_sent("r", 2700)
        assert db.is_still_down_alert_on_cooldown("r") is True
        # An expired still-down row no longer suppresses the second alert.
        self.stub.put_item(Item={
            "PK": "RIDE#r", "SK": "COOLDOWN#STILL_DOWN",
            "ttl": int(time.time()) - 1,
        })
        assert db.is_still_down_alert_on_cooldown("r") is False


# ── BACK_UP cooldown — the 2026-05-11 fix ──

class TestBackUpCooldown:
    """The BACK_UP cooldown closes the bug where flapping rides
    generated one DOWN alert + N BACK UP pings. Test ensures the
    same shape as DOWN cooldown without collision."""

    def setup_method(self):
        self.stub = _StubTable()
        _swap_in_stub(self.stub)

    def test_back_up_cooldown_independent_of_down(self):
        """Marking DOWN cooldown doesn't gate the BACK_UP alert, and
        vice versa — they're separate SK rows."""
        db.mark_down_alert_sent("ride-123")
        assert db.is_back_up_alert_on_cooldown("ride-123") is False

    def test_back_up_set_after_mark(self):
        db.mark_back_up_alert_sent("ride-123")
        assert db.is_back_up_alert_on_cooldown("ride-123") is True


# ── Low-wait cooldown ──

# ── Raw wait observations (M6-B Phase 1) ──────────────────────────

class TestWaitObservations:
    """The M6-B Phase 1 data-collection path. Mirrors the Pi pattern
    in DDB: one row per (operating ride, poll). Aggregator will
    eventually source from these rows for the analytics snapshot."""

    def setup_method(self):
        self.stub = _StubTable()
        _swap_in_stub(self.stub)

    def test_writes_row_with_expected_shape(self):
        """Verify the row shape the aggregator will eventually read."""
        db.record_wait_observation(
            ride_id="big-thunder",
            park_key="magic_kingdom",
            wait_mins=45,
            polled_at="2026-05-17T14:30:00+00:00",
        )
        key = ("RIDE#big-thunder", "WAIT#2026-05-17T14:30:00+00:00")
        assert key in self.stub.items
        row = self.stub.items[key]
        assert row["wait_mins"] == 45
        assert row["park_key"] == "magic_kingdom"
        assert row["polled_at"] == "2026-05-17T14:30:00+00:00"
        # TTL must be set — bounds storage growth.
        assert "ttl" in row
        assert isinstance(row["ttl"], int)
        assert row["ttl"] > 0

    def test_multiple_polls_create_distinct_rows(self):
        """Two polls of the same ride at different timestamps must
        produce two distinct WAIT# rows, not overwrite each other.
        This is the whole point of the per-poll collection pattern —
        every observation preserved for the aggregator."""
        db.record_wait_observation(
            ride_id="big-thunder",
            park_key="magic_kingdom",
            wait_mins=45,
            polled_at="2026-05-17T14:30:00+00:00",
        )
        db.record_wait_observation(
            ride_id="big-thunder",
            park_key="magic_kingdom",
            wait_mins=55,
            polled_at="2026-05-17T14:32:00+00:00",
        )
        # Two distinct SKs under the same PK.
        bt_rows = [
            row for (pk, sk), row in self.stub.items.items()
            if pk == "RIDE#big-thunder" and sk.startswith("WAIT#")
        ]
        assert len(bt_rows) == 2
        assert {r["wait_mins"] for r in bt_rows} == {45, 55}


class TestLowWaitCooldown:
    def setup_method(self):
        self.stub = _StubTable()
        _swap_in_stub(self.stub)

    def test_independent_from_down_cooldowns(self):
        """Low-wait fires on operating polls, not transitions — its
        cooldown shouldn't share state with the transition cooldowns."""
        db.mark_down_alert_sent("ride-123")
        db.mark_back_up_alert_sent("ride-123")
        assert db.is_low_wait_alert_on_cooldown("ride-123") is False

    def test_set_after_mark(self):
        db.mark_low_wait_alert_sent("ride-123")
        assert db.is_low_wait_alert_on_cooldown("ride-123") is True


# ── M5 activation + plan-window gating in build_active_plan_ride_index ──

class TestActivePlanGating:
    """The activation + plan-window gates, plus the date / outcome filters
    the GSI query relies on. The stub query() returns all items without
    parsing the Key/Filter expressions, so these fixtures rely on the
    in-Python guards (which mirror the DDB Key + FilterExpression) for the
    date / active / outcome / window behavior."""

    def setup_method(self):
        self.stub = _StubTable()
        _swap_in_stub(self.stub)

    def _put_plan(self, plan_id, *, active=None, plan_window=None,
                  ride_id="r1", ride_name="Space Mountain", pfd="2026-06-23",
                  outcome_recorded=False):
        item = {
            "PK": "USER#megan",
            "SK": f"PLAN#{plan_id}",
            "planned_for_date": pfd,
            "outcome_recorded": outcome_recorded,
            "park_key": "magic_kingdom",
            "ride_sequence": [{"ride_id": ride_id, "ride_name": ride_name}],
        }
        if active is not None:
            item["active"] = active
        if plan_window is not None:
            item["plan_window"] = plan_window
        self.stub.put_item(item)

    def test_active_plan_included(self):
        self._put_plan("p1", active=True)
        index, active_plans = db.build_active_plan_ride_index("2026-06-23")
        assert ("megan", "p1") in index.get("r1", [])
        assert any(p["plan_id"] == "p1" for p in active_plans)

    def test_dormant_plan_excluded(self):
        self._put_plan("p1", active=False)
        index, active_plans = db.build_active_plan_ride_index("2026-06-23")
        assert index == {}
        assert active_plans == []

    def test_legacy_plan_without_active_field_included(self):
        # Rows predating the `active` field still fire (back-compat).
        self._put_plan("p1", active=None)
        index, _ = db.build_active_plan_ride_index("2026-06-23")
        assert ("megan", "p1") in index.get("r1", [])

    def test_window_containing_now_included(self):
        now = datetime(2026, 6, 23, 14, 0, tzinfo=timezone(timedelta(hours=-4)))
        self._put_plan("p1", active=True, plan_window={
            "open": "2026-06-23T10:00:00-04:00",
            "close": "2026-06-23T22:00:00-04:00",
        })
        index, _ = db.build_active_plan_ride_index("2026-06-23", now_et=now)
        assert ("megan", "p1") in index.get("r1", [])

    def test_window_before_open_excluded(self):
        now = datetime(2026, 6, 23, 8, 0, tzinfo=timezone(timedelta(hours=-4)))
        self._put_plan("p1", active=True, plan_window={
            "open": "2026-06-23T10:00:00-04:00",
            "close": "2026-06-23T22:00:00-04:00",
        })
        index, active_plans = db.build_active_plan_ride_index("2026-06-23", now_et=now)
        assert index == {}
        assert active_plans == []

    def test_no_window_always_included(self):
        now = datetime(2026, 6, 23, 3, 0, tzinfo=timezone(timedelta(hours=-4)))
        self._put_plan("p1", active=True)  # no plan_window
        index, _ = db.build_active_plan_ride_index("2026-06-23", now_et=now)
        assert ("megan", "p1") in index.get("r1", [])

    def test_window_fail_open_on_unparseable(self):
        now = datetime(2026, 6, 23, 14, 0, tzinfo=timezone(timedelta(hours=-4)))
        self._put_plan("p1", active=True,
                       plan_window={"open": "morning", "close": "night"})
        index, _ = db.build_active_plan_ride_index("2026-06-23", now_et=now)
        assert ("megan", "p1") in index.get("r1", [])  # fail-open

    def test_plan_for_other_date_excluded(self):
        # A plan for a different day must not surface in today's index.
        # The GSI key condition handles this in prod; the Python date
        # guard covers it under the stub (which returns all items).
        self._put_plan("p1", active=True, pfd="2026-06-24")
        index, active_plans = db.build_active_plan_ride_index("2026-06-23")
        assert index == {}
        assert active_plans == []

    def test_outcome_recorded_plan_excluded(self):
        # A plan whose outcome is already recorded is done — no alerts.
        self._put_plan("p1", active=True, outcome_recorded=True)
        index, active_plans = db.build_active_plan_ride_index("2026-06-23")
        assert index == {}
        assert active_plans == []

    def test_many_plans_paginated(self):
        # Larger fixture that drives the ExclusiveStartKey loop (page_size
        # forces multi-page results). Guards the pagination path CLAUDE.md
        # calls for in the data-growth category: every active plan must be
        # found across pages, not just the first page's worth.
        self.stub.page_size = 2
        for i in range(5):
            self._put_plan(f"p{i}", active=True, ride_id=f"r{i}")
        index, active_plans = db.build_active_plan_ride_index("2026-06-23")
        assert len(active_plans) == 5
        for i in range(5):
            assert ("megan", f"p{i}") in index.get(f"r{i}", [])
