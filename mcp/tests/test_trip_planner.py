"""Tests for the multi-day trip planner write tools (M5, phase 1).

Covers the new/changed write-side logic in server.py:
- record_plan: same-day auto-activates, future-dated stays dormant,
  bad date fails loudly, created_by attribution.
- create_trip: writes a TRIP header + one dormant day-plan per date;
  validates everything before any write.
- get_plan_for_day: finds the day's plan, prefers the active one.
- get_upcoming_trip: returns the nearest trip + per-day status.
- _plan_pending_ttl: date-based TTL (future plans survive past their day).

DDB is stubbed with a dict-backed table (the project convention is a
stub table, not moto). The stub implements just the surface these tools
use: put_item / query / batch_writer.
"""

from datetime import datetime, timedelta, timezone

import pytest

import server  # conftest puts mcp/ on the path


# ─── Dict-backed stub table ─────────────────────────────────────────


class _StubTable:
    def __init__(self):
        self.items: dict[tuple, dict] = {}

    def put_item(self, Item):
        self.items[(Item["PK"], Item["SK"])] = dict(Item)

    def get_item(self, Key):
        it = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(it)} if it else {}

    def query(self, KeyConditionExpression=None, ExpressionAttributeValues=None,
              ScanIndexForward=True, Limit=None, ExclusiveStartKey=None):
        pk = ExpressionAttributeValues[":pk"]
        sk_prefix = ExpressionAttributeValues.get(":sk", "")
        rows = [
            dict(v) for (p, s), v in self.items.items()
            if p == pk and s.startswith(sk_prefix)
        ]
        rows.sort(key=lambda r: r["SK"], reverse=not ScanIndexForward)
        if Limit is not None:
            rows = rows[:Limit]
        return {"Items": rows}

    def update_item(self, Key, UpdateExpression=None, ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None, ConditionExpression=None,
                    ReturnValues=None):
        key = (Key["PK"], Key["SK"])
        if (ConditionExpression and "attribute_exists(PK)" in ConditionExpression
                and key not in self.items):
            from botocore.exceptions import ClientError
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "cond"}},
                "UpdateItem",
            )
        item = self.items.setdefault(key, {"PK": Key["PK"], "SK": Key["SK"]})
        names = ExpressionAttributeNames or {}
        vals = ExpressionAttributeValues or {}
        expr = (UpdateExpression or "").strip()
        if expr.upper().startswith("SET "):
            for assign in expr[4:].split(","):
                lhs, rhs = assign.split("=")
                attr = lhs.strip()
                attr = names.get(attr, attr)  # resolve #ttl etc.
                item[attr] = vals[rhs.strip()]
        if ReturnValues == "ALL_NEW":
            return {"Attributes": dict(item)}
        return {}

    def batch_writer(self):
        table = self

        class _BW:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

            def put_item(self_, Item):
                table.put_item(Item=Item)

        return _BW()


@pytest.fixture
def stub(monkeypatch):
    t = _StubTable()
    monkeypatch.setattr(server, "_ddb_table", lambda: t)
    return t


def _today():
    return server._today_et_date_iso()


def _future(days):
    d = datetime.fromisoformat(_today()).date() + timedelta(days=days)
    return d.isoformat()


# ─── record_plan ────────────────────────────────────────────────────


class TestRecordPlan:
    def test_same_day_auto_activates(self, stub):
        out = server.record_plan("MK", [{"ride_name": "Space Mountain", "ride_id": "sm"}])
        assert out["active"] is True
        assert out["planned_for_date"] == _today()
        stored = stub.items[(f"USER#megan", f"PLAN#{out['plan_id']}")]
        assert stored["active"] is True
        assert stored["activated_at"] is not None
        assert stored["created_by"] == "megan"

    def test_future_date_is_dormant(self, stub):
        fut = _future(30)
        out = server.record_plan("EPCOT", [{"ride_name": "Test Track", "ride_id": "tt"}],
                                  planned_for_date=fut, trip_id="trip-x")
        assert out["active"] is False
        assert out["planned_for_date"] == fut
        assert out["trip_id"] == "trip-x"
        stored = stub.items[("USER#megan", f"PLAN#{out['plan_id']}")]
        assert stored["active"] is False
        assert stored["activated_at"] is None
        # Future plan's TTL must be well beyond 24h (survives until its day).
        assert stored["ttl"] > int(datetime.now(timezone.utc).timestamp()) + 5 * 24 * 3600

    def test_bad_date_fails_loud(self, stub):
        out = server.record_plan("MK", [], planned_for_date="June 23")
        assert "Invalid planned_for_date" in out["error"]

    def test_created_by_attribution(self, stub):
        out = server.record_plan("MK", [], created_by="jim")
        stored = stub.items[("USER#megan", f"PLAN#{out['plan_id']}")]
        assert stored["created_by"] == "jim"


# ─── create_trip ────────────────────────────────────────────────────


class TestCreateTrip:
    def test_writes_header_and_dormant_days(self, stub):
        days = [
            {"date": _future(20), "park": "MK", "ride_sequence": [{"ride_name": "Space", "ride_id": "sm"}]},
            {"date": _future(21), "park": "EPCOT"},
        ]
        out = server.create_trip("June trip", days)
        assert out["start_date"] == _future(20)
        assert out["end_date"] == _future(21)
        assert len(out["days"]) == 2

        # one TRIP header + two PLAN rows
        trip_rows = [v for (p, s), v in stub.items.items() if s.startswith("TRIP#")]
        plan_rows = [v for (p, s), v in stub.items.items() if s.startswith("PLAN#")]
        assert len(trip_rows) == 1
        assert len(plan_rows) == 2
        assert trip_rows[0]["name"] == "June trip"
        assert {d["date"] for d in trip_rows[0]["days"]} == {_future(20), _future(21)}
        # every day-plan is dormant + tagged with the trip_id
        for pr in plan_rows:
            assert pr["active"] is False
            assert pr["trip_id"] == out["trip_id"]

    def test_empty_days_errors(self, stub):
        assert "at least one day" in server.create_trip("x", [])["error"]

    def test_bad_date_errors(self, stub):
        out = server.create_trip("x", [{"date": "nope", "park": "MK"}])
        assert out["error"] == "Invalid day date"
        assert stub.items == {}  # nothing written

    def test_bad_park_errors(self, stub):
        out = server.create_trip("x", [{"date": _future(5), "park": "Narnia"}])
        assert out["error"] == "Invalid day park"
        assert stub.items == {}

    def test_missing_fields_error(self, stub):
        out = server.create_trip("x", [{"date": _future(5)}])  # no park
        assert "needs 'date' and 'park'" in out["error"]


# ─── get_plan_for_day ───────────────────────────────────────────────


class TestGetPlanForDay:
    def test_finds_today(self, stub):
        rec = server.record_plan("MK", [{"ride_name": "Space", "ride_id": "sm"}])
        out = server.get_plan_for_day()
        assert out["found"] is True
        assert out["plan_id"] == rec["plan_id"]
        assert out["active"] is True

    def test_not_found(self, stub):
        out = server.get_plan_for_day(date=_future(99))
        assert out["found"] is False

    def test_prefers_active(self, stub):
        # Two plans for the same day — one dormant, one active.
        d = _today()
        dormant = server.record_plan("MK", [], planned_for_date=d, active=False)
        active = server.record_plan("MK", [], planned_for_date=d, active=True)
        out = server.get_plan_for_day(date=d)
        assert out["found"] is True
        assert out["active"] is True
        assert out["plan_id"] == active["plan_id"]
        assert out["other_plans_for_day"] == 1

    def test_bad_date_errors(self, stub):
        assert "Invalid date" in server.get_plan_for_day(date="soon")["error"]


# ─── get_upcoming_trip ──────────────────────────────────────────────


class TestGetUpcomingTrip:
    def test_returns_trip_with_day_status(self, stub):
        days = [{"date": _future(10), "park": "MK"}, {"date": _future(11), "park": "EPCOT"}]
        created = server.create_trip("Trip", days)
        out = server.get_upcoming_trip()
        assert out["found"] is True
        assert out["trip_id"] == created["trip_id"]
        assert len(out["days"]) == 2
        assert all(d["active"] is False for d in out["days"])
        assert {d["park_key"] for d in out["days"]} == {"magic_kingdom", "epcot"}

    def test_none_upcoming(self, stub):
        assert server.get_upcoming_trip()["found"] is False

    def test_past_trip_excluded(self, stub):
        # A trip whose last day is in the past should not be "upcoming".
        server.create_trip("Old", [{"date": _future(-10), "park": "MK"}])
        assert server.get_upcoming_trip()["found"] is False


# ─── activate_plan ──────────────────────────────────────────────────


class TestActivatePlan:
    def test_activates_dormant_future_plan_by_date(self, stub):
        d = _future(15)
        server.create_trip("Trip", [{"date": d, "park": "MK"}])
        # The day-plan is dormant; activate it by date.
        out = server.activate_plan(date=d, ride_sequence=[{"ride_name": "Space", "ride_id": "sm"}],
                                   plan_window={"open": "10:00", "close": "22:00"})
        assert out["active"] is True
        assert out["activated_at"] is not None
        assert out["ride_count"] == 1
        assert out["plan_window"] == {"open": "10:00", "close": "22:00"}
        # underlying row now active with the re-evaluated sequence
        pk_sk = ("USER#megan", f"PLAN#{out['plan_id']}")
        assert stub.items[pk_sk]["active"] is True
        assert len(stub.items[pk_sk]["ride_sequence"]) == 1

    def test_activate_by_plan_id(self, stub):
        rec = server.record_plan("MK", [], planned_for_date=_future(5), active=False)
        out = server.activate_plan(plan_id=rec["plan_id"])
        assert out["active"] is True
        assert stub.items[("USER#megan", f"PLAN#{rec['plan_id']}")]["active"] is True

    def test_activate_no_plan_for_day(self, stub):
        out = server.activate_plan(date=_future(77))
        assert "No plan to activate" in out["error"]

    def test_activate_unknown_plan_id(self, stub):
        out = server.activate_plan(plan_id="2026-01-01T00:00:00+00:00")
        assert out["error"] == "Plan not found"


# ─── TTL helper ─────────────────────────────────────────────────────


class TestPendingTtl:
    def test_future_survives_past_its_day(self):
        fut = _future(30)
        ttl = server._plan_pending_ttl(fut)
        fut_epoch = datetime.fromisoformat(fut).replace(tzinfo=server._EASTERN).timestamp()
        assert ttl > fut_epoch  # expires AFTER the trip day

    def test_bad_date_falls_back_to_24h(self):
        ttl = server._plan_pending_ttl("not-a-date")
        now = int(datetime.now(timezone.utc).timestamp())
        assert now < ttl <= now + server._PLAN_PENDING_TTL_SECS + 5
