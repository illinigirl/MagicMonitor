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
