"""Tests for the multi-day trip WRITE tools on the HTTP transport (M5).

The tool *logic* is duplicated from server.py (tested in test_trip_planner).
What's unique to the HTTP port and tested here:
- Every write lands in the SHARED partition (USER#megan), never a
  per-caller partition, and there is NO client-supplied user_id param.
- `created_by` is derived from the verified token's sub (ContextVar),
  mapped to a friendly id, falling back to the raw sub when unmapped.
"""

import os

import pytest

os.environ.setdefault("MCP_PUBLIC_BASE_URL", "https://mcp.example.com")
os.environ.setdefault("COGNITO_USER_POOL_ID", "us-east-2_TESTPOOL")
os.environ.setdefault("COGNITO_REGION", "us-east-2")
os.environ.setdefault("COGNITO_DOMAIN_URL", "https://auth.example.com")

import server_http as s  # noqa: E402


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
        rows = [dict(v) for (p, sk), v in self.items.items()
                if p == pk and sk.startswith(sk_prefix)]
        return {"Items": rows}

    def update_item(self, Key, UpdateExpression=None, ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None, ConditionExpression=None, ReturnValues=None):
        key = (Key["PK"], Key["SK"])
        if (ConditionExpression and "attribute_exists(PK)" in ConditionExpression
                and key not in self.items):
            from botocore.exceptions import ClientError
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "c"}}, "UpdateItem")
        item = self.items.setdefault(key, {"PK": Key["PK"], "SK": Key["SK"]})
        names = ExpressionAttributeNames or {}
        vals = ExpressionAttributeValues or {}
        expr = (UpdateExpression or "").strip()
        if expr.upper().startswith("SET "):
            for assign in expr[4:].split(","):
                lhs, rhs = assign.split("=")
                attr = names.get(lhs.strip(), lhs.strip())
                item[attr] = vals[rhs.strip()]
        if ReturnValues == "ALL_NEW":
            return {"Attributes": dict(item)}
        return {}

    def delete_item(self, Key, ConditionExpression=None):
        key = (Key["PK"], Key["SK"])
        if (ConditionExpression and "attribute_exists(PK)" in ConditionExpression
                and key not in self.items):
            from botocore.exceptions import ClientError
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "c"}}, "DeleteItem")
        self.items.pop(key, None)

    def batch_writer(self):
        table = self

        class _BW:
            def __enter__(self_): return self_
            def __exit__(self_, *a): return False
            def put_item(self_, Item): table.put_item(Item=Item)
            def delete_item(self_, Key): table.delete_item(Key=Key)
        return _BW()


@pytest.fixture
def stub(monkeypatch):
    t = _StubTable()
    monkeypatch.setattr(s, "_ddb_table", lambda: t)
    monkeypatch.setattr(s, "_SUB_USER_MAP", {"sub-megan": "megan", "sub-jim": "jim"})
    yield t


@pytest.fixture
def as_user(monkeypatch):
    """Set the authenticated sub ContextVar for the duration of a test."""
    def _set(sub):
        token = s._authenticated_sub.set(sub)
        return token
    tokens = []
    def setter(sub):
        tokens.append(s._authenticated_sub.set(sub))
    yield setter
    for tok in reversed(tokens):
        s._authenticated_sub.reset(tok)


# ─── Identity / created_by ──────────────────────────────────────────


class TestIdentity:
    def test_created_by_maps_known_sub(self, stub, as_user):
        as_user("sub-jim")
        assert s._created_by_from_context() == "jim"

    def test_created_by_falls_back_to_raw_sub(self, stub, as_user):
        as_user("sub-unmapped-uuid")
        assert s._created_by_from_context() == "sub-unmapped-uuid"

    def test_created_by_no_context_is_shared_id(self, stub):
        assert s._created_by_from_context() == s._SHARED_USER_ID


# ─── Shared partition + attribution on writes ───────────────────────


class TestSharedWrites:
    def test_record_plan_shared_partition_and_attribution(self, stub, as_user):
        as_user("sub-jim")
        out = s.record_plan("MK", [{"ride_name": "Space", "ride_id": "sm"}])
        assert out["created_by"] == "jim"
        # Always the shared partition, regardless of who called.
        assert ("USER#megan", f"PLAN#{out['plan_id']}") in stub.items
        assert stub.items[("USER#megan", f"PLAN#{out['plan_id']}")]["created_by"] == "jim"
        assert out["active"] is True  # same-day

    def test_no_user_id_param(self):
        # Security: HTTP write tools must NOT accept a client user_id.
        import inspect
        for name in ["record_plan", "create_trip", "activate_plan",
                     "mark_ride_complete", "record_plan_outcome",
                     "delete_trip", "delete_plan", "update_trip"]:
            params = inspect.signature(getattr(s, name)).parameters
            assert "user_id" not in params, f"{name} must not expose user_id"

    def test_create_trip_shared_and_dormant(self, stub, as_user):
        as_user("sub-megan")
        days = [{"date": "2099-06-23", "park": "MK"}, {"date": "2099-06-24", "park": "EPCOT"}]
        out = s.create_trip("Future trip", days)
        assert out["created_by"] == "megan"
        plan_rows = [v for (p, sk), v in stub.items.items() if sk.startswith("PLAN#")]
        trip_rows = [v for (p, sk), v in stub.items.items() if sk.startswith("TRIP#")]
        assert len(plan_rows) == 2 and len(trip_rows) == 1
        assert all(p == "USER#megan" for (p, sk) in stub.items)
        assert all(r["active"] is False and r["created_by"] == "megan" for r in plan_rows)


# ─── Activation + read on the shared partition ──────────────────────


class TestActivateAndRead:
    def test_activate_future_plan(self, stub, as_user):
        as_user("sub-megan")
        rec = s.record_plan("EPCOT", [], planned_for_date="2099-07-01", active=False)
        assert rec["active"] is False
        out = s.activate_plan(plan_id=rec["plan_id"],
                              ride_sequence=[{"ride_name": "TT", "ride_id": "tt"}],
                              plan_window={"open": "10:00", "close": "21:00"})
        assert out["active"] is True
        assert out["ride_count"] == 1
        assert stub.items[("USER#megan", f"PLAN#{rec['plan_id']}")]["active"] is True

    def test_get_plan_for_day_today(self, stub, as_user):
        as_user("sub-megan")
        rec = s.record_plan("MK", [{"ride_name": "Space", "ride_id": "sm"}])
        out = s.get_plan_for_day()
        assert out["found"] is True
        assert out["plan_id"] == rec["plan_id"]
        assert out["created_by"] == "megan"

    def test_mark_ride_complete(self, stub, as_user):
        as_user("sub-megan")
        rec = s.record_plan("MK", [{"ride_name": "Space", "ride_id": "sm"}])
        out = s.mark_ride_complete(rec["plan_id"], "sm", "Space Mountain", actual_wait_min=30)
        assert out["completed"] == 1
        stored = stub.items[("USER#megan", f"PLAN#{rec['plan_id']}")]
        assert len(stored["ride_sequence"]) == 0
        assert stored["completed_rides"][0]["actual_wait_min"] == 30


class TestPlanHistory:
    def test_history_and_calibration(self, stub, as_user):
        as_user("sub-megan")
        rec = s.record_plan("MK", [{"ride_name": "Space", "ride_id": "sm"}])
        s.record_plan_outcome(rec["plan_id"], aggression_rating="about_right",
                              timing_rating="on_time")
        out = s.get_user_plan_history()
        assert out["count"] == 1
        assert out["plans"][0]["created_by"] == "megan"
        assert out["plans"][0]["outcome_recorded"] is True
        # one recorded plan → calibration summary present
        assert out["calibration_summary"] is not None
        assert out["calibration_summary"]["n_recorded_plans"] == 1

    def test_history_no_calibration_flag(self, stub, as_user):
        as_user("sub-megan")
        s.record_plan("MK", [])
        out = s.get_user_plan_history(include_calibration=False)
        assert "calibration_summary" not in out


# ─── trip CRUD (delete_trip / delete_plan / update_trip) ────────────


class TestTripCrudHttp:
    def _trip(self, as_user):
        as_user("sub-megan")
        out = s.create_trip("June trip", [
            {"date": "2099-06-23", "park": "MK",
             "ride_sequence": [{"ride_name": "Space", "ride_id": "sm"}]},
            {"date": "2099-06-24", "park": "EPCOT"},
        ])
        return out["trip_id"]

    def test_delete_trip_cascades(self, stub, as_user):
        trip_id = self._trip(as_user)
        out = s.delete_trip(trip_id)
        assert out["ok"] is True and out["deleted_days"] == 2 and out["deleted_header"]
        assert not [v for v in stub.items.values() if v.get("trip_id") == trip_id]
        assert ("USER#megan", f"TRIP#{trip_id}") not in stub.items

    def test_delete_trip_refuses_on_outcome(self, stub, as_user):
        trip_id = self._trip(as_user)
        day = next(v for v in stub.items.values() if v.get("trip_id") == trip_id)
        day["outcome_recorded"] = True
        out = s.delete_trip(trip_id)
        assert out["error"] == "Trip has recorded outcomes"
        assert ("USER#megan", f"TRIP#{trip_id}") in stub.items  # untouched

    def test_delete_plan(self, stub, as_user):
        as_user("sub-megan")
        rec = s.record_plan("MK", [], planned_for_date="2099-06-23", active=False)
        out = s.delete_plan(rec["plan_id"])
        assert out["ok"] is True
        assert ("USER#megan", f"PLAN#{rec['plan_id']}") not in stub.items

    def test_update_trip_rename(self, stub, as_user):
        trip_id = self._trip(as_user)
        out = s.update_trip(trip_id, "Renamed trip")
        assert out["ok"] is True
        assert stub.items[("USER#megan", f"TRIP#{trip_id}")]["name"] == "Renamed trip"

    def test_upcoming_trip_derives_added_day(self, stub, as_user):
        trip_id = self._trip(as_user)
        s.record_plan("HS", [], planned_for_date="2099-06-25",
                      trip_id=trip_id, active=False)
        out = s.get_upcoming_trip()
        assert out["found"] is True
        assert "2099-06-25" in [d["date"] for d in out["days"]]
        assert out["end_date"] == "2099-06-25"
