"""Tests for write-time ride_id resolution and the errors around it.

The bug these cover (2026-10-10): create_trip documented ride_sequence
as an opaque `[...]` and wrote it through verbatim, so a day could be
saved with ride NAMES and no ride_ids. ll_holds is a DDB map KEYED by
ride_id, so set_held_ll could never record a Lightning Lane for such a
ride — all four LLs on the 2026-10-17 EPCOT day were refused, and the
error said "Ride not in plan" even though the name matched fine.

Covered here:
- resolve_ride_sequence_ids: the matching ladder against a park's
  STATE-row catalog, every failure branch, and the no-read fast path.
- Catalog paging: a park whose STATE rows span multiple GSI pages is
  read in full (the MCP-side stubs historically never forced >1 page —
  see the CLAUDE.md coverage standing orders).
- create_trip on BOTH transports: resolves, and fails loud without
  writing anything.
- set_held_ll: an id-less plan ride now reports its own distinct error
  instead of masquerading as "Ride not in plan".
- _ALERTABLE_SCHEDULE_TYPES: party nights count as open hours.

DDB is stubbed with a dict-backed table (project convention — no moto).
"""

import os
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("MCP_PUBLIC_BASE_URL", "https://mcp.example.com")
os.environ.setdefault("COGNITO_USER_POOL_ID", "us-east-2_TESTPOOL")
os.environ.setdefault("COGNITO_REGION", "us-east-2")
os.environ.setdefault("COGNITO_DOMAIN_URL", "https://auth.example.com")

import _tool_impls  # noqa: E402
import server  # noqa: E402  (conftest puts mcp/ on the path)
import server_http  # noqa: E402


# ─── Dict-backed stub table, GSI-aware and pageable ─────────────────


class _StubTable:
    """Stub implementing the surface these paths use: put_item,
    batch_writer, a PK/SK-prefix query, and the park_key-SK-index GSI
    query that the ride catalog read goes through.

    `page_size` forces LastEvaluatedKey paging so the catalog read's
    pagination loop is actually exercised rather than assumed.
    """

    def __init__(self, page_size: int | None = None):
        self.items: dict[tuple, dict] = {}
        self.page_size = page_size
        self.gsi_queries = 0  # how many catalog reads happened

    # -- writes --
    def put_item(self, Item):
        self.items[(Item["PK"], Item["SK"])] = dict(Item)

    def batch_writer(self):
        outer = self

        class _BW:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def put_item(self, Item):
                outer.put_item(Item)

        return _BW()

    # -- reads --
    def get_item(self, Key):
        it = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(it)} if it else {}

    def query(self, IndexName=None, KeyConditionExpression=None,
              ExpressionAttributeValues=None, ScanIndexForward=True,
              Limit=None, ExclusiveStartKey=None):
        vals = ExpressionAttributeValues or {}
        if IndexName:
            # park_key = :p AND SK = :sk  → the STATE-row catalog
            self.gsi_queries += 1
            rows = [
                dict(v) for v in self.items.values()
                if v.get("park_key") == vals[":p"] and v.get("SK") == vals[":sk"]
            ]
            rows.sort(key=lambda r: r["PK"])
        else:
            pk = vals[":pk"]
            prefix = vals.get(":sk", "")
            rows = [
                dict(v) for (p, s), v in self.items.items()
                if p == pk and s.startswith(prefix)
            ]
            rows.sort(key=lambda r: r["SK"], reverse=not ScanIndexForward)

        start = 0
        if ExclusiveStartKey:
            last = ExclusiveStartKey["PK"]
            start = next(
                (i + 1 for i, r in enumerate(rows) if r["PK"] == last), 0
            )
        page = rows[start:]
        if self.page_size is not None and len(page) > self.page_size:
            page = page[: self.page_size]
            return {"Items": page, "LastEvaluatedKey": {"PK": page[-1]["PK"]}}
        if Limit is not None:
            page = page[:Limit]
        return {"Items": page}

    def update_item(self, Key, UpdateExpression=None,
                    ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None, ConditionExpression=None,
                    ReturnValues=None):
        """Handles the shapes these paths use: apply_held_ll's ll_holds
        map updates (ensure-map / set-one-key / remove-one-key), plus a
        generic comma-separated `SET attr = :val` for activate_plan.

        The ll_holds branches must stay ahead of the generic SET branch —
        `SET ll_holds = if_not_exists(...)` also starts with "SET ".
        """
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
        if expr.startswith("SET ll_holds = if_not_exists"):
            item.setdefault("ll_holds", dict(vals[":empty"]))
        elif expr.startswith("SET ll_holds.#r"):
            item.setdefault("ll_holds", {})[names["#r"]] = vals[":t"]
        elif expr.startswith("REMOVE ll_holds.#r"):
            item.get("ll_holds", {}).pop(names["#r"], None)
        elif expr.upper().startswith("SET "):
            for assign in expr[4:].split(","):
                lhs, rhs = assign.split("=")
                attr = lhs.strip()
                item[names.get(attr, attr)] = vals[rhs.strip()]
        else:  # pragma: no cover - guard against silent test drift
            raise AssertionError(f"stub can't handle: {expr!r}")
        return {"Attributes": dict(item)}

    # -- test helpers --
    def plan_and_trip_rows(self) -> list[dict]:
        """Everything create_trip would have written — deliberately
        excludes the pre-loaded RIDE#/STATE catalog rows."""
        return [
            v for (p, s), v in self.items.items()
            if s.startswith("TRIP#") or s.startswith("PLAN#")
        ]

    def add_ride(self, ride_id: str, name: str, park_key: str):
        self.put_item({
            "PK": f"RIDE#{ride_id}", "SK": "STATE",
            "ride_id": ride_id, "name": name, "park_key": park_key,
            "status": "OPERATING", "wait_mins": 30,
        })


EPCOT_RIDES = [
    ("remy", "Remy's Ratatouille Adventure"),
    ("frozen", "Frozen Ever After"),
    ("lwtl", "Living with the Land"),
    ("sse", "Spaceship Earth"),
    ("soarin", "Soarin' Around the World"),
    ("guardians", "Guardians of the Galaxy: Cosmic Rewind"),
    ("testtrack", "Test Track"),
    ("missionspace", "Mission: SPACE"),
]


@pytest.fixture
def stub(monkeypatch):
    t = _StubTable()
    for rid, name in EPCOT_RIDES:
        t.add_ride(rid, name, "epcot")
    t.add_ride("sm", "Space Mountain", "magic_kingdom")
    monkeypatch.setattr(server, "_ddb_table", lambda: t)
    monkeypatch.setattr(server_http, "_ddb_table", lambda: t)
    return t


def _future(days: int) -> str:
    d = datetime.fromisoformat(server._today_et_date_iso()).date() + timedelta(
        days=days
    )
    return d.isoformat()


# ─── resolve_ride_sequence_ids ──────────────────────────────────────


class TestResolveRideSequenceIds:
    def test_fills_missing_ids_by_name(self, stub):
        seq = [
            {"ride_name": "Remy's Ratatouille Adventure", "position": 1},
            {"ride_name": "Frozen Ever After", "position": 2},
        ]
        assert _tool_impls.resolve_ride_sequence_ids(stub, seq, "epcot") is None
        assert [r["ride_id"] for r in seq] == ["remy", "frozen"]

    def test_no_catalog_read_when_every_id_present(self, stub):
        seq = [{"ride_name": "Frozen Ever After", "ride_id": "frozen"}]
        assert _tool_impls.resolve_ride_sequence_ids(stub, seq, "epcot") is None
        assert stub.gsi_queries == 0, "fully-specified sequence must do no reads"

    def test_resolves_only_the_missing_entries(self, stub):
        seq = [
            {"ride_name": "WRONG NAME ENTIRELY", "ride_id": "sse"},
            {"ride_name": "Test Track"},
        ]
        assert _tool_impls.resolve_ride_sequence_ids(stub, seq, "epcot") is None
        # the entry that already had an id keeps it, name notwithstanding
        assert seq[0]["ride_id"] == "sse"
        assert seq[1]["ride_id"] == "testtrack"

    def test_punctuation_insensitive_match(self, stub):
        seq = [{"ride_name": "mission space"}]
        assert _tool_impls.resolve_ride_sequence_ids(stub, seq, "epcot") is None
        assert seq[0]["ride_id"] == "missionspace"

    def test_unique_partial_match(self, stub):
        seq = [{"ride_name": "Guardians"}]
        assert _tool_impls.resolve_ride_sequence_ids(stub, seq, "epcot") is None
        assert seq[0]["ride_id"] == "guardians"

    def test_ambiguous_name_errors_and_mutates_nothing(self, stub):
        # "track" is unique, but "t" hits Test Track + others
        seq = [{"ride_name": "the"}]
        err = _tool_impls.resolve_ride_sequence_ids(stub, seq, "epcot")
        assert err["error"] == "Ambiguous ride"
        assert "nothing was saved" in err["error_message"]
        assert "ride_id" not in seq[0]

    def test_unknown_name_errors(self, stub):
        seq = [{"ride_name": "Tower of Terror"}]
        err = _tool_impls.resolve_ride_sequence_ids(stub, seq, "epcot")
        assert err["error"] == "Unknown ride"
        assert "ride_id" not in seq[0]

    def test_ride_in_another_park_does_not_match(self, stub):
        """Space Mountain exists, but not at EPCOT — park-scoped catalog."""
        seq = [{"ride_name": "Space Mountain"}]
        err = _tool_impls.resolve_ride_sequence_ids(stub, seq, "epcot")
        assert err["error"] == "Unknown ride"

    def test_entry_with_neither_name_nor_id_errors(self, stub):
        seq = [{"position": 1}]
        err = _tool_impls.resolve_ride_sequence_ids(stub, seq, "epcot")
        assert err["error"] == "Ride needs a name or id"

    def test_blank_id_counts_as_missing(self, stub):
        seq = [{"ride_name": "Test Track", "ride_id": "   "}]
        assert _tool_impls.resolve_ride_sequence_ids(stub, seq, "epcot") is None
        assert seq[0]["ride_id"] == "testtrack"

    def test_empty_catalog_errors(self, monkeypatch):
        empty = _StubTable()
        seq = [{"ride_name": "Test Track"}]
        err = _tool_impls.resolve_ride_sequence_ids(empty, seq, "epcot")
        assert err["error"] == "Ride catalog unavailable"

    def test_empty_sequence_is_a_noop(self, stub):
        assert _tool_impls.resolve_ride_sequence_ids(stub, [], "epcot") is None
        assert stub.gsi_queries == 0

    def test_catalog_read_follows_every_gsi_page(self, monkeypatch):
        """8 EPCOT rides at 3 per page — the last ride is only reachable
        by following LastEvaluatedKey. Without the pagination loop the
        name resolves to nothing and this goes red."""
        paged = _StubTable(page_size=3)
        for rid, name in EPCOT_RIDES:
            paged.add_ride(rid, name, "epcot")
        catalog = _tool_impls._park_state_rows_via_gsi(paged, "epcot")
        assert len(catalog) == len(EPCOT_RIDES)
        # and resolution works for a ride beyond the first page
        seq = [{"ride_name": "Mission: SPACE"}]
        assert _tool_impls.resolve_ride_sequence_ids(paged, seq, "epcot") is None
        assert seq[0]["ride_id"] == "missionspace"


# ─── create_trip, both transports ───────────────────────────────────


@pytest.mark.parametrize("mod_name", ["server", "server_http"])
class TestCreateTripResolvesIds:
    def _create(self, mod_name, *args):
        mod = server if mod_name == "server" else server_http
        return mod.create_trip(*args)

    def test_name_only_day_gets_ids(self, stub, mod_name):
        """The 2026-10-17 regression: a day built from names alone must
        come out of create_trip with every ride_id populated, so the
        Lightning Lanes can actually be recorded afterwards."""
        days = [{
            "date": _future(7), "park": "EPCOT",
            "ride_sequence": [
                {"ride_name": "Remy's Ratatouille Adventure", "position": 1},
                {"ride_name": "Frozen Ever After", "position": 2},
                {"ride_name": "Spaceship Earth", "position": 3},
                {"ride_name": "Soarin' Around the World", "position": 4},
                {"ride_name": "Guardians of the Galaxy: Cosmic Rewind",
                 "position": 5},
                {"ride_name": "Test Track", "position": 6},
            ],
        }]
        out = self._create(mod_name, "Oct trip", days)
        assert "error" not in out, out
        plan = next(
            v for (p, s), v in stub.items.items() if s.startswith("PLAN#")
        )
        ids = [r.get("ride_id") for r in plan["ride_sequence"]]
        assert all(ids), f"every ride needs an id, got {ids}"
        assert ids == ["remy", "frozen", "sse", "soarin", "guardians",
                       "testtrack"]

    def test_unknown_ride_writes_nothing(self, stub, mod_name):
        days = [{
            "date": _future(7), "park": "EPCOT",
            "ride_sequence": [{"ride_name": "Haunted Mansion"}],
        }]
        out = self._create(mod_name, "Bad trip", days)
        assert out["error"] == "Unknown ride"
        assert "day 0" in out["error_message"]
        assert stub.plan_and_trip_rows() == [], \
            "no partial trip may survive an error"

    def test_second_day_failure_writes_nothing(self, stub, mod_name):
        """Validation happens for ALL days before any write — a bad ride
        on day 2 must not leave day 1 on the table."""
        days = [
            {"date": _future(7), "park": "EPCOT",
             "ride_sequence": [{"ride_name": "Test Track"}]},
            {"date": _future(8), "park": "EPCOT",
             "ride_sequence": [{"ride_name": "Nonexistent Ride"}]},
        ]
        out = self._create(mod_name, "Two day", days)
        assert out["error"] == "Unknown ride"
        assert "day 1" in out["error_message"]
        assert stub.plan_and_trip_rows() == []

    def test_day_without_rides_still_works(self, stub, mod_name):
        days = [{"date": _future(7), "park": "EPCOT"}]
        out = self._create(mod_name, "Open day", days)
        assert "error" not in out, out
        assert stub.gsi_queries == 0


# ─── set_held_ll's distinct id-less error ───────────────────────────


class TestSetHeldLlIdLessRide:
    def _plan_with(self, stub, ride_sequence, date_iso):
        stub.put_item({
            "PK": f"USER#{server_http._SHARED_USER_ID}",
            "SK": f"PLAN#{date_iso}T09:00:00+00:00",
            "planned_for_date": date_iso,
            "planned_at": f"{date_iso}T09:00:00+00:00",
            "park_key": "epcot",
            "active": True,
            "ride_sequence": ride_sequence,
        })

    def test_idless_ride_reports_its_own_error(self, stub):
        """The name matches a plan ride; only the id is missing. That
        must NOT come back as 'Ride not in plan' — reporting it that way
        sent the 2026-10-17 debugging down a matching-logic dead end."""
        d = server_http._today_et_date_iso()
        self._plan_with(stub, [{"ride_name": "Test Track", "position": 1}], d)
        out = server_http.set_held_ll("Test Track", "3:00 PM")
        assert out["error"] == "Plan ride has no ride_id"
        assert "keyed by ride_id" in out["error_message"]
        assert "record_plan" in out["error_message"]

    def test_genuinely_absent_ride_still_says_not_in_plan(self, stub):
        d = server_http._today_et_date_iso()
        self._plan_with(stub, [{"ride_name": "Test Track", "ride_id": "tt"}], d)
        out = server_http.set_held_ll("Frozen Ever After", "3:00 PM")
        assert out["error"] == "Ride not in plan"

    def test_ride_with_id_sets_the_hold(self, stub):
        d = server_http._today_et_date_iso()
        self._plan_with(
            stub, [{"ride_name": "Test Track", "ride_id": "testtrack"}], d
        )
        out = server_http.set_held_ll("Test Track", "3:00 PM")
        assert "error" not in out, out
        assert out["ride_id"] == "testtrack"


# ─── party nights count as open hours ───────────────────────────────


class TestAlertableScheduleTypes:
    def test_ticketed_event_is_alertable(self):
        assert "TICKETED_EVENT" in _tool_impls._ALERTABLE_SCHEDULE_TYPES

    def test_matches_the_poller_tuple(self):
        """This module's contract is that the planner sees the same hours
        the alert filter uses; drift between the two is what produced the
        6pm-close bug on party nights."""
        import pathlib
        import re
        src = (
            pathlib.Path(__file__).resolve().parents[2]
            / "infra/lambda/poller/wait_times.py"
        ).read_text()
        m = re.search(r"ALERTABLE_SCHEDULE_TYPES = \(([^)]*)\)", src)
        poller = tuple(
            x.strip().strip('"\'') for x in m.group(1).split(",") if x.strip()
        )
        assert poller == _tool_impls._ALERTABLE_SCHEDULE_TYPES

    def test_party_night_close_is_the_party_close(self, monkeypatch):
        """MK on a party night: OPERATING 9-6 plus TICKETED_EVENT 7-12.
        The reported close must be midnight, not 6pm."""
        import requests
        today = datetime.now(_tool_impls._EASTERN).date().isoformat()

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"schedule": [
                    {"date": today, "type": "OPERATING",
                     "openingTime": f"{today}T09:00:00-04:00",
                     "closingTime": f"{today}T18:00:00-04:00"},
                    {"date": today, "type": "TICKETED_EVENT",
                     "openingTime": f"{today}T19:00:00-04:00",
                     "closingTime": f"{today}T23:59:00-04:00"},
                ]}

        monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
        out = _tool_impls._fetch_park_hours_today("magic_kingdom")
        assert out is not None
        assert out["close"].startswith(f"{today}T23:59")
        assert out["open"].startswith(f"{today}T09:00")


# ─── record_plan: the same door into the same bug ───────────────────


@pytest.mark.parametrize("mod_name", ["server", "server_http"])
class TestRecordPlanResolvesIds:
    """record_plan's docstring has always ASKED for ride_id, and that ask
    is what failed on 2026-10-17 — create_trip was fixed first, but a
    name-only plan could still be written straight through record_plan.
    An ask is not enforcement."""

    def _record(self, mod_name, **kw):
        mod = server if mod_name == "server" else server_http
        if mod_name == "server_http":
            kw.pop("user_id", None)
        return mod.record_plan(**kw)

    def _plan_row(self, stub):
        return next(
            v for (p, s), v in stub.items.items() if s.startswith("PLAN#")
        )

    def test_name_only_rides_get_ids(self, stub, mod_name):
        out = self._record(
            mod_name,
            park="EPCOT",
            ride_sequence=[
                {"ride_name": "Test Track", "position": 1},
                {"ride_name": "Frozen Ever After", "position": 2},
            ],
            planned_for_date=_future(3),
        )
        assert "error" not in out, out
        ids = [r.get("ride_id") for r in self._plan_row(stub)["ride_sequence"]]
        assert ids == ["testtrack", "frozen"]

    def test_name_only_rides_WITH_holds_now_succeed(self, stub, mod_name):
        """Before this fix resolve_ll_holds rejected the call outright:
        the hold's ride matched by name but had no ride_id to key the
        ll_holds map by. Resolving ids first turns that into a success."""
        out = self._record(
            mod_name,
            park="EPCOT",
            ride_sequence=[{"ride_name": "Test Track", "position": 1}],
            ll_holds={"Test Track": "3:00 PM"},
            planned_for_date=_future(3),
        )
        assert "error" not in out, out
        row = self._plan_row(stub)
        assert list(row["ll_holds"].keys()) == ["testtrack"]
        assert row["ll_holds"]["testtrack"].startswith(f"{_future(3)}T15:00")

    def test_unknown_ride_writes_nothing(self, stub, mod_name):
        out = self._record(
            mod_name,
            park="EPCOT",
            ride_sequence=[{"ride_name": "Haunted Mansion", "position": 1}],
            planned_for_date=_future(3),
        )
        assert out["error"] == "Unknown ride"
        assert stub.plan_and_trip_rows() == []

    def test_ambiguous_ride_writes_nothing(self, stub, mod_name):
        out = self._record(
            mod_name,
            park="EPCOT",
            ride_sequence=[{"ride_name": "the", "position": 1}],
            planned_for_date=_future(3),
        )
        assert out["error"] == "Ambiguous ride"
        assert stub.plan_and_trip_rows() == []

    def test_ids_already_present_does_no_catalog_read(self, stub, mod_name):
        out = self._record(
            mod_name,
            park="EPCOT",
            ride_sequence=[{"ride_name": "Test Track", "ride_id": "testtrack"}],
            planned_for_date=_future(3),
        )
        assert "error" not in out, out
        assert stub.gsi_queries == 0

    def test_empty_sequence_still_records(self, stub, mod_name):
        """The zero-state case: an empty plan is legal and must not be
        turned into an error by the resolver."""
        out = self._record(
            mod_name, park="EPCOT", ride_sequence=[],
            planned_for_date=_future(3),
        )
        assert "error" not in out, out
        assert stub.gsi_queries == 0


# ─── activate_plan: the dangerous door (it REGRESSES a good plan) ───


@pytest.mark.parametrize("mod_name", ["server", "server_http"])
class TestActivatePlanPreservesIds:
    """activate_plan overwrites ride_sequence wholesale, on the morning
    of the trip. A name-only re-evaluation there doesn't just fail to
    add ids — it STRIPS ids a correct plan already had, making every
    ride invisible to the poller and orphaning ll_holds (still keyed by
    ride_id). This is the 2026-10-17 shape at its worst."""

    def _seed(self, stub, mod_name, ride_sequence, ll_holds=None):
        uid = (
            server._DEFAULT_USER_ID if mod_name == "server"
            else server_http._SHARED_USER_ID
        )
        today = server._today_et_date_iso()
        sk = f"PLAN#{today}T09:00:00+00:00"
        item = {
            "PK": f"USER#{uid}", "SK": sk,
            "planned_for_date": today,
            "planned_at": f"{today}T09:00:00+00:00",
            "park_key": "epcot",
            "active": False,
            "ride_sequence": ride_sequence,
        }
        if ll_holds:
            item["ll_holds"] = ll_holds
        stub.put_item(item)
        return sk, (f"USER#{uid}", sk)

    def _activate(self, mod_name, **kw):
        mod = server if mod_name == "server" else server_http
        return mod.activate_plan(**kw)

    def test_name_only_reevaluation_gets_ids(self, stub, mod_name):
        sk, key = self._seed(
            stub, mod_name, [{"ride_name": "Test Track", "ride_id": "testtrack"}]
        )
        out = self._activate(
            mod_name, plan_id=sk,
            ride_sequence=[
                {"ride_name": "Frozen Ever After", "position": 1},
                {"ride_name": "Test Track", "position": 2},
            ],
        )
        assert "error" not in out, out
        ids = [r.get("ride_id") for r in stub.items[key]["ride_sequence"]]
        assert ids == ["frozen", "testtrack"], "ids must survive activation"

    def test_holds_are_not_orphaned(self, stub, mod_name):
        """The Oct 17 scenario: a plan with ids AND four-ish holds gets
        re-evaluated by name on the day. The holds stay keyed by
        ride_id, so if activation stripped the ids every hold would
        point at a ride no longer in the sequence."""
        holds = {"testtrack": "2026-10-17T15:25:00-04:00",
                 "frozen": "2026-10-17T10:25:00-04:00"}
        sk, key = self._seed(
            stub, mod_name,
            [{"ride_name": "Test Track", "ride_id": "testtrack"},
             {"ride_name": "Frozen Ever After", "ride_id": "frozen"}],
            ll_holds=holds,
        )
        out = self._activate(
            mod_name, plan_id=sk,
            ride_sequence=[
                {"ride_name": "Test Track", "position": 1},
                {"ride_name": "Frozen Ever After", "position": 2},
            ],
        )
        assert "error" not in out, out
        row = stub.items[key]
        seq_ids = {r.get("ride_id") for r in row["ride_sequence"]}
        orphans = set(row["ll_holds"]) - seq_ids
        assert orphans == set(), f"holds orphaned by activation: {orphans}"

    def test_unknown_ride_does_not_activate_or_overwrite(self, stub, mod_name):
        sk, key = self._seed(
            stub, mod_name, [{"ride_name": "Test Track", "ride_id": "testtrack"}]
        )
        out = self._activate(
            mod_name, plan_id=sk,
            ride_sequence=[{"ride_name": "Haunted Mansion", "position": 1}],
        )
        assert out["error"] == "Unknown ride"
        row = stub.items[key]
        assert row["active"] is False, "must not activate on a bad sequence"
        assert row["ride_sequence"][0]["ride_id"] == "testtrack", \
            "original sequence must be untouched"

    def test_activation_without_ride_sequence_still_works(self, stub, mod_name):
        """The recovery path the error message points at — and proof the
        resolver doesn't do a catalog read when there's nothing to
        resolve."""
        sk, key = self._seed(
            stub, mod_name, [{"ride_name": "Test Track", "ride_id": "testtrack"}]
        )
        out = self._activate(mod_name, plan_id=sk)
        assert "error" not in out, out
        assert stub.items[key]["active"] is True
        assert stub.gsi_queries == 0

    def test_missing_park_key_refuses_rather_than_degrading(
        self, stub, mod_name
    ):
        uid = (
            server._DEFAULT_USER_ID if mod_name == "server"
            else server_http._SHARED_USER_ID
        )
        today = server._today_et_date_iso()
        sk = f"PLAN#{today}T09:00:00+00:00"
        stub.put_item({
            "PK": f"USER#{uid}", "SK": sk,
            "planned_for_date": today, "active": False,
            "ride_sequence": [{"ride_name": "Test Track", "ride_id": "tt"}],
        })  # deliberately no park_key
        out = self._activate(
            mod_name, plan_id=sk,
            ride_sequence=[{"ride_name": "Test Track", "position": 1}],
        )
        assert out["error"] == "Cannot resolve plan's park"
        assert "without ride_sequence" in out["error_message"]
