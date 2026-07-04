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
        elif expr.upper().startswith(("ADD ", "DELETE ")):
            # Atomic set ops (alert_subscribers). DDB semantics: ADD
            # creates the set if absent; DELETE removing the last member
            # removes the attribute entirely.
            op, rest = expr.split(" ", 1)
            attr, rhs = rest.strip().split(" ")
            val = set(vals[rhs.strip()])
            cur = set(item.get(attr) or set())
            cur = cur | val if op.upper() == "ADD" else cur - val
            if cur:
                item[attr] = cur
            else:
                item.pop(attr, None)
        if ReturnValues == "ALL_NEW":
            return {"Attributes": dict(item)}
        return {}

    def delete_item(self, Key, ConditionExpression=None):
        key = (Key["PK"], Key["SK"])
        if (ConditionExpression and "attribute_exists(PK)" in ConditionExpression
                and key not in self.items):
            from botocore.exceptions import ClientError
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "cond"}},
                "DeleteItem",
            )
        self.items.pop(key, None)

    def batch_writer(self):
        table = self

        class _BW:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

            def put_item(self_, Item):
                table.put_item(Item=Item)

            def delete_item(self_, Key):
                table.delete_item(Key=Key)

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

    def test_bad_park_returns_clean_error(self, stub):
        # A malformed park name returns a clean error, not a raised
        # exception (parity with the HTTP server's guard).
        out = server.record_plan("Narnia", [{"ride_name": "X", "position": 1}])
        assert out["error"] == "Invalid park"
        assert "Narnia" in out["error_message"]

    def test_created_by_attribution(self, stub):
        out = server.record_plan("MK", [], created_by="jim")
        stored = stub.items[("USER#megan", f"PLAN#{out['plan_id']}")]
        assert stored["created_by"] == "jim"

    def test_upsert_preserves_completed_rides(self, stub):
        # Re-recording a same-day plan must NOT wipe execution state
        # captured by mark_ride_complete. The upsert reuses the SK and
        # does a full put_item; without the merge, the completed ride and
        # its actual_wait_min (the strongest calibration signal) vanish.
        out = server.record_plan("MK", [
            {"ride_name": "Space Mountain", "ride_id": "sm"},
            {"ride_name": "Pirates", "ride_id": "pi"},
        ])
        plan_id = out["plan_id"]
        server.mark_ride_complete(plan_id, "sm", "Space Mountain", actual_wait_min=35)
        assert len(stub.items[("USER#megan", f"PLAN#{plan_id}")]["completed_rides"]) == 1

        # Re-record the same day (same trip_id=None) → upsert in place.
        out2 = server.record_plan("MK", [{"ride_name": "Pirates", "ride_id": "pi"}])
        assert out2["plan_id"] == plan_id  # same row, not a duplicate
        stored = stub.items[("USER#megan", f"PLAN#{plan_id}")]
        assert len(stored["completed_rides"]) == 1
        assert stored["completed_rides"][0].get("actual_wait_min") == 35

    def test_upsert_preserves_plan_window_when_not_respecified(self, stub):
        # An already-resolved plan_window survives a re-record that doesn't
        # pass one; a re-record that DOES pass one overrides it.
        out = server.record_plan("MK", [], plan_window={"open": "09:00", "close": "22:00"})
        plan_id = out["plan_id"]
        server.record_plan("MK", [])  # no plan_window passed
        assert stub.items[("USER#megan", f"PLAN#{plan_id}")]["plan_window"] == {
            "open": "09:00", "close": "22:00",
        }

    def test_naive_planned_at_normalized_to_aware(self, stub):
        # A naive timestamp (plausible model output) must be coerced to an
        # aware UTC SK so downstream aware-minus-naive date math can't crash.
        out = server.record_plan("MK", [], context={"planned_at": "2026-06-09T18:00"})
        assert "error" not in out
        parsed = datetime.fromisoformat(out["plan_id"])
        assert parsed.tzinfo is not None

    def test_unparseable_planned_at_fails_loud(self, stub):
        out = server.record_plan("MK", [], context={"planned_at": "sometime tuesday"})
        assert out["error"] == "Invalid context.planned_at"
        assert stub.items == {}  # nothing written

    def test_datetime_form_planned_for_date_normalized(self, stub):
        # A datetime-shaped date must be stored as bare YYYY-MM-DD, or it
        # would silently never match the date string-equality used for
        # activation + get_plan_for_day.
        out = server.record_plan(
            "MK", [], planned_for_date="2026-12-23T09:00:00"
        )
        assert out["planned_for_date"] == "2026-12-23"
        stored = stub.items[("USER#megan", f"PLAN#{out['plan_id']}")]
        assert stored["planned_for_date"] == "2026-12-23"
        # ...and it's now findable by the bare date.
        found = server.get_plan_for_day("2026-12-23")
        assert found["found"] is True

    def test_get_plan_for_day_normalizes_datetime_arg(self, stub):
        server.record_plan("MK", [], planned_for_date="2026-12-23")
        found = server.get_plan_for_day("2026-12-23T15:00:00")
        assert found["found"] is True


class TestRecordPlanDedupWarning:
    def test_warns_when_dedup_read_fails(self, stub, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("ddb blip")

        monkeypatch.setattr(server, "_query_user_prefix", _boom)
        out = server.record_plan("MK", [{"ride_name": "Space", "ride_id": "sm"}])
        # Plan still written, but the response flags the possible duplicate.
        assert "warning" in out
        assert ("USER#megan", f"PLAN#{out['plan_id']}") in stub.items


class TestRecordOutcomeValidation:
    def test_invalid_aggression_rating_rejected(self, stub):
        out = server.record_plan_outcome("PLAN#x", aggression_rating="slightly_aggressive")
        assert out["error"] == "Invalid aggression_rating"
        assert stub.items == {}  # rejected before any write

    def test_invalid_timing_rating_rejected(self, stub):
        out = server.record_plan_outcome("PLAN#x", timing_rating="overran")
        assert out["error"] == "Invalid timing_rating"

    def test_valid_ratings_accepted(self, stub):
        plan = server.record_plan("MK", [{"ride_name": "Space", "ride_id": "sm"}])
        out = server.record_plan_outcome(
            plan["plan_id"], aggression_rating="about_right", timing_rating="on_time"
        )
        assert "error" not in out
        stored = stub.items[("USER#megan", f"PLAN#{plan['plan_id']}")]
        assert stored["aggression_rating"] == "about_right"
        assert stored["timing_rating"] == "on_time"


class TestPlanHistory:
    def test_tolerates_legacy_naive_planned_at(self, stub):
        # A legacy row whose planned_at is naive (written before
        # normalization) must not crash the whole history read with an
        # uncaught aware-minus-naive TypeError.
        stub.put_item(Item={
            "PK": "USER#megan", "SK": "PLAN#2026-06-09T18:00",
            "planned_at": "2026-06-09T18:00",  # naive, pre-normalization
            "planned_for_date": "2026-06-09", "outcome_recorded": False,
            "ride_sequence": [], "park_key": "magic_kingdom",
        })
        out = server.get_user_plan_history()
        assert "error" not in out
        assert out["count"] == 1
        assert out["plans"][0]["days_since_plan"] is not None

    def test_unrecorded_only_finds_old_plan_behind_newer_recorded(self, stub):
        # include_unrecorded_only must filter BEFORE the limit. With limit=2
        # and three NEWER recorded plans ahead of one OLDER unrecorded plan,
        # the pre-fix behavior (Limit=2 then filter) returned nothing; the
        # fix paginates and surfaces the old unrecorded plan.
        def put(ts, recorded):
            stub.put_item(Item={
                "PK": "USER#megan", "SK": f"PLAN#{ts}",
                "planned_at": ts, "planned_for_date": ts[:10],
                "outcome_recorded": recorded, "ride_sequence": [],
                "park_key": "magic_kingdom",
            })
        put("2026-06-10T10:00:00+00:00", True)
        put("2026-06-09T10:00:00+00:00", True)
        put("2026-06-08T10:00:00+00:00", True)
        put("2026-06-01T10:00:00+00:00", False)  # older, unrecorded

        out = server.get_user_plan_history(include_unrecorded_only=True, limit=2)
        ids = [p["plan_id"] for p in out["plans"]]
        assert "2026-06-01T10:00:00+00:00" in ids
        assert all(not p["outcome_recorded"] for p in out["plans"])


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
        # Two plans for the same day — one dormant, one active. record_plan
        # now upserts per (date, trip_id), so two same-day calls would
        # collapse to one row; inject the pair directly to exercise the
        # "prefer active, count the rest" path get_plan_for_day still owns.
        d = _today()
        stub.put_item(Item={"PK": "USER#megan", "SK": "PLAN#2099-01-01T10:00:00+00:00",
                            "planned_for_date": d, "active": False, "park_key": "magic_kingdom"})
        stub.put_item(Item={"PK": "USER#megan", "SK": "PLAN#2099-01-01T11:00:00+00:00",
                            "planned_for_date": d, "active": True, "park_key": "magic_kingdom"})
        out = server.get_plan_for_day(date=d)
        assert out["found"] is True
        assert out["active"] is True
        assert out["plan_id"] == "2099-01-01T11:00:00+00:00"
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
    def test_activates_dormant_today_plan_by_date(self, stub):
        # Activation is an ON-THE-DAY action, so the dormant plan is dated
        # today (create_trip always writes dormant). Activating it works.
        d = _today()
        server.create_trip("Trip", [{"date": d, "park": "MK"}])
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

    def test_activate_by_plan_id_today(self, stub):
        rec = server.record_plan("MK", [], planned_for_date=_today(), active=False)
        out = server.activate_plan(plan_id=rec["plan_id"])
        assert out["active"] is True
        assert stub.items[("USER#megan", f"PLAN#{rec['plan_id']}")]["active"] is True

    def test_refuses_to_activate_a_future_plan(self, stub):
        # The guard: a future-dated dormant plan must NOT activate early —
        # it would fire disruption alerts weeks ahead. Activate on the day.
        rec = server.record_plan("MK", [], planned_for_date=_future(10),
                                  trip_id="t", active=False)
        out = server.activate_plan(plan_id=rec["plan_id"])
        assert out["error"] == "Plan is future-dated"
        assert stub.items[("USER#megan", f"PLAN#{rec['plan_id']}")]["active"] is False

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


# ─── delete_trip / delete_plan / update_trip (trip CRUD) ────────────


def _make_trip(stub, name="June trip"):
    """Build a 2-day trip via create_trip and return its trip_id."""
    out = server.create_trip(name, [
        {"date": _future(20), "park": "MK", "ride_sequence": [{"ride_name": "Space", "ride_id": "sm"}]},
        {"date": _future(21), "park": "EPCOT"},
    ])
    return out["trip_id"]


class TestDeleteTrip:
    def test_cascade_deletes_header_and_days(self, stub):
        trip_id = _make_trip(stub)
        out = server.delete_trip(trip_id)
        assert out["ok"] is True
        assert out["deleted_days"] == 2
        assert out["deleted_header"] is True
        # nothing left under this trip
        assert ("USER#megan", f"TRIP#{trip_id}") not in stub.items
        assert not [k for k, v in stub.items.items()
                    if v.get("trip_id") == trip_id]

    def test_not_found(self, stub):
        out = server.delete_trip("nope_123")
        assert out["error"] == "Trip not found"

    def test_refuses_when_a_day_has_outcome(self, stub):
        trip_id = _make_trip(stub)
        # mark one day's outcome recorded
        day = next(v for v in stub.items.values()
                   if v.get("trip_id") == trip_id)
        day["outcome_recorded"] = True
        out = server.delete_trip(trip_id)
        assert out["error"] == "Trip has recorded outcomes"
        assert day["planned_for_date"] in out["days_with_outcomes"]
        # nothing deleted
        assert ("USER#megan", f"TRIP#{trip_id}") in stub.items

    def test_force_deletes_despite_outcome(self, stub):
        trip_id = _make_trip(stub)
        day = next(v for v in stub.items.values() if v.get("trip_id") == trip_id)
        day["outcome_recorded"] = True
        out = server.delete_trip(trip_id, force=True)
        assert out["ok"] is True
        assert ("USER#megan", f"TRIP#{trip_id}") not in stub.items


class TestDeletePlan:
    def test_deletes_one_day(self, stub):
        rec = server.record_plan("MK", [], planned_for_date=_future(5), active=False)
        out = server.delete_plan(rec["plan_id"])
        assert out["ok"] is True
        assert ("USER#megan", f"PLAN#{rec['plan_id']}") not in stub.items

    def test_not_found(self, stub):
        out = server.delete_plan("2026-01-01T00:00:00+00:00")
        assert out["error"] == "Plan not found"

    def test_refuses_when_outcome_recorded(self, stub):
        rec = server.record_plan("MK", [], planned_for_date=_future(5), active=False)
        stub.items[("USER#megan", f"PLAN#{rec['plan_id']}")]["outcome_recorded"] = True
        out = server.delete_plan(rec["plan_id"])
        assert out["error"] == "Plan has a recorded outcome"
        assert ("USER#megan", f"PLAN#{rec['plan_id']}") in stub.items  # not deleted


class TestUpdateTrip:
    def test_rename(self, stub):
        trip_id = _make_trip(stub, name="old name")
        out = server.update_trip(trip_id, "June 2026 family trip")
        assert out["ok"] is True
        assert stub.items[("USER#megan", f"TRIP#{trip_id}")]["name"] == "June 2026 family trip"

    def test_not_found(self, stub):
        out = server.update_trip("nope_123", "x")
        assert out["error"] == "Trip not found"


# ─── get_upcoming_trip derives days from PLAN# rows (header-sync fix) ─


class TestUpcomingTripDerivation:
    def test_day_added_after_create_shows_up(self, stub):
        # The header-sync bug: a day added via record_plan(trip_id=...) for
        # a date NOT in the create_trip header must still appear, because
        # the day list is derived from PLAN# rows, not the header.
        trip_id = _make_trip(stub)  # days at +20, +21
        added = _future(22)
        server.record_plan("HS", [], planned_for_date=added,
                           trip_id=trip_id, active=False)
        out = server.get_upcoming_trip()
        assert out["found"] is True
        dates = [d["date"] for d in out["days"]]
        assert added in dates                       # the new day surfaced
        assert out["end_date"] == added             # date range derived from rows
        assert len(out["days"]) == 3

    def test_no_upcoming_when_all_days_past(self, stub):
        # A trip whose days are all in the past is not "upcoming" even if a
        # stale header end_date might once have suggested otherwise.
        out = server.create_trip("old", [{"date": "2020-01-01", "park": "MK"}])
        res = server.get_upcoming_trip()
        assert res["found"] is False


# ─── record_plan upsert-per-day + get_upcoming_trip dedupe ──────────


class TestRecordPlanUpsert:
    @staticmethod
    def _plan_rows(stub, trip_id=None):
        return [v for (p, s), v in stub.items.items()
                if s.startswith("PLAN#") and (trip_id is None or v.get("trip_id") == trip_id)]

    def test_re_record_same_trip_day_updates_in_place(self, stub):
        # The bug this fixes: re-recording a pre-built day (e.g. to add
        # shows) used to append a 2nd row for the date. Now it upserts.
        tid = "t-upsert"
        r1 = server.record_plan("MK", [{"ride_name": "Space", "ride_id": "sm"}],
                                planned_for_date=_future(10), trip_id=tid, active=False)
        r2 = server.record_plan(
            "MK", [{"ride_name": "Space", "ride_id": "sm"}],
            show_selections=[{"show_name": "Happily Ever After",
                              "performance_start": "2099-01-01T21:00:00",
                              "predicted_arrival_min": 45}],
            planned_for_date=_future(10), trip_id=tid, active=False)
        assert r2["plan_id"] == r1["plan_id"]              # same SK reused
        rows = self._plan_rows(stub, tid)
        assert len(rows) == 1                              # ONE row, not two
        assert len(rows[0].get("show_selections") or []) == 1  # shows landed

    def test_re_record_standalone_same_day_updates_in_place(self, stub):
        r1 = server.record_plan("MK", [], planned_for_date=_future(3), active=False)
        r2 = server.record_plan("MK", [{"ride_name": "TRON", "ride_id": "tron"}],
                                planned_for_date=_future(3), active=False)
        assert r2["plan_id"] == r1["plan_id"]
        rows = [v for v in self._plan_rows(stub) if not v.get("trip_id")]
        assert len(rows) == 1
        assert len(rows[0].get("ride_sequence") or []) == 1

    def test_different_dates_do_not_collide(self, stub):
        tid = "t-multi"
        a = server.record_plan("MK", [], planned_for_date=_future(5), trip_id=tid, active=False)
        b = server.record_plan("EPCOT", [], planned_for_date=_future(6), trip_id=tid, active=False)
        assert a["plan_id"] != b["plan_id"]
        assert len(self._plan_rows(stub, tid)) == 2

    def test_recorded_outcome_not_overwritten(self, stub):
        # A day whose outcome is already recorded must not be silently
        # clobbered by a re-record — that one is history; a new row is fine.
        tid = "t-rec"
        r1 = server.record_plan("MK", [], planned_for_date=_future(8), trip_id=tid, active=False)
        stub.items[("USER#megan", f"PLAN#{r1['plan_id']}")]["outcome_recorded"] = True
        r2 = server.record_plan("MK", [], planned_for_date=_future(8), trip_id=tid, active=False)
        assert r2["plan_id"] != r1["plan_id"]              # new row, didn't touch the recorded one
        assert len(self._plan_rows(stub, tid)) == 2

    def test_upcoming_trip_dedupes_duplicate_date(self, stub):
        # Inject a legacy dup (two rows, same date+trip) and confirm
        # get_upcoming_trip collapses to one day, preferring the active row.
        tid = "t-dup"
        stub.put_item(Item={"PK": "USER#megan", "SK": f"TRIP#{tid}", "name": "Dup trip",
                            "days": [{"date": _future(15), "park_key": "magic_kingdom"}]})
        stub.put_item(Item={"PK": "USER#megan", "SK": "PLAN#2099-01-01T10:00:00+00:00",
                            "planned_for_date": _future(15), "trip_id": tid, "active": False,
                            "ride_sequence": [{"ride_name": "A"}], "outcome_recorded": False})
        stub.put_item(Item={"PK": "USER#megan", "SK": "PLAN#2099-01-01T11:00:00+00:00",
                            "planned_for_date": _future(15), "trip_id": tid, "active": True,
                            "ride_sequence": [{"ride_name": "A"}, {"ride_name": "B"}],
                            "outcome_recorded": False})
        out = server.get_upcoming_trip()
        assert out["found"] is True and out["trip_id"] == tid
        assert len(out["days"]) == 1                        # the date shows ONCE
        assert out["days"][0]["active"] is True             # active row preferred
        assert out["days"][0]["ride_count"] == 2


# ─── set_plan_alert_subscription (2026-07-03) ───────────────────────


class TestPlanAlertSubscription:
    def _seed_profile(self, stub, member_id, pushover_key="pk-123"):
        item = {"PK": f"USER#{member_id}", "SK": "PROFILE"}
        if pushover_key:
            item["pushover_user_key"] = pushover_key
        stub.put_item(Item=item)

    def test_subscribe_member_trip_wide(self, stub):
        self._seed_profile(stub, "sub-sis")
        trip = server.create_trip("Trip", [
            {"date": _future(10), "park": "MK"},
            {"date": _future(11), "park": "EPCOT"},
        ])
        out = server.set_plan_alert_subscription("sub-sis", trip_id=trip["trip_id"])
        assert "error" not in out and "warning" not in out
        assert sorted(out["days_updated"]) == [_future(10), _future(11)]
        plan_rows = [v for (p, s), v in stub.items.items() if s.startswith("PLAN#")]
        for r in plan_rows:
            assert r["alert_subscribers"] == {"sub-sis"}

    def test_unsubscribe_removes_attribute(self, stub):
        self._seed_profile(stub, "sub-sis")
        trip = server.create_trip("Trip", [{"date": _future(10), "park": "MK"}])
        server.set_plan_alert_subscription("sub-sis", trip_id=trip["trip_id"])
        out = server.set_plan_alert_subscription(
            "sub-sis", subscribed=False, trip_id=trip["trip_id"]
        )
        assert out["subscribed"] is False
        row = next(v for (p, s), v in stub.items.items() if s.startswith("PLAN#"))
        assert "alert_subscribers" not in row  # last member out → attr gone

    def test_single_date_only_touches_that_day(self, stub):
        self._seed_profile(stub, "sub-sis")
        trip = server.create_trip("Trip", [
            {"date": _future(10), "park": "MK"},
            {"date": _future(11), "park": "EPCOT"},
        ])
        out = server.set_plan_alert_subscription("sub-sis", date=_future(11))
        assert out["days_updated"] == [_future(11)]
        rows = {v["planned_for_date"]: v for (p, s), v in stub.items.items()
                if s.startswith("PLAN#")}
        assert "alert_subscribers" not in rows[_future(10)]
        assert rows[_future(11)]["alert_subscribers"] == {"sub-sis"}

    def test_member_without_profile_errors(self, stub):
        server.create_trip("Trip", [{"date": _future(10), "park": "MK"}])
        out = server.set_plan_alert_subscription("nobody", date=_future(10))
        assert out["error"] == "Member has no profile"
        assert "/me" in out["error_message"]

    def test_owner_is_noop(self, stub):
        self._seed_profile(stub, "megan")
        server.create_trip("Trip", [{"date": _future(10), "park": "MK"}])
        out = server.set_plan_alert_subscription("megan", date=_future(10))
        assert out["days_updated"] == []
        assert "always receives" in out["note"]

    def test_missing_pushover_key_warns_but_stores(self, stub):
        self._seed_profile(stub, "sub-sis", pushover_key=None)
        server.create_trip("Trip", [{"date": _future(10), "park": "MK"}])
        out = server.set_plan_alert_subscription("sub-sis", date=_future(10))
        assert "Pushover" in out["warning"]
        row = next(v for (p, s), v in stub.items.items() if s.startswith("PLAN#"))
        assert row["alert_subscribers"] == {"sub-sis"}

    def test_requires_trip_or_date(self, stub):
        out = server.set_plan_alert_subscription("sub-sis")
        assert "Provide trip_id and/or date" in out["error"]

    def test_upsert_preserves_subscribers(self, stub):
        # The calibration-wipe bug class: a same-day re-record must not
        # drop opted-in members (put_item replaces the whole row).
        self._seed_profile(stub, "sub-sis")
        server.record_plan("MK", [], planned_for_date=_future(10), trip_id="t1")
        server.set_plan_alert_subscription("sub-sis", date=_future(10))
        server.record_plan("MK", [{"ride_name": "Space", "ride_id": "sm"}],
                           planned_for_date=_future(10), trip_id="t1")
        row = next(v for (p, s), v in stub.items.items() if s.startswith("PLAN#"))
        assert row["alert_subscribers"] == {"sub-sis"}


class TestSplitDroppedRides:
    """split_dropped_rides keeps the MCP planner's view in sync with the
    poller: rides in dropped_ride_ids (the /replan atomic set) leave
    ride_sequence and surface separately."""

    def test_no_drops_returns_all_planned(self):
        import _tool_impls
        plan = {"ride_sequence": [{"ride_id": "a"}, {"ride_id": "b"}]}
        still, dropped = _tool_impls.split_dropped_rides(plan)
        assert [r["ride_id"] for r in still] == ["a", "b"]
        assert dropped == []

    def test_dropped_split_out(self):
        import _tool_impls
        plan = {
            "ride_sequence": [{"ride_id": "a"}, {"ride_id": "b"}, {"ride_id": "c"}],
            "dropped_ride_ids": ["b"],
        }
        still, dropped = _tool_impls.split_dropped_rides(plan)
        assert [r["ride_id"] for r in still] == ["a", "c"]
        assert [r["ride_id"] for r in dropped] == ["b"]

    def test_accepts_set_or_list(self):
        import _tool_impls
        plan = {"ride_sequence": [{"ride_id": "a"}], "dropped_ride_ids": {"a"}}
        still, dropped = _tool_impls.split_dropped_rides(plan)
        assert still == [] and [r["ride_id"] for r in dropped] == ["a"]


class TestParseLLTime:
    def test_full_iso_passthrough(self):
        import _tool_impls
        assert _tool_impls.parse_ll_time(
            "2026-07-03T15:00:00-04:00", "2026-07-03"
        ) == "2026-07-03T15:00:00-04:00"

    def test_12h_and_24h_forms(self):
        import _tool_impls
        for s in ("3:00 PM", "3pm", "15:00"):
            out = _tool_impls.parse_ll_time(s, "2026-07-03")
            assert out is not None and "T15:00" in out

    def test_noon_midnight_edges(self):
        import _tool_impls
        assert "T12:00" in _tool_impls.parse_ll_time("12:00 PM", "2026-07-03")
        assert "T00:00" in _tool_impls.parse_ll_time("12:00 AM", "2026-07-03")

    def test_garbage_returns_none(self):
        import _tool_impls
        assert _tool_impls.parse_ll_time("later", "2026-07-03") is None
        assert _tool_impls.parse_ll_time("", "2026-07-03") is None


class TestPlanOrderHonored:
    """split_dropped_rides applies a Claude-set plan_order so the MCP view
    matches what the family reordered on /replan."""

    def test_reorders_still_by_plan_order(self):
        import _tool_impls
        plan = {
            "ride_sequence": [{"ride_id": "a"}, {"ride_id": "b"}, {"ride_id": "c"}],
            "plan_order": ["c", "a"],
        }
        still, _ = _tool_impls.split_dropped_rides(plan)
        # c, a first (in order), then b (unlisted) keeps trailing.
        assert [r["ride_id"] for r in still] == ["c", "a", "b"]

    def test_order_plus_drop(self):
        import _tool_impls
        plan = {
            "ride_sequence": [{"ride_id": "a"}, {"ride_id": "b"}, {"ride_id": "c"}],
            "plan_order": ["c", "b", "a"],
            "dropped_ride_ids": ["b"],
        }
        still, dropped = _tool_impls.split_dropped_rides(plan)
        assert [r["ride_id"] for r in still] == ["c", "a"]  # b dropped
        assert [r["ride_id"] for r in dropped] == ["b"]


# ─── record_plan ll_holds (pre-booked Lightning Lanes, 2026-07-04) ────
# Root cause pinned: with no LL parameter on record_plan, pre-booked
# MLL/ILL times only ever landed in free-text notes — invisible to the
# trip page and the alert engine (earlier-LL suppression, drift, nudge).


class TestRecordPlanLlHolds:
    RIDES = [
        {"ride_name": "Remy's Ratatouille Adventure", "ride_id": "remy"},
        {"ride_name": "Guardians of the Galaxy: Cosmic Rewind", "ride_id": "gotg"},
        {"ride_name": "Test Track", "ride_id": "tt"},
    ]

    def test_holds_resolve_by_name_and_id(self, stub):
        out = server.record_plan(
            "EPCOT", self.RIDES,
            ll_holds={"Remy": "10:00 AM", "gotg": "2:30 PM"},
        )
        stored = stub.items[("USER#megan", f"PLAN#{out['plan_id']}")]
        holds = stored["ll_holds"]
        assert set(holds) == {"remy", "gotg"}
        assert holds["remy"].startswith(f"{_today()}T10:00:00")
        assert holds["gotg"].startswith(f"{_today()}T14:30:00")
        # Result confirms what landed so the model can echo it back.
        assert out["ll_holds_recorded"] == holds

    def test_future_day_holds_use_that_day(self, stub):
        fut = _future(10)
        out = server.record_plan(
            "EPCOT", self.RIDES, planned_for_date=fut,
            ll_holds={"Test Track": "15:00"},
        )
        stored = stub.items[("USER#megan", f"PLAN#{out['plan_id']}")]
        assert stored["ll_holds"]["tt"].startswith(f"{fut}T15:00:00")

    def test_unknown_ride_fails_loud_and_writes_nothing(self, stub):
        out = server.record_plan(
            "EPCOT", self.RIDES, ll_holds={"Space Mountain": "10:00 AM"},
        )
        assert out["error"] == "Held-LL ride not in plan"
        assert stub.items == {}  # validate-before-write: no partial row

    def test_bad_time_fails_loud_and_writes_nothing(self, stub):
        out = server.record_plan(
            "EPCOT", self.RIDES, ll_holds={"Remy": "morningish"},
        )
        assert out["error"] == "Invalid held-LL return time"
        assert stub.items == {}

    def test_upsert_preserves_holds_when_not_respecified(self, stub):
        out = server.record_plan(
            "EPCOT", self.RIDES, ll_holds={"Remy": "10:00 AM"},
        )
        plan_id = out["plan_id"]
        # Re-record the day without ll_holds → prior holds survive.
        server.record_plan("EPCOT", self.RIDES)
        stored = stub.items[("USER#megan", f"PLAN#{plan_id}")]
        assert set(stored["ll_holds"]) == {"remy"}

    def test_upsert_with_new_holds_replaces(self, stub):
        out = server.record_plan(
            "EPCOT", self.RIDES, ll_holds={"Remy": "10:00 AM"},
        )
        plan_id = out["plan_id"]
        server.record_plan(
            "EPCOT", self.RIDES, ll_holds={"gotg": "2:30 PM"},
        )
        stored = stub.items[("USER#megan", f"PLAN#{plan_id}")]
        assert set(stored["ll_holds"]) == {"gotg"}


# ─── record_plan target_time / ll_planned / reservations (2026-07-04) ─
# Same structured-vs-notes bug class as ll_holds: ride times, planned-LL
# intent, and dining reservations all previously lived only in free text.


class TestRecordPlanStructuredFields:
    RIDES = [
        {"ride_name": "Remy's Ratatouille Adventure", "ride_id": "remy",
         "target_time": "10:00 AM"},
        {"ride_name": "Test Track", "ride_id": "tt",
         "predicted_wait_min": 15, "ll_planned": True},
    ]

    def test_target_time_normalized_to_et_iso(self, stub):
        out = server.record_plan("EPCOT", [dict(r) for r in self.RIDES])
        stored = stub.items[("USER#megan", f"PLAN#{out['plan_id']}")]
        remy = stored["ride_sequence"][0]
        assert remy["target_time"].startswith(f"{_today()}T10:00:00")
        assert "-04:00" in remy["target_time"] or "-05:00" in remy["target_time"]

    def test_ll_planned_flag_survives(self, stub):
        out = server.record_plan("EPCOT", [dict(r) for r in self.RIDES])
        stored = stub.items[("USER#megan", f"PLAN#{out['plan_id']}")]
        assert stored["ride_sequence"][1]["ll_planned"] is True

    def test_bad_target_time_fails_loud(self, stub):
        rides = [{"ride_name": "Remy", "ride_id": "remy", "target_time": "brunchish"}]
        out = server.record_plan("EPCOT", rides)
        assert out["error"] == "Invalid ride target_time"
        assert stub.items == {}

    def test_reservations_normalized_and_sorted(self, stub):
        out = server.record_plan(
            "EPCOT", [dict(r) for r in self.RIDES],
            reservations=[
                {"name": "Space 220", "time": "6:15 PM", "type": "dining"},
                {"name": "Crystal Palace", "time": "12:30 PM"},
            ],
        )
        stored = stub.items[("USER#megan", f"PLAN#{out['plan_id']}")]
        res = stored["reservations"]
        assert [r["name"] for r in res] == ["Crystal Palace", "Space 220"]  # time-sorted
        assert res[0]["time"].startswith(f"{_today()}T12:30:00")
        assert res[1]["type"] == "dining"

    def test_bad_reservation_time_fails_loud(self, stub):
        out = server.record_plan(
            "EPCOT", [dict(r) for r in self.RIDES],
            reservations=[{"name": "Space 220", "time": "dinnertime"}],
        )
        assert out["error"] == "Invalid reservation time"
        assert stub.items == {}

    def test_upsert_preserves_reservations_when_not_respecified(self, stub):
        out = server.record_plan(
            "EPCOT", [dict(r) for r in self.RIDES],
            reservations=[{"name": "Crystal Palace", "time": "12:30 PM"}],
        )
        plan_id = out["plan_id"]
        server.record_plan("EPCOT", [dict(r) for r in self.RIDES])
        stored = stub.items[("USER#megan", f"PLAN#{plan_id}")]
        assert [r["name"] for r in stored["reservations"]] == ["Crystal Palace"]


# ─── match_plan_ride (hardened ride matching, 2026-07-04) ─────────────
# First-substring-wins put a hold on Spaceship Earth twice when the
# intended ride was another "space" ride; colons defeated exact match.


class TestMatchPlanRide:
    SEQ = [
        {"ride_id": "sse", "ride_name": "Spaceship Earth"},
        {"ride_id": "ms", "ride_name": "Mission: SPACE"},
        {"ride_id": "tt", "ride_name": "Test Track"},
    ]

    def test_exact_id_wins(self):
        import _tool_impls
        m, err = _tool_impls.match_plan_ride(self.SEQ, "ms")
        assert err is None and m["ride_id"] == "ms"

    def test_punctuation_normalized_exact_name(self):
        import _tool_impls
        # "mission space" == "Mission: SPACE" despite the colon.
        m, err = _tool_impls.match_plan_ride(self.SEQ, "mission space")
        assert err is None and m["ride_id"] == "ms"

    def test_ambiguous_partial_fails_loud_with_candidates(self):
        import _tool_impls
        # "space" hits BOTH space rides → error naming them, never a guess.
        m, err = _tool_impls.match_plan_ride(self.SEQ, "space")
        assert m is None
        assert err["error"] == "Ambiguous ride"
        assert "Mission: SPACE" in err["error_message"]
        assert "Spaceship Earth" in err["error_message"]

    def test_unique_partial_matches(self):
        import _tool_impls
        m, err = _tool_impls.match_plan_ride(self.SEQ, "track")
        assert err is None and m["ride_id"] == "tt"

    def test_no_match_and_empty_query(self):
        import _tool_impls
        m, err = _tool_impls.match_plan_ride(self.SEQ, "everest")
        assert m is None and err["error"] == "Ride not in plan"
        m, err = _tool_impls.match_plan_ride(self.SEQ, "  ")
        assert m is None and err["error"] == "Ride required"

    def test_ll_holds_resolution_uses_hardened_matcher(self, stub):
        # An ambiguous ll_holds key on record_plan fails the whole call.
        out = server.record_plan(
            "EPCOT",
            [dict(r, position=i + 1) for i, r in enumerate(self.SEQ)],
            ll_holds={"space": "2:35 PM"},
        )
        assert out["error"] == "Held-LL ride not in plan"
        assert "more than one" in out["error_message"]
        assert stub.items == {}
