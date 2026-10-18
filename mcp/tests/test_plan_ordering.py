"""Tests for where a ride lands when it's added to an existing plan.

The bug (reported 2026-10-18, after the 10-17 EPCOT day): asking for a
ride at position 3 put it at the END of the plan. `position` was stored
on the entry and the entry was then unconditionally appended, and the
docstring called the field "informational only" — which was true in the
worst way, since NOTHING in the repo ever read it.

add_ride_to_plan had ZERO test coverage before this file, which is how
a parameter that never worked survived to a live trip day.

Three representations of order matter here:
  - ride_sequence array order — the backbone, authoritative
  - plan_order — explicit ride_id ranking from the /replan flow; the
    web read ranks anything MISSING from it dead last, so inserting
    into the array alone does not fix the symptom
  - position — renumbered to match, still read by nobody

DDB is stubbed with a dict-backed table (project convention, no moto).
"""

import os

import pytest

os.environ.setdefault("MCP_PUBLIC_BASE_URL", "https://mcp.example.com")
os.environ.setdefault("COGNITO_USER_POOL_ID", "us-east-2_TESTPOOL")
os.environ.setdefault("COGNITO_REGION", "us-east-2")
os.environ.setdefault("COGNITO_DOMAIN_URL", "https://auth.example.com")

import _tool_impls  # noqa: E402
import server  # noqa: E402
import server_http  # noqa: E402


class _StubTable:
    """Just the surface add_ride_to_plan uses: get_item, put_item, and an
    update_item that understands `SET a = :x, b = :y DELETE s :v` — the
    combined expression this tool writes."""

    def __init__(self):
        self.items: dict[tuple, dict] = {}

    def put_item(self, Item):
        self.items[(Item["PK"], Item["SK"])] = dict(Item)

    def get_item(self, Key):
        it = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(it)} if it else {}

    def update_item(self, Key, UpdateExpression=None,
                    ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None, ConditionExpression=None,
                    ReturnValues=None):
        key = (Key["PK"], Key["SK"])
        if (ConditionExpression and "attribute_exists(PK)" in ConditionExpression
                and key not in self.items):
            from botocore.exceptions import ClientError
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException",
                           "Message": "cond"}},
                "UpdateItem",
            )
        item = self.items.setdefault(key, {"PK": Key["PK"], "SK": Key["SK"]})
        names = ExpressionAttributeNames or {}
        vals = ExpressionAttributeValues or {}
        expr = (UpdateExpression or "").strip()
        assert expr.upper().startswith("SET "), f"unhandled: {expr!r}"
        body = expr[4:]
        delete_part = None
        if " DELETE " in body:
            body, delete_part = body.split(" DELETE ", 1)
        for assign in body.split(","):
            lhs, rhs = assign.split("=")
            attr = lhs.strip()
            item[names.get(attr, attr)] = vals[rhs.strip()]
        if delete_part:
            attr, ref = delete_part.split()
            attr = names.get(attr, attr)
            cur = item.get(attr)
            if isinstance(cur, set):
                item[attr] = cur - set(vals[ref.strip()])
        return {"Attributes": dict(item)}


SK = "PLAN#2026-10-17T09:00:00+00:00"


@pytest.fixture
def stub(monkeypatch):
    t = _StubTable()
    monkeypatch.setattr(server, "_ddb_table", lambda: t)
    monkeypatch.setattr(server_http, "_ddb_table", lambda: t)
    return t


def _seed(stub, mod_name, rides, plan_order=None):
    uid = (
        server._DEFAULT_USER_ID if mod_name == "server"
        else server_http._SHARED_USER_ID
    )
    item = {
        "PK": f"USER#{uid}", "SK": SK,
        "planned_for_date": "2026-10-17",
        "park_key": "epcot", "active": True,
        "ride_sequence": [
            {"ride_name": n, "ride_id": i, "position": p}
            for p, (i, n) in enumerate(rides, start=1)
        ],
    }
    if plan_order is not None:
        item["plan_order"] = list(plan_order)
    stub.put_item(item)
    return (f"USER#{uid}", SK)


def _names(stub, key):
    return [r["ride_name"] for r in stub.items[key]["ride_sequence"]]


def _add(mod_name, **kw):
    mod = server if mod_name == "server" else server_http
    if mod_name == "server_http":
        kw.pop("user_id", None)
    return mod.add_ride_to_plan(**kw)


# ─── the reported bug ───────────────────────────────────────────────


@pytest.mark.parametrize("mod_name", ["server", "server_http"])
class TestAddRideHonorsPosition:
    def test_position_3_lands_third_not_last(self, stub, mod_name):
        """The exact report: asked for position 3, got the end."""
        key = _seed(stub, mod_name, [
            ("caballeros", "Three Caballeros"),
            ("soarin", "Soarin'"),
            ("testtrack", "Test Track"),
            ("guardians", "Guardians"),
        ])
        out = _add(mod_name, plan_id=SK, ride_id="lwtl",
                   ride_name="Living with the Land", position=3)
        assert out["added"] == 1, out
        assert _names(stub, key) == [
            "Three Caballeros", "Soarin'", "Living with the Land",
            "Test Track", "Guardians",
        ]

    def test_position_1_lands_first(self, stub, mod_name):
        key = _seed(stub, mod_name, [("a", "A"), ("b", "B")])
        _add(mod_name, plan_id=SK, ride_id="z", ride_name="Z", position=1)
        assert _names(stub, key) == ["Z", "A", "B"]

    def test_no_position_still_appends(self, stub, mod_name):
        """Unchanged behavior when the caller doesn't care."""
        key = _seed(stub, mod_name, [("a", "A"), ("b", "B")])
        _add(mod_name, plan_id=SK, ride_id="z", ride_name="Z")
        assert _names(stub, key) == ["A", "B", "Z"]

    def test_position_past_the_end_clamps(self, stub, mod_name):
        key = _seed(stub, mod_name, [("a", "A"), ("b", "B")])
        _add(mod_name, plan_id=SK, ride_id="z", ride_name="Z", position=99)
        assert _names(stub, key) == ["A", "B", "Z"]

    def test_position_zero_or_negative_clamps_to_first(self, stub, mod_name):
        key = _seed(stub, mod_name, [("a", "A"), ("b", "B")])
        _add(mod_name, plan_id=SK, ride_id="z", ride_name="Z", position=0)
        assert _names(stub, key) == ["Z", "A", "B"]

    def test_positions_are_renumbered_to_match_order(self, stub, mod_name):
        """The field stops disagreeing with the array — that mismatch is
        what made the original behavior so confusing to debug."""
        key = _seed(stub, mod_name, [("a", "A"), ("b", "B"), ("c", "C")])
        _add(mod_name, plan_id=SK, ride_id="z", ride_name="Z", position=2)
        seq = stub.items[key]["ride_sequence"]
        assert [r["ride_name"] for r in seq] == ["A", "Z", "B", "C"]
        assert [r["position"] for r in seq] == [1, 2, 3, 4]

    def test_empty_plan_accepts_a_positioned_ride(self, stub, mod_name):
        """Zero-state: position 3 into an empty plan clamps to first."""
        key = _seed(stub, mod_name, [])
        _add(mod_name, plan_id=SK, ride_id="z", ride_name="Z", position=3)
        assert _names(stub, key) == ["Z"]

    def test_duplicate_add_is_still_idempotent(self, stub, mod_name):
        key = _seed(stub, mod_name, [("a", "A"), ("b", "B")])
        out = _add(mod_name, plan_id=SK, ride_id="a", ride_name="A",
                   position=1)
        assert out["added"] == 0
        assert _names(stub, key) == ["A", "B"], "no reorder on a no-op add"


# ─── plan_order: the half that array insertion alone doesn't fix ────


@pytest.mark.parametrize("mod_name", ["server", "server_http"])
class TestPlanOrderStaysConsistent:
    def test_ride_is_slotted_into_an_existing_plan_order(self, stub, mod_name):
        """With a plan_order on the row, the web reader ranks any ride
        MISSING from it at MAX_SAFE_INTEGER — dead last — so fixing the
        array alone would leave the symptom exactly as reported. Same
        trap replan/actions.ts hit on 2026-07-04 with Test Track."""
        key = _seed(
            stub, mod_name,
            [("a", "A"), ("b", "B"), ("c", "C")],
            plan_order=["a", "b", "c"],
        )
        _add(mod_name, plan_id=SK, ride_id="z", ride_name="Z", position=2)
        assert stub.items[key]["plan_order"] == ["a", "z", "b", "c"]

    def test_no_plan_order_means_no_plan_order_write(self, stub, mod_name):
        """Rows without an explicit ranking must not grow one — array
        order is authoritative on its own."""
        key = _seed(stub, mod_name, [("a", "A"), ("b", "B")])
        _add(mod_name, plan_id=SK, ride_id="z", ride_name="Z", position=1)
        assert "plan_order" not in stub.items[key]

    def test_array_and_plan_order_agree_after_insert(self, stub, mod_name):
        key = _seed(
            stub, mod_name,
            [("a", "A"), ("b", "B"), ("c", "C")],
            plan_order=["a", "b", "c"],
        )
        _add(mod_name, plan_id=SK, ride_id="z", ride_name="Z", position=3)
        row = stub.items[key]
        assert [r["ride_id"] for r in row["ride_sequence"]] == row["plan_order"]


# ─── the helper in isolation ────────────────────────────────────────


class TestInsertRideAtPosition:
    def _seq(self, *ids):
        return [{"ride_id": i, "ride_name": i.upper()} for i in ids]

    def test_returns_none_plan_order_when_given_none(self):
        seq, order = _tool_impls.insert_ride_at_position(
            self._seq("a", "b"), {"ride_id": "z"}, 1, None
        )
        assert order is None
        assert [r["ride_id"] for r in seq] == ["z", "a", "b"]

    def test_returns_none_when_ride_already_ranked(self):
        """Idempotence at the helper level — no duplicate ranking."""
        _, order = _tool_impls.insert_ride_at_position(
            self._seq("a", "b"), {"ride_id": "a"}, 1, ["a", "b"]
        )
        assert order is None

    def test_does_not_mutate_the_input_list(self):
        original = self._seq("a", "b")
        _tool_impls.insert_ride_at_position(
            original, {"ride_id": "z"}, 1, None
        )
        assert [r["ride_id"] for r in original] == ["a", "b"]

    def test_entry_without_ride_id_leaves_plan_order_alone(self):
        _, order = _tool_impls.insert_ride_at_position(
            self._seq("a"), {"ride_name": "no id"}, 1, ["a"]
        )
        assert order is None
