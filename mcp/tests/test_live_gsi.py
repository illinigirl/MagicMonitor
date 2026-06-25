"""Tests for the live read tools' GSI migration (2026-06-24).

get_park_live_status, get_live_ride_status, _resolve_ride_via_ddb, and
_fetch_park_currently_down used a full-table Scan+FilterExpression to find
~150 STATE rows — O(table size), ~20s once the table passed 3.5M rows and
nearing the 30s API Gateway cap. They now Query the park_key-SK-index GSI
(per-park partition reads), O(rides-in-park).

The stub's query() honors the GSI key condition; its scan() RAISES — so
any test that still hit a scan path would fail loudly. That's the
guarantee: the live read tools never scan the table again.
"""

import os

os.environ.setdefault("MCP_PUBLIC_BASE_URL", "https://mcp.example.com")
os.environ.setdefault("COGNITO_USER_POOL_ID", "us-east-2_TESTPOOL")
os.environ.setdefault("COGNITO_REGION", "us-east-2")
os.environ.setdefault("COGNITO_DOMAIN_URL", "https://auth.example.com")

import _tool_impls  # noqa: E402
import server_http as s  # noqa: E402


def _state(park, name, rid, status="OPERATING", wait=20):
    return {
        "PK": f"RIDE#{rid}", "SK": "STATE", "park_key": park,
        "name": name, "ride_id": rid, "park_name": park.replace("_", " ").title(),
        "status": status, "wait_mins": wait, "ll": None, "last_seen": "2026-06-24T12:00:00Z",
    }


# Three STATE rows across two parks + a WAIT# noise row the GSI query
# (SK="STATE") must NOT return.
ROWS = [
    _state("magic_kingdom", "Space Mountain", "sm"),
    _state("magic_kingdom", "Big Thunder Mountain Railroad", "bt", status="DOWN"),
    _state("epcot", "Test Track", "tt"),
    {"PK": "RIDE#sm", "SK": "WAIT#2026-06-24T12:00:00Z", "park_key": "magic_kingdom"},
]


class _GsiStub:
    """Honors the park_key-SK-index Query; scan() raises to prove no scan."""

    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def query(self, IndexName=None, KeyConditionExpression=None,
              ExpressionAttributeValues=None, ExclusiveStartKey=None, **kw):
        assert IndexName == "park_key-SK-index", f"expected GSI, got {IndexName!r}"
        self.queries.append(ExpressionAttributeValues)
        p = ExpressionAttributeValues[":p"]
        sk = ExpressionAttributeValues[":sk"]
        items = [r for r in self.rows if r.get("park_key") == p and r.get("SK") == sk]
        return {"Items": items}

    def scan(self, **kw):
        raise AssertionError("scan() called — the live tools must use the GSI now")


def test_park_state_rows_via_gsi_returns_only_that_parks_state():
    stub = _GsiStub(ROWS)
    rows = _tool_impls._park_state_rows_via_gsi(stub, "magic_kingdom")
    assert {r["name"] for r in rows} == {"Space Mountain", "Big Thunder Mountain Railroad"}
    # WAIT# noise excluded (SK != STATE); one GSI query for the one park.
    assert stub.queries == [{":p": "magic_kingdom", ":sk": "STATE"}]


def test_all_park_state_rows_queries_each_park_once():
    stub = _GsiStub(ROWS)
    rows = _tool_impls._all_park_state_rows_via_gsi(stub)
    assert {r["name"] for r in rows} == {
        "Space Mountain", "Big Thunder Mountain Railroad", "Test Track"
    }
    assert len(stub.queries) == 4  # one partition query per park, no scan


def test_fetch_park_currently_down_filters_to_down():
    stub = _GsiStub(ROWS)
    out = _tool_impls._fetch_park_currently_down(stub, "magic_kingdom")
    assert [d["ride_name"] for d in out] == ["Big Thunder Mountain Railroad"]


def test_get_park_live_status_uses_gsi(monkeypatch):
    stub = _GsiStub(ROWS)
    monkeypatch.setattr(s, "_ddb_table", lambda: stub)
    out = s.get_park_live_status("epcot")
    assert "error" not in out
    # EPCOT-only via the GSI; scan() would have raised.
    assert stub.queries == [{":p": "epcot", ":sk": "STATE"}]


def test_get_live_ride_status_resolves_by_name_across_parks(monkeypatch):
    stub = _GsiStub(ROWS)
    monkeypatch.setattr(s, "_ddb_table", lambda: stub)
    out = s.get_live_ride_status("space mountain")
    assert out["ride_name"] == "Space Mountain"
    assert out["status"] == "OPERATING"
    assert len(stub.queries) == 4  # 4 per-park GSI queries, no scan
