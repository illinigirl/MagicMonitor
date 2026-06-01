"""Magic Monitor MCP — HTTPS transport (v1).

Companion to `server.py` (stdio transport for Claude Desktop). Both
expose the same tool semantics, but this file is the read-side
duplicate that runs in AWS Lambda behind API Gateway, so Claude
mobile (which only supports remote MCP servers) can hit the same
data plane.

**Duplicate-first by design.** Per the M9 Phase 1 rollout strategy
(PROJECT.md), this file is a verbatim copy of the v1 tool subset
from server.py rather than a refactor. The stdio path stays
bit-for-bit identical so the working Claude Desktop demo carries
zero regression risk from this work. Consolidating the two into a
shared `_tool_impls.py` is later cleanup once the HTTPS path is
proven on mobile.

**Scope (read-only, no writes).** Session 2.5 brings the HTTP tool
surface to parity with server.py's read side:
- Live DDB tools: `get_live_ride_status`, `get_park_live_status`,
  `get_ride_forecast`, `get_ride_downtime_today`.
- Snapshot analytics tools (S3-backed, see below): `get_park_heatmap`,
  `get_ride_analytics`, `get_ride_dow_pattern`, `get_ride_down_clusters`,
  `get_ride_ll_drops`, `get_short_wait_baseline`, `find_rides_matching`.
- Bundled-data / egress tools: `get_party_calendar`, `get_mll_tiers`,
  `get_park_showtimes`.

The write-side plan-feedback loop (record_plan / mark_ride_complete /
record_plan_outcome) and the heavyweight `get_planning_context` follow
in later sessions once write-side IAM lands.

**Analytics snapshot delivery (S3, not bundled).** The 1.2MB analytics
snapshot + short-wait baselines are regenerated nightly by the
aggregator and uploaded to S3 (see `_snapshot()` below). The Lambda
fetches them lazily on the first analytics tool call and caches for the
container lifetime. S3 — rather than bundling into the asset — keeps
the data's nightly update cadence decoupled from the code's (rare)
deploy cadence: a cold start picks up the latest nightly regen with no
redeploy. The live DDB tools are always current regardless. If S3 is
unreachable and nothing is cached, snapshot-backed tools return a
graceful "temporarily unavailable" payload; the live + bundled-data
tools are unaffected.

**Auth (session 2B).** Cognito access tokens, verified per-request
against the user pool's JWKS, gated by a hard-coded sub allowlist.
Public OAuth discovery + Dynamic Client Registration endpoints are
handled inside the middleware (no auth gate, by spec). See
`jwt_verifier.py` and `dcr_proxy.py` for the verifier + DCR proxy.

The earlier shared-bearer-secret middleware was hard-replaced in
this session; no dual-auth path exists. The SSM bearer parameter
is removed in a follow-up commit once the OAuth path is verified
end-to-end.

**Stateless.** Lambda doesn't keep state across invocations, so
the streamable-HTTP transport must run in stateless mode — each
request is self-contained and doesn't rely on a server-side
session. FastMCP supports this via the `stateless_http=True`
setting; we pass it at construction.
"""

import json
import os
import re as _re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

import dcr_proxy
import jwt_verifier

# ─── Config / constants ─────────────────────────────────────────────
# Mirror server.py's DDB region pin. In Lambda there's no profile —
# boto3 uses the execution role's credentials via the default chain.
_DDB_REGION = os.environ.get("DISNEY_REGION", "us-east-2")
_DDB_TABLE = os.environ.get("DISNEY_TABLE_NAME", "DisneyData")

# ─── DDB lazy table accessor ────────────────────────────────────────
# Mirrors server.py's lazy pattern: don't pay for boto3 + session
# construction until a tool that actually reads DDB runs. In Lambda
# this means cold-start cost lands on the first request, not on
# every container start.

_table = None


def _ddb_table():
    """Lazy-init the DDB table resource on first live-data tool call."""
    global _table
    if _table is None:
        import boto3
        # No profile_name — Lambda execution role provides creds via
        # the default chain. Same module would also work locally if
        # AWS_PROFILE / SSO is set.
        session = boto3.Session(region_name=_DDB_REGION)
        _table = session.resource("dynamodb").Table(_DDB_TABLE)
    return _table


def _convert_decimals(obj: Any) -> Any:
    """Recursively convert boto3 Decimals to int/float for JSON output.

    Verbatim from server.py — DDB returns Decimals which JSON can't
    serialize. We convert back to int when whole, float otherwise.
    """
    from decimal import Decimal
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: _convert_decimals(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_convert_decimals(v) for v in obj]
    return obj


def _aws_error_payload(e: Exception) -> dict[str, Any] | None:
    """Friendly error dict for AWS-auth failures, or None if not auth.

    Verbatim shape from server.py, with the user-facing hints adjusted
    for the Lambda context: SSO refresh / profile guidance doesn't
    apply when the runtime is API Gateway → Lambda → IAM role.
    """
    msg = str(e)
    if "Token has expired" in msg or "ExpiredToken" in msg:
        return {
            "error": "AWS credentials expired",
            "error_hint": "Lambda execution role credentials should auto-refresh; if this persists check role policy.",
        }
    if "InvalidClientTokenId" in msg or "UnrecognizedClientException" in msg:
        return {
            "error": "AWS credentials not recognized",
            "error_hint": "Lambda execution role can't read this DDB table — check IAM policy attached to the role.",
        }
    return None


_PARK_KEYS = {"magic_kingdom", "epcot", "hollywood_studios", "animal_kingdom"}


def _normalize_park(park: str) -> str:
    """Accept 'Magic Kingdom' / 'mk' / 'MK' etc, return canonical key.

    Verbatim from server.py.
    """
    p = park.strip().lower().replace(" ", "_").replace("-", "_")
    if p in _PARK_KEYS:
        return p
    aliases = {
        "mk": "magic_kingdom",
        "magickingdom": "magic_kingdom",
        "ep": "epcot",
        "hs": "hollywood_studios",
        "dhs": "hollywood_studios",
        "hollywoodstudios": "hollywood_studios",
        "ak": "animal_kingdom",
        "animalkingdom": "animal_kingdom",
    }
    if p.replace("_", "") in aliases:
        return aliases[p.replace("_", "")]
    raise ValueError(
        f"Unknown park '{park}'. Use one of: "
        f"{', '.join(sorted(_PARK_KEYS))} (or aliases like MK, EP, HS, AK)."
    )


# ─── Analytics snapshot (S3-backed) + reference data ────────────────
# The 1.2MB analytics snapshot and the short-wait baselines are
# regenerated nightly by the aggregator (.github/workflows/aggregate.yml)
# and uploaded to S3. We fetch them lazily on the first analytics tool
# call and cache in module globals for the container lifetime. See the
# module docstring for the why-S3-not-bundled rationale.
_SNAPSHOT_BUCKET = os.environ.get("MCP_SNAPSHOT_BUCKET", "")
_SNAPSHOT_KEY = os.environ.get("MCP_SNAPSHOT_KEY", "analytics-snapshot.json")
_BASELINES_KEY = os.environ.get("MCP_BASELINES_KEY", "baselines.json")

# Park-day boundary + ET zone, mirroring server.py so the live downtime
# tool agrees with the historical heatmap (a 1-3am poll counts as the
# previous park-day, not the current calendar day).
_PARK_DAY_BOUNDARY_HOUR = 4
_EASTERN = ZoneInfo("America/New_York")
# HIST# rows TTL at 90 days in the poller; queries past that return empty.
_HIST_RETENTION_DAYS = 90

# SQLite-style day-of-week (Sun=0..Sat=6) — matches the heatmap data.
_DOW_NAMES = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
_DOW_INDEX = {name: i for i, name in enumerate(_DOW_NAMES)}

# Static reference data bundled into the asset — mcp/data/ ships as a
# sibling of this module (the CDK bundler copies the whole mcp/ tree).
# Unlike the snapshot these change rarely and are small, so deploy-time
# freshness is fine; no need for the S3 round-trip.
_DATA_DIR = Path(__file__).resolve().parent / "data"
_MLL_TIERS_PATH = _DATA_DIR / "mll_tiers.json"
_PARTY_CALENDAR_PATH = _DATA_DIR / "party_calendar.json"

# Module-level caches. None = not yet fetched this container lifetime.
_snapshot_cache: dict[str, Any] | None = None
_baselines_cache: dict[str, Any] | None = None
_party_calendar_cache: dict[str, Any] | None = None
_mll_tiers_cache: dict[str, Any] | None = None


class _SnapshotUnavailable(Exception):
    """The analytics snapshot/baselines couldn't be loaded from S3 and
    nothing is cached. Snapshot-backed tools catch this and return a
    graceful 'temporarily unavailable' payload rather than 500ing — the
    live DDB tools and the bundled-data tools are unaffected."""


def _s3_get_json(key: str) -> Any:
    """Fetch + JSON-parse one object from the analytics data bucket."""
    if not _SNAPSHOT_BUCKET:
        raise _SnapshotUnavailable("MCP_SNAPSHOT_BUCKET not configured")
    import boto3  # Lambda runtime provides boto3; it's stripped from the asset.
    s3 = boto3.client("s3", region_name=_DDB_REGION)
    obj = s3.get_object(Bucket=_SNAPSHOT_BUCKET, Key=key)
    return json.loads(obj["Body"].read())


def _snapshot() -> dict[str, Any]:
    """Return the analytics snapshot, fetching from S3 on first use.

    Cached for the container lifetime — a warm container reuses it; a
    cold start (after idle) re-fetches, which is how the nightly regen
    reaches the Lambda with no redeploy. Raises _SnapshotUnavailable if
    S3 can't be reached and nothing is cached yet.
    """
    global _snapshot_cache
    if _snapshot_cache is None:
        try:
            _snapshot_cache = _s3_get_json(_SNAPSHOT_KEY)
        except _SnapshotUnavailable:
            raise
        except Exception as e:
            raise _SnapshotUnavailable(f"snapshot fetch failed: {e}") from e
    return _snapshot_cache


def _baselines() -> dict[str, Any]:
    """Return the short-wait baselines, fetching from S3 on first use."""
    global _baselines_cache
    if _baselines_cache is None:
        try:
            _baselines_cache = _s3_get_json(_BASELINES_KEY)
        except _SnapshotUnavailable:
            raise
        except Exception as e:
            raise _SnapshotUnavailable(f"baselines fetch failed: {e}") from e
    return _baselines_cache


def _snapshot_unavailable_payload() -> dict[str, Any]:
    """Graceful response when the snapshot can't be loaded from S3."""
    return {
        "error": "analytics temporarily unavailable",
        "error_hint": (
            "The analytics snapshot couldn't be loaded from S3 right now. "
            "Live tools (get_live_ride_status, get_park_live_status, "
            "get_ride_forecast, get_ride_downtime_today) and the "
            "party-calendar / MLL-tier / showtimes tools are unaffected. "
            "Retry the analytics tool shortly."
        ),
    }


def _find_ride(ride_name: str) -> dict[str, Any]:
    """Resolve a free-text ride name to a snapshot record (substring).

    Verbatim from server.py. Raises ValueError on no match (surfaced to
    the client) and propagates _SnapshotUnavailable when S3 is down.
    """
    q = (ride_name or "").strip().lower()
    if not q:
        raise ValueError("ride_name cannot be empty")
    rides = _snapshot()["rides"]
    for r in rides:
        if q in r["ride_name"].lower():
            return r
    raise ValueError(
        f"No ride matching '{ride_name}'. "
        f"Use find_rides_matching to list rides by criteria."
    )


def _resolve_ride_via_ddb(ride_name: str) -> dict[str, Any] | None:
    """Resolve a ride name to its STATE row via a paginated DDB scan.

    The live tools (forecast, downtime) use this instead of the snapshot
    so they keep working even when the S3 snapshot is unavailable — their
    actual data lives in DDB, which the Lambda always reads live. Mirrors
    the substring resolver already inlined in get_live_ride_status.
    Returns the matched STATE item (with ride_id + name) or None.
    Lets DDB exceptions propagate so the caller can map auth failures.
    """
    q = (ride_name or "").strip().lower()
    if not q:
        return None
    table = _ddb_table()
    items: list[dict] = []
    scan_kwargs = {
        "FilterExpression": "SK = :sk",
        "ExpressionAttributeValues": {":sk": "STATE"},
    }
    while True:
        resp = table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    items = _convert_decimals(items)
    return next((r for r in items if q in (r.get("name") or "").lower()), None)


def _party_calendar() -> dict[str, Any]:
    """Load the party calendar from the bundled data file (cached).

    Returns {} on missing file so the tool can degrade gracefully.
    """
    global _party_calendar_cache
    if _party_calendar_cache is None:
        _party_calendar_cache = (
            json.loads(_PARTY_CALENDAR_PATH.read_text())
            if _PARTY_CALENDAR_PATH.exists()
            else {}
        )
    return _party_calendar_cache


def _mll_tiers() -> dict[str, Any]:
    """Load the MLL tier roster from the bundled data file (cached).

    Returns {} on missing file so the tool can degrade gracefully.
    """
    global _mll_tiers_cache
    if _mll_tiers_cache is None:
        _mll_tiers_cache = (
            json.loads(_MLL_TIERS_PATH.read_text())
            if _MLL_TIERS_PATH.exists()
            else {}
        )
    return _mll_tiers_cache


def _park_day_window_utc(days_back: int) -> tuple[datetime, datetime, str]:
    """Return [start, end_inclusive] UTC datetimes covering one park-day.

    Verbatim from server.py. Park-days run 4am ET to 4am ET so the live
    downtime count stays consistent with the historical heatmap. The
    end is shifted back 1 microsecond so a DDB BETWEEN over HIST# SKs is
    a clean half-open interval.
    """
    now_et = datetime.now(_EASTERN)
    target_date = (now_et - timedelta(days=days_back)).date()
    start_et = datetime.combine(
        target_date, time(_PARK_DAY_BOUNDARY_HOUR, 0), tzinfo=_EASTERN
    )
    end_et = start_et + timedelta(days=1) - timedelta(microseconds=1)
    return (
        start_et.astimezone(timezone.utc),
        end_et.astimezone(timezone.utc),
        target_date.isoformat(),
    )


# ─────────────────────── showtimes (verbatim port) ──────────────────
# Python port of the show classifier + fetcher, identical to server.py
# (which in turn mirrors web/src/lib/showtimes.ts). KEEP IN SYNC with
# server.py when shows turn over — drift means a misclassified show on
# the mobile planner, never fatal. Source: themeparks.wiki /live (the
# Lambda is not in a VPC, so this outbound HTTPS works by default).
_SHOW_PARK_IDS = {
    "magic_kingdom":     "75ea578a-adc8-4116-a54d-dccb60765ef9",
    "epcot":             "47f90d2c-e191-4239-a466-5892ef59a88b",
    "hollywood_studios": "288747d1-8b4f-4a64-867e-ea7c9b27bad8",
    "animal_kingdom":    "1c84a229-8862-4648-9c71-378ddd2c7693",
}

_SHOW_HEADLINER_CATEGORIES = ("spectacular", "parade", "stage")

_NAMED_ACT_OVERRIDES = [
    (_re.compile(r"mickey's magical friendship faire"),                "stage"),
    (_re.compile(r"celebraci[oó]n encanto"),                           "stage"),
    (_re.compile(r"feathered friends in flight"),                      "stage"),
    (_re.compile(r"indiana jones.*epic stunt"),                        "stage"),
    (_re.compile(r"candlelight processional"),                         "stage"),
    (_re.compile(r"viva mexico"),                                      "music"),
    (_re.compile(r"entertainment at canada mill stage"),               "music"),
    (_re.compile(r"entertainment at germany gazebo"),                  "music"),
    (_re.compile(r"eat to the beat"),                                  "music"),
    (_re.compile(r"garden rocks"),                                     "music"),
    (_re.compile(r"disney on broadway"),                               "music"),
    (_re.compile(r"adventures with kevin"),                            "character_meet"),
]

_SPECTACULAR_RX = _re.compile(
    r"\b(fireworks|spectacular|enchantment|happily ever after|luminous|"
    r"fantasmic|wonderful world of animation|disney movie magic|"
    r"tree of life awakenings|disney starlight|symphony of us)\b"
)
_PARADE_RX = _re.compile(r"\b(parade|cavalcade)\b")
_STAGE_RX = _re.compile(
    r"\b(live on stage|sing-?along|musical adventure|musical celebration|"
    r"festival of the lion king|finding nemo|epic stunt|frozen sing|"
    r"first order searches|disney villains|big blue|beauty and the beast)\b"
)
_MUSIC_RX = _re.compile(
    r"\b(band|philharmonic|drum|drummers|drummer|pianist|musician|concert|"
    r"mariachi|marimba|voices of|jammitors|dapper dans|beats and strings|"
    r"kora tinga|rhythmics|swingin|matsuriza)\b"
)


def _classify_show(name: str) -> str:
    """Bucket a SHOW entity by name into one of six categories.

    Mirrors web/src/lib/showtimes.ts `classifyShow`. Anything unmatched
    falls through to "atmosphere" — wrong-but-safe (still surfaced).
    """
    n = name.lower()
    for pattern, category in _NAMED_ACT_OVERRIDES:
        if pattern.search(n):
            return category
    if n.startswith("meet "):
        return "character_meet"
    if _SPECTACULAR_RX.search(n):
        return "spectacular"
    if _PARADE_RX.search(n):
        return "parade"
    if _STAGE_RX.search(n):
        return "stage"
    if _MUSIC_RX.search(n):
        return "music"
    return "atmosphere"


def _fetch_park_showtimes(park_key: str) -> list[dict[str, Any]] | None:
    """Fetch today's SHOW entities for a park from themeparks.wiki.

    Verbatim from server.py. Returns None on fetch failure — callers
    degrade gracefully (showtimes are nice-to-have, not load-bearing).
    """
    park_id = _SHOW_PARK_IDS.get(park_key)
    if not park_id:
        return None

    import requests
    url = f"https://api.themeparks.wiki/v1/entity/{park_id}/live"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None

    today_local = datetime.now(_EASTERN).date().isoformat()
    shows: list[dict[str, Any]] = []

    for item in data.get("liveData", []) or []:
        if item.get("entityType") != "SHOW":
            continue
        todays = []
        for s in item.get("showtimes", []) or []:
            start = s.get("startTime")
            end = s.get("endTime")
            if not start or not end:
                continue
            if start[:10] != today_local:
                continue
            todays.append({"start": start, "end": end})
        if not todays:
            continue
        todays.sort(key=lambda x: x["start"])
        shows.append({
            "id": item.get("id"),
            "name": item.get("name"),
            "category": _classify_show(item.get("name", "")),
            "showtimes": todays,
        })

    now_iso = datetime.now(_EASTERN).isoformat()
    def _sort_key(show: dict[str, Any]) -> tuple[int, str]:
        for t in show["showtimes"]:
            if t["start"] > now_iso:
                return (0, t["start"])
        return (1, (show.get("name") or "").lower())
    shows.sort(key=_sort_key)
    return shows


def _next_upcoming_showtime(
    show: dict[str, Any], now_iso: str
) -> dict[str, str] | None:
    """First performance of `show` starting after `now_iso`, or None."""
    for t in show.get("showtimes", []) or []:
        if t["start"] > now_iso:
            return t
    return None


# ─── FastMCP server + tools ─────────────────────────────────────────
# `stateless_http=True` is REQUIRED for Lambda: each request needs to
# be self-contained because the Lambda container doesn't preserve
# server-side session state across invocations.
#
# `transport_security` disables the MCP SDK's DNS rebinding protection.
# That protection is designed for localhost-bound MCP servers — it
# blocks a browser-side attacker from using DNS rebinding to trick
# a local app into thinking it's talking to localhost when it's
# actually talking to a malicious origin. In API Gateway → Lambda
# we don't have the localhost-binding shape that risk attacks, and
# the bearer-token middleware below is the real auth gate. Without
# this setting every request 421s with "Invalid Host header" because
# the SDK rejects API Gateway's *.execute-api hostnames.
mcp = FastMCP(
    "Magic Monitor (HTTP)",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)


@mcp.tool()
def hello_magic_monitor() -> str:
    """Sanity-check that the Magic Monitor MCP server is loaded and reachable.

    Returns a short greeting confirming the wiring works. Useful first
    call from any new client to verify auth + transport layers.
    """
    return (
        "Hello from Magic Monitor (HTTP transport) — MCP wiring works. "
        "Read tools available: get_live_ride_status, get_park_live_status, "
        "get_ride_forecast, get_ride_downtime_today, get_park_heatmap, "
        "get_ride_analytics, get_ride_dow_pattern, get_ride_down_clusters, "
        "get_ride_ll_drops, get_short_wait_baseline, find_rides_matching, "
        "get_park_showtimes, get_party_calendar, get_mll_tiers. "
        "Write tools + get_planning_context ship in later sessions."
    )


@mcp.tool()
def get_live_ride_status(ride_name: str) -> dict[str, Any]:
    """Return the most recent live status of a single ride by name.

    Reads the RIDE#<id>/STATE row written by the poller every 2
    minutes. Use this when the question is about ONE ride: 'is Space
    Mountain operating right now?' / 'what's the current wait for Big
    Thunder?'.

    For park-wide queries use `get_park_live_status` instead.

    Args:
        ride_name: Substring match (case-insensitive) against the
            `name` field on STATE rows. The first ride whose name
            contains the query wins.

    Returns:
        Dict with ride_name, ride_id, status, wait_mins, park, last_seen.
        On no match: `error: "ride not found"`. On AWS auth failure: a
        clear `error_hint` pointing at IAM.
    """
    q = (ride_name or "").strip().lower()
    if not q:
        return {"error": "ride_name cannot be empty"}

    try:
        table = _ddb_table()
        # The HTTP v1 doesn't ship the analytics snapshot to Lambda
        # (cuts the asset by ~1.1MB), so we resolve ride_name by
        # scanning STATE rows for a substring match on `name`. At ~88
        # rides × ~500 bytes per STATE row this fits one scan page.
        # If snapshot-side resolution ever becomes necessary
        # (offline-friendly behavior, better disambiguation), ship
        # the snapshot in session 2 and switch the resolver.
        items: list[dict] = []
        scan_kwargs = {
            "FilterExpression": "SK = :sk",
            "ExpressionAttributeValues": {":sk": "STATE"},
        }
        while True:
            resp = table.scan(**scan_kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except Exception as e:
        err = _aws_error_payload(e)
        if err is not None:
            return err
        return {"error": "DynamoDB read failed", "error_message": str(e)}

    items = _convert_decimals(items)
    match = next(
        (r for r in items if q in (r.get("name") or "").lower()),
        None,
    )
    if not match:
        return {
            "error": f"No ride matching '{ride_name}'.",
            "hint": "Try a more specific name, or use get_park_live_status to list rides.",
        }

    return {
        "ride_name": match.get("name"),
        "ride_id": match.get("ride_id"),
        "park_key": match.get("park_key"),
        "park_name": match.get("park_name"),
        "status": match.get("status"),
        "wait_mins": match.get("wait_mins"),
        "ll": match.get("ll"),
        "last_seen": match.get("last_seen"),
        "last_forecast_at": match.get("last_forecast_at"),
    }


@mcp.tool()
def get_park_live_status(
    park: str, status_filter: str | None = None
) -> dict[str, Any]:
    """Return the current live status of every ride in one park.

    Reads STATE rows with park_key matching the requested park. Use
    this when the question is about a park or a status across rides:
    'what's down at Magic Kingdom?' / 'which EPCOT rides have waits
    over 60 min?'.

    Returns rides sorted DOWN-first, then OPERATING by descending
    wait, then CLOSED/REFURBISHMENT — same order the live web UI uses.

    Args:
        park: Park key or human name. Accepts 'magic_kingdom',
            'Magic Kingdom', 'MK', etc.
        status_filter: Optional. One of 'OPERATING', 'DOWN', 'CLOSED',
            'REFURBISHMENT'. Case-insensitive.

    Returns:
        Dict with park, status_filter echoed back, count of matching
        rides, and the rides list (each with ride_id, name, status,
        wait_mins, last_seen).
    """
    try:
        park_key = _normalize_park(park)
    except ValueError as e:
        return {"error": str(e)}

    valid_statuses = {"OPERATING", "DOWN", "CLOSED", "REFURBISHMENT"}
    status_norm: str | None = None
    if status_filter is not None:
        status_norm = status_filter.strip().upper()
        if status_norm not in valid_statuses:
            return {
                "error": (
                    f"Unknown status_filter '{status_filter}'. "
                    f"Use one of: {', '.join(sorted(valid_statuses))}."
                )
            }

    try:
        table = _ddb_table()
        # Paginated Scan — same shape server.py uses post the
        # 2026-05-24 silent-regression fix. Web/ moved to a GSI Query
        # on 2026-05-25 (commit 4fd17bc3); MCP could follow but
        # session 1's "verbatim copy" rule means we ship the Scan
        # version here and follow up in a later session.
        items: list[dict] = []
        scan_kwargs = {
            "FilterExpression": "SK = :sk AND park_key = :pk",
            "ExpressionAttributeValues": {":sk": "STATE", ":pk": park_key},
        }
        while True:
            resp = table.scan(**scan_kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except Exception as e:
        err = _aws_error_payload(e)
        if err is not None:
            return {"park": park_key, **err}
        return {
            "park": park_key,
            "error": "DynamoDB scan failed",
            "error_message": str(e),
        }

    items = _convert_decimals(items)
    if status_norm is not None:
        items = [r for r in items if r.get("status") == status_norm]

    status_rank = {"DOWN": 0, "REFURBISHMENT": 1, "OPERATING": 2, "CLOSED": 3}

    def _sort_key(r: dict) -> tuple:
        rank = status_rank.get(r.get("status", ""), 99)
        wait = r.get("wait_mins")
        return (rank, -(wait if isinstance(wait, (int, float)) else -1))

    items.sort(key=_sort_key)

    return {
        "park": park_key,
        "status_filter": status_norm,
        "count": len(items),
        "rides": [
            {
                "ride_id": r.get("ride_id"),
                "name": r.get("name"),
                "status": r.get("status"),
                "wait_mins": r.get("wait_mins"),
                "last_seen": r.get("last_seen"),
            }
            for r in items
        ],
    }


# ─── Snapshot analytics tools (S3-backed) ──────────────────────────
# These seven read pre-aggregated history out of the analytics
# snapshot. On an S3 outage with nothing cached they return the
# graceful _snapshot_unavailable_payload(); a no-match still raises
# ValueError (surfaced to the client) exactly as server.py does.


@mcp.tool()
def get_park_heatmap(park: str, day_of_week: str | None = None) -> dict[str, Any]:
    """Return the wait-time heatmap cells for one park.

    The heatmap aggregates millions of historical poll snapshots into
    average wait minutes by day-of-week × hour-of-day, in Eastern time.
    Hours 0-3 (12-3am) are attributed to the previous day's row to
    handle late-night events. Cells with fewer than 20 active polls
    are omitted (treated as "park closed at this hour").

    Args:
        park: Park key or human-readable name. Accepts 'magic_kingdom',
            'Magic Kingdom', 'MK', etc.
        day_of_week: Optional. Filter to one day. Accepts 'monday',
            'tuesday', etc. If omitted, returns all 7 days.

    Returns:
        Dict with park key, optional day filter, and a list of cells
        each containing hour (0-23 ET), dow (0=Sun..6=Sat), avg wait
        in minutes, and the underlying poll count.
    """
    park_key = _normalize_park(park)
    try:
        cells = _snapshot()["heatmaps"].get(park_key, [])
    except _SnapshotUnavailable:
        return _snapshot_unavailable_payload()
    dow_filter: int | None = None
    if day_of_week is not None:
        dow_filter = _DOW_INDEX.get(day_of_week.strip().lower())
        if dow_filter is None:
            raise ValueError(
                f"Unknown day_of_week '{day_of_week}'. "
                f"Use one of: {', '.join(_DOW_NAMES)}."
            )
        cells = [c for c in cells if c["dow"] == dow_filter]
    return {
        "park": park_key,
        "day_of_week": _DOW_NAMES[dow_filter] if dow_filter is not None else None,
        "cell_count": len(cells),
        "cells": cells,
    }


@mcp.tool()
def get_ride_analytics(ride_name: str) -> dict[str, Any]:
    """Return analytics for one ride (downtime %, hourly waits, etc.).

    Substring match on ride_name (case-insensitive); returns the
    first matching ride. Use find_rides_matching to discover ride
    names by criteria.

    Args:
        ride_name: Free-text ride name. 'big thunder' matches
            'Big Thunder Mountain Railroad'.

    Returns:
        Dict with ride_name, park_key, total_polls, downtime_pct
        (0-100), max_wait minutes, avg_wait minutes, hourly_wait
        (one entry per ET hour-of-day with avg wait in minutes), and
        hourly_downtime (one entry per ET hour-of-day with downtime %).
    """
    try:
        return _find_ride(ride_name)
    except _SnapshotUnavailable:
        return _snapshot_unavailable_payload()


@mcp.tool()
def get_short_wait_baseline(
    ride_name: str, hour: int | None = None
) -> dict[str, Any]:
    """Return the per-hour short-wait alert thresholds for a ride.

    Magic Monitor's poller fires a Pushover notification when a ride's
    current wait drops below this threshold (and the ride is operating,
    and the user has favorited it, etc.). Thresholds are computed from
    historical wait_history as min(30, 0.5 × typical), and are only
    emitted when typical waits at that hour clear 25 minutes — there's
    no point alerting on rides whose typical wait is already short.

    Args:
        ride_name: Free-text ride name. Substring match.
        hour: Optional. One ET hour-of-day (0-23). If omitted, returns
            the threshold for every hour the ride has one for.

    Returns:
        Dict with ride_name, ride_id, an explanation of the metric,
        and either a single threshold (if `hour` given) or a map
        of hour -> threshold. Returns an empty thresholds map if the
        ride has no alertable hours (typical wait too low everywhere).
    """
    try:
        ride = _find_ride(ride_name)
        rid = ride["ride_id"]
        thresholds = _baselines().get("rides", {}).get(rid, {})
    except _SnapshotUnavailable:
        return _snapshot_unavailable_payload()
    info: dict[str, Any] = {
        "ride_name": ride["ride_name"],
        "ride_id": rid,
        "explanation": (
            "Wait minutes below this threshold (when status=OPERATING) "
            "fire a SHORT_WAIT alert. Threshold = min(30, 0.5 × typical) "
            "for hours where typical wait ≥ 25 min."
        ),
    }
    if hour is not None:
        if not 0 <= hour <= 23:
            raise ValueError(f"hour must be 0-23, got {hour}")
        info["hour"] = hour
        info["threshold_minutes"] = thresholds.get(str(hour))
    else:
        info["thresholds_by_hour"] = {int(h): t for h, t in sorted(
            thresholds.items(), key=lambda kv: int(kv[0])
        )}
    return info


@mcp.tool()
def get_ride_dow_pattern(
    ride_name: str, day_of_week: str | None = None
) -> dict[str, Any]:
    """Return per-(day-of-week, hour) wait + downtime cells for one ride.

    Use this when you need to answer 'how does X behave on Sundays?' /
    'is Pirates worse on Mondays vs Saturdays?' / 'when's the best
    Tuesday-evening window for Test Track?' type questions. Cells with
    fewer than 20 active polls in the historical data are omitted, so
    rides with thin Sunday-morning samples show only the buckets
    where the answer is meaningful.

    Same park-day-boundary rule as the park heatmap: 12-3am polls are
    attributed to the previous day's row.

    Args:
        ride_name: Substring match (case-insensitive). 'big thunder'
            matches 'Big Thunder Mountain Railroad'.
        day_of_week: Optional. Filter to one day. Accepts 'monday',
            'tuesday', etc. If omitted, returns cells for all 7 days.

    Returns:
        Dict with ride_name, ride_id, the optional day_of_week filter
        echoed back, the cell count, and an array of cells each with
        dow (0=Sun..6=Sat), hour (0-23 ET), downtime_pct, n_active,
        and wait (operating-only avg, omitted when zero operating
        polls in that bucket).
    """
    try:
        ride = _find_ride(ride_name)
    except _SnapshotUnavailable:
        return _snapshot_unavailable_payload()
    cells = ride.get("dow_hourly", [])
    dow_filter: int | None = None
    if day_of_week is not None:
        dow_filter = _DOW_INDEX.get(day_of_week.strip().lower())
        if dow_filter is None:
            raise ValueError(
                f"Unknown day_of_week '{day_of_week}'. "
                f"Use one of: {', '.join(_DOW_NAMES)}."
            )
        cells = [c for c in cells if c["dow"] == dow_filter]
    return {
        "ride_name": ride["ride_name"],
        "ride_id": ride["ride_id"],
        "day_of_week": _DOW_NAMES[dow_filter] if dow_filter is not None else None,
        "cell_count": len(cells),
        "cells": cells,
    }


@mcp.tool()
def get_ride_down_clusters(ride_name: str) -> dict[str, Any]:
    """Return the contiguous DOWN runs ('clusters') detected for a ride.

    Use this to investigate whether a ride's downtime looks 'structural'
    (long sustained DOWN periods recurring at consistent times) or
    'flap-style' (many short DOWN events scattered throughout the data
    window). High clustering at consistent (dow, hour) IS a signal,
    not a diagnosis — possible causes include scheduled maintenance,
    inspections, multi-week mechanical issues right before refurb, or
    upstream API quirks. The data alone can't determine which.

    Companion to get_ride_dow_pattern: each cell's
    `recurring_down_fraction` tells you what fraction of that bucket's
    DOWN polls were part of a long cluster.

    Args:
        ride_name: Substring match (case-insensitive).

    Returns:
        Dict with ride_name, ride_id, cluster_count, and a list of
        clusters each with start_ts, end_ts, duration_minutes,
        poll_count, start_hour (0-23 ET), and start_dow (0=Sun..6=Sat,
        park-day-shifted). Includes summary stats: total downtime
        across clusters, count of long clusters (≥2h), and the most
        common (dow, hour) start pair if one exists.
    """
    try:
        ride = _find_ride(ride_name)
    except _SnapshotUnavailable:
        return _snapshot_unavailable_payload()
    clusters = ride.get("down_clusters", [])

    total_downtime = sum(c["duration_minutes"] for c in clusters)
    long_count = sum(1 for c in clusters if c["duration_minutes"] >= 120)
    from collections import Counter
    start_counter = Counter(
        (c["start_dow"], c["start_hour"]) for c in clusters
    )
    most_common_start = None
    if start_counter:
        (top_dow, top_hour), top_n = start_counter.most_common(1)[0]
        if top_n >= 2:
            most_common_start = {
                "dow": top_dow,
                "hour": top_hour,
                "occurrences": top_n,
                "out_of_total_clusters": len(clusters),
            }
    return {
        "ride_name": ride["ride_name"],
        "ride_id": ride["ride_id"],
        "cluster_count": len(clusters),
        "long_cluster_count": long_count,
        "total_downtime_minutes": total_downtime,
        "most_common_start": most_common_start,
        "clusters": clusters,
    }


@mcp.tool()
def get_ride_ll_drops(ride_name: str) -> dict[str, Any]:
    """Return Lightning Lane drop pattern analytics for one ride.

    Use this to answer trip-planning questions about when LL slots
    typically refresh — i.e., "when should I check the Disney app to
    try to grab a better TRON LL slot?" A "drop" is a same-day event
    where Disney moves the next-available return time earlier
    (cancellations, no-shows, or system refreshes). Each drop is an
    opportunity for a guest to swap their current LL for a better
    slot through the app.

    Sourced from the historical analytics snapshot. For rides without
    an LL offering or too few drops to characterize, returns
    `data_available: false`.

    Args:
        ride_name: Substring match (case-insensitive). 'big thunder'
            matches 'Big Thunder Mountain Railroad'.

    Returns:
        Dict with ride_name, ride_id, total drops in the window,
        active_days, drops_per_active_day, typical_shift_minutes
        (median minutes the slot moves earlier on a drop), drop_hours
        (histogram by ET hour), and drop_dow (histogram by day of
        week, 0=Sun..6=Sat). On no data: data_available: false.
    """
    try:
        ride = _find_ride(ride_name)
    except _SnapshotUnavailable:
        return _snapshot_unavailable_payload()
    drops_total = ride.get("ll_drops_total")
    if not drops_total:
        return {
            "ride_name": ride["ride_name"],
            "ride_id": ride["ride_id"],
            "data_available": False,
            "explanation": (
                "No LL drop data in the snapshot for this ride. Most "
                "likely the ride doesn't offer Lightning Lane (walk-up "
                "attraction, no-queue ride, or show), or it had too few "
                "drops to characterize. Rides like Test Track or "
                "Slinky Dog Dash typically have hundreds of drops in a "
                "5-week window."
            ),
        }

    drop_hours = ride.get("ll_drop_hours") or []
    drop_dow = ride.get("ll_drop_dow") or []
    top_hours = sorted(drop_hours, key=lambda x: -x["count"])[:3]
    top_dows = sorted(drop_dow, key=lambda x: -x["count"])[:3]
    dow_names = ["Sunday", "Monday", "Tuesday", "Wednesday",
                 "Thursday", "Friday", "Saturday"]

    return {
        "ride_name": ride["ride_name"],
        "ride_id": ride["ride_id"],
        "data_available": True,
        "total_drops": drops_total,
        "active_days": ride.get("ll_active_days"),
        "drops_per_active_day": ride.get("ll_drops_per_active_day"),
        "typical_shift_minutes": ride.get("ll_typical_shift_mins"),
        "drop_hours": drop_hours,
        "top_drop_hours": [
            {"hour": h["hour"], "count": h["count"]}
            for h in top_hours
        ],
        "drop_dow": drop_dow,
        "top_drop_days": [
            {"dow": d["dow"], "day_name": dow_names[d["dow"]], "count": d["count"]}
            for d in top_dows
        ],
        "explanation": (
            "drops_per_active_day tells you how frequently this ride's "
            "LL slot refreshes earlier. typical_shift_minutes tells you "
            "how big the typical refresh is (median). top_drop_hours "
            "is when in the ET day refreshes most commonly happen — "
            "useful for suggesting when a guest should check the app "
            "if they want a better slot."
        ),
    }


@mcp.tool()
def find_rides_matching(
    park: str | None = None,
    max_downtime_pct: float | None = None,
    min_downtime_pct: float | None = None,
    min_avg_wait: int | None = None,
    max_avg_wait: int | None = None,
    sort_by: str = "downtime_pct",
    sort_desc: bool = True,
    limit: int = 20,
) -> dict[str, Any]:
    """Filter and sort rides across the analytics snapshot.

    Use this to answer questions like 'which rides have low downtime
    but high waits?' or 'what's the most reliable ride at Magic
    Kingdom?'

    Args:
        park: Optional. Restrict to one park (key or human name).
        max_downtime_pct: Optional. Only rides at or below this %.
        min_downtime_pct: Optional. Only rides at or above this %.
        min_avg_wait: Optional. Only rides with avg wait >= this (min).
        max_avg_wait: Optional. Only rides with avg wait <= this (min).
        sort_by: One of 'downtime_pct', 'avg_wait', 'max_wait',
            'total_polls', 'ride_name'. Default 'downtime_pct'.
        sort_desc: Whether to sort descending (default True).
        limit: Max rows to return. Default 20, max 100.

    Returns:
        Dict with the resolved filter, the sort, the matched count,
        and a list of ride summaries (ride_name, park_key,
        downtime_pct, avg_wait, max_wait, total_polls).
    """
    try:
        rides = list(_snapshot()["rides"])
    except _SnapshotUnavailable:
        return _snapshot_unavailable_payload()
    park_key: str | None = None
    if park is not None:
        park_key = _normalize_park(park)
        rides = [r for r in rides if r["park_key"] == park_key]

    if max_downtime_pct is not None:
        rides = [r for r in rides if r["downtime_pct"] <= max_downtime_pct]
    if min_downtime_pct is not None:
        rides = [r for r in rides if r["downtime_pct"] >= min_downtime_pct]
    if min_avg_wait is not None:
        rides = [r for r in rides if (r.get("avg_wait") or 0) >= min_avg_wait]
    if max_avg_wait is not None:
        rides = [r for r in rides if (r.get("avg_wait") or 0) <= max_avg_wait]

    valid_sorts = {"downtime_pct", "avg_wait", "max_wait", "total_polls", "ride_name"}
    if sort_by not in valid_sorts:
        raise ValueError(
            f"sort_by must be one of {sorted(valid_sorts)}, got '{sort_by}'"
        )

    def _key(r: dict[str, Any]) -> Any:
        v = r.get(sort_by)
        if sort_by == "ride_name":
            return (v or "").lower()
        return v if v is not None else float("-inf")

    rides.sort(key=_key, reverse=sort_desc)
    limit = max(1, min(limit, 100))
    return {
        "filter": {
            "park": park_key,
            "max_downtime_pct": max_downtime_pct,
            "min_downtime_pct": min_downtime_pct,
            "min_avg_wait": min_avg_wait,
            "max_avg_wait": max_avg_wait,
        },
        "sort_by": sort_by,
        "sort_desc": sort_desc,
        "match_count": len(rides),
        "rides": [
            {
                "ride_name": r["ride_name"],
                "park_key": r["park_key"],
                "downtime_pct": r["downtime_pct"],
                "avg_wait": r.get("avg_wait"),
                "max_wait": r.get("max_wait"),
                "total_polls": r["total_polls"],
            }
            for r in rides[:limit]
        ],
    }


# ─── Live DDB analytics tools ───────────────────────────────────────
# forecast + downtime resolve the ride name via a DDB STATE scan
# (_resolve_ride_via_ddb) rather than the snapshot, so they keep working
# even when the S3 snapshot is unavailable — their data lives in DDB.


@mcp.tool()
def get_ride_forecast(ride_name: str) -> dict[str, Any]:
    """Return the most recent themeparks.wiki forecast for a ride.

    Live tool — reads the latest FORECAST# row from DynamoDB. Forecasts
    are upstream's hourly wait-time predictions covering current-hour
    through park close. The poller captures one forecast snapshot per
    ride per 2-min poll and TTLs them after 7 days.

    Use this to answer 'how busy is Space Mountain expected to get
    later today?' / 'when does the system think Big Thunder peaks
    today?' Pair with get_ride_analytics for "expected today vs typical
    for this hour."

    Forecasts are absent for DOWN rides, walk-up character meets,
    no-queue attractions, and some shows. The tool returns
    `forecast_available: false` in those cases, with the ride's
    `last_forecast_at` when available.

    Args:
        ride_name: Substring match (case-insensitive). 'space mountain'
            matches 'Space Mountain'.

    Returns:
        Dict with ride_name, ride_id, polled_at (UTC iso), forecast
        (list of {time, wait_mins, percentage}). On absence: ride_name,
        ride_id, forecast_available=false, last_forecast_at (or null).
        On AWS auth failure: a clear error_hint pointing at IAM.
    """
    try:
        match = _resolve_ride_via_ddb(ride_name)
    except Exception as e:
        err = _aws_error_payload(e)
        if err is not None:
            return err
        return {"error": "DynamoDB read failed", "error_message": str(e)}

    if not match:
        return {
            "error": f"No ride matching '{ride_name}'.",
            "hint": "Try a more specific name, or use get_park_live_status to list rides.",
        }

    rid = match.get("ride_id")
    ride_display = match.get("name")
    table = _ddb_table()
    try:
        resp = table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": f"RIDE#{rid}",
                ":sk": "FORECAST#",
            },
            ScanIndexForward=False,
            Limit=1,
        )
    except Exception as e:
        err = _aws_error_payload(e)
        if err is not None:
            return {"ride_name": ride_display, "ride_id": rid, **err}
        return {
            "ride_name": ride_display,
            "ride_id": rid,
            "error": "DynamoDB query failed",
            "error_message": str(e),
        }

    items = resp.get("Items", [])
    if not items:
        # No forecast row. We already have the STATE row from the scan,
        # so surface last_forecast_at + current status directly — no
        # second GetItem needed (unlike server.py, which re-fetches).
        return {
            "ride_name": ride_display,
            "ride_id": rid,
            "forecast_available": False,
            "last_forecast_at": match.get("last_forecast_at"),
            "current_status": match.get("status"),
            "explanation": (
                "No forecast in DynamoDB for this ride. Common reasons: "
                "ride is DOWN, walk-up attraction with no queue, or "
                "the upstream API isn't predicting it today. "
                "If last_forecast_at is set, that's when we last saw "
                "one — the gap is itself a signal worth investigating."
            ),
        }

    item = _convert_decimals(items[0])
    return {
        "ride_name": ride_display,
        "ride_id": rid,
        "polled_at": item.get("polled_at"),
        "forecast_available": True,
        "forecast_entries": len(item.get("forecast", [])),
        "forecast": item.get("forecast", []),
    }


@mcp.tool()
def get_ride_downtime_today(
    ride_name: str, days_back: int = 0
) -> dict[str, Any]:
    """Count how many times a ride went DOWN during one park-day.

    Live tool — queries the RIDE#<id>/HIST#<ts> sub-rows the poller
    writes on every status transition. Returns each DOWN incident that
    *started* during the park-day window, plus how many DOWN→OPERATING
    recoveries fell in the same window.

    "Park-day" matches the analytics convention exactly: 4am ET to 4am
    ET (next calendar day). A 12-3am poll attributes to the previous
    park-day's row.

    Use this for: 'how many times has Big Thunder been down today?'
    'has Test Track had any breakdowns this morning?' 'pull yesterday's
    Pirates downtime.'

    Args:
        ride_name: Substring match (case-insensitive). 'big thunder'
            matches 'Big Thunder Mountain Railroad'.
        days_back: 0 = today (default), 1 = yesterday, etc. Capped at
            90 (HIST# rows TTL after 90 days).

    Returns:
        Dict with ride_name, ride_id, park_day (ISO date), down_count,
        recovery_count, total_transitions, the down incidents (each
        with went_down_at + wait_at_breakdown), and an is_partial_day
        flag when the park-day is still in progress. On AWS auth
        failure: a clear error_hint pointing at IAM.
    """
    if days_back < 0:
        raise ValueError("days_back must be >= 0 (0 = today, 1 = yesterday)")
    if days_back > _HIST_RETENTION_DAYS:
        raise ValueError(
            f"HIST rows TTL after {_HIST_RETENTION_DAYS} days; "
            f"days_back={days_back} would return empty. "
            f"For older windows use get_ride_down_clusters."
        )

    try:
        match = _resolve_ride_via_ddb(ride_name)
    except Exception as e:
        err = _aws_error_payload(e)
        if err is not None:
            return err
        return {"error": "DynamoDB read failed", "error_message": str(e)}

    if not match:
        return {
            "error": f"No ride matching '{ride_name}'.",
            "hint": "Try a more specific name, or use get_park_live_status to list rides.",
        }

    rid = match.get("ride_id")
    ride_display = match.get("name")
    window_start_utc, window_end_utc, park_day = _park_day_window_utc(days_back)

    table = _ddb_table()
    try:
        items: list[dict] = []
        sk_lo = f"HIST#{window_start_utc.isoformat()}"
        sk_hi = f"HIST#{window_end_utc.isoformat()}"
        kwargs = {
            "KeyConditionExpression": "PK = :pk AND SK BETWEEN :lo AND :hi",
            "ExpressionAttributeValues": {
                ":pk": f"RIDE#{rid}",
                ":lo": sk_lo,
                ":hi": sk_hi,
            },
        }
        while True:
            resp = table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except Exception as e:
        err = _aws_error_payload(e)
        if err is not None:
            return {"ride_name": ride_display, "ride_id": rid, **err}
        return {
            "ride_name": ride_display,
            "ride_id": rid,
            "error": "DynamoDB query failed",
            "error_message": str(e),
        }

    items = _convert_decimals(items)
    down_events = [i for i in items if i.get("new_status") == "DOWN"]
    recoveries = [
        i for i in items
        if i.get("old_status") == "DOWN" and i.get("new_status") == "OPERATING"
    ]

    return {
        "ride_name": ride_display,
        "ride_id": rid,
        "park_day": park_day,
        "park_day_window_utc": {
            "start": window_start_utc.isoformat(),
            "end_inclusive": window_end_utc.isoformat(),
        },
        "is_partial_day": days_back == 0,
        "down_count": len(down_events),
        "recovery_count": len(recoveries),
        "total_transitions": len(items),
        "down_incidents": [
            {
                "went_down_at": e.get("changed_at"),
                "wait_at_breakdown": e.get("wait_mins"),
            }
            for e in down_events
        ],
    }


# ─── Bundled-data / egress tools ────────────────────────────────────
# showtimes hits themeparks.wiki (egress, Lambda not in a VPC); party
# calendar + MLL tiers read small JSON files bundled into the asset.


@mcp.tool()
def get_park_showtimes(park: str) -> dict[str, Any]:
    """Today's entertainment lineup at a park: shows + performance times.

    Returns every SHOW entity at the park that has at least one
    performance starting today (in park-local Eastern time), each
    classified into one of: spectacular, parade, stage, music,
    atmosphere, character_meet. The first three are the "headliners"
    the web UI surfaces by default.

    Source: themeparks.wiki /live endpoint. Showtimes are published
    once per day and rarely change intra-day.

    Args:
        park: Park key or human name. Accepts 'magic_kingdom',
            'Magic Kingdom', 'MK', etc.

    Returns:
        Dict with park, current_time_et, shows (list of
        {id, name, category, showtimes: [{start, end}, ...]}, sorted
        by next-upcoming start time), and next_up — the soonest
        unstarted performance anywhere in the park, or None. On fetch
        failure, returns shows=None with an error_message.
    """
    park_key = _normalize_park(park)
    now_et = datetime.now(_EASTERN)
    shows = _fetch_park_showtimes(park_key)

    if shows is None:
        return {
            "park": park_key,
            "current_time_et": now_et.isoformat(),
            "shows": None,
            "next_up": None,
            "error_message": (
                "Failed to fetch showtimes from themeparks.wiki. "
                "Try again in a moment; the upstream API is occasionally "
                "flaky for individual park endpoints."
            ),
        }

    now_iso = now_et.isoformat()
    next_up: dict[str, Any] | None = None
    for show in shows:
        nt = _next_upcoming_showtime(show, now_iso)
        if not nt:
            continue
        if next_up is None or nt["start"] < next_up["time"]["start"]:
            next_up = {
                "show": {
                    "id": show["id"],
                    "name": show["name"],
                    "category": show["category"],
                },
                "time": nt,
            }

    return {
        "park": park_key,
        "current_time_et": now_et.isoformat(),
        "shows": shows,
        "next_up": next_up,
    }


@mcp.tool()
def get_party_calendar(
    date: str | None = None,
    days_ahead: int = 14,
) -> dict[str, Any]:
    """Check for WDW after-hours parties (MNSSHP, MVMCP) on or near a date.

    Call this when planning ANY trip — present-day or future. Even
    knowing a trip date is NOT a party day is useful (rules out the
    6pm-closure concern). Returns parties happening on the specified
    date and within the next N days.

    **Crowd dynamics when a planning day IS a party day:**
    - Daytime crowds at the host park are LIGHTER than typical (AP
      holders and locals avoid the 6pm early close). Treat predicted
      waits as if park_load_ratio were ~0.80-0.85.
    - **The park closes for non-party guests at the party start time
      (typically 6pm).** Surface this upfront: if the user is planning
      a party-date day without a party ticket, warn them BEFORE laying
      out the plan.
    - Evening-during-party waits are often SHORTER than typical daytime
      waits — capped attendance is the whole point.

    **Data caveat the tool surfaces:** dates_status tells you whether
    the listed dates are verified or estimated; hedge party-day claims
    accordingly when estimated.

    Args:
        date: ISO date string (YYYY-MM-DD). Defaults to today (ET).
        days_ahead: Days from `date` to include in the lookahead.
            Default 14.

    Returns:
        Dict with date_checked, days_ahead, end_date, parties (list of
        {abbreviation, full_name, park, dates_in_range,
        is_party_day_on_target_date, park_closes_early_for_non_party,
        party_hours, crowd_effects, non_party_ticket_implications,
        dates_status, dates_caveat}), data_updated_at, maintenance_note.
        Empty parties list = no party-day constraints to apply.
    """
    data = _party_calendar()
    if not data:
        return {
            "error": "Party calendar data file missing",
            "error_message": (
                "data/party_calendar.json not found in the bundle. Tool "
                "degrades gracefully — proceed with normal planning, but "
                "note the planner can't catch party-day constraints "
                "without this data file."
            ),
        }

    if date is None:
        target_date = datetime.now(_EASTERN).date()
    else:
        try:
            target_date = datetime.fromisoformat(date).date()
        except ValueError:
            return {
                "error": "Invalid date format",
                "error_message": f"Could not parse '{date}'. Use YYYY-MM-DD.",
            }

    end_date = target_date + timedelta(days=max(0, days_ahead))
    target_iso = target_date.isoformat()
    end_iso = end_date.isoformat()

    matches: list[dict[str, Any]] = []
    for party_key, party in (data.get("parties") or {}).items():
        all_dates = sorted(set(party.get("dates") or []))
        in_range = [d for d in all_dates if target_iso <= d <= end_iso]
        if not in_range:
            continue
        matches.append({
            "abbreviation": party_key,
            "full_name": party.get("full_name"),
            "park": party.get("park"),
            "dates_in_range": in_range,
            "is_party_day_on_target_date": target_iso in in_range,
            "park_closes_early_for_non_party": party.get("park_closes_early_for_non_party"),
            "party_hours": party.get("party_hours"),
            "crowd_effects": party.get("crowd_effects"),
            "non_party_ticket_implications": party.get("non_party_ticket_implications"),
            "dates_status": party.get("dates_status"),
            "dates_caveat": party.get("dates_caveat"),
        })

    return {
        "date_checked": target_iso,
        "days_ahead": days_ahead,
        "end_date": end_iso,
        "parties": matches,
        "data_updated_at": data.get("updated_at"),
        "maintenance_note": data.get("maintenance_note"),
    }


@mcp.tool()
def get_mll_tiers(park: str) -> dict[str, Any]:
    """Current Multi-Pass tier rosters for a park.

    Returns the Tier 1 / Tier 2 lists for the park (or a no-tiers note
    for Animal Kingdom). Use this when reasoning about a guest's
    pre-arrival LL bookings — the 3-ride allocation requires exactly
    1 Tier 1 + 2 Tier 2 at MK/EPCOT/HS, while AK allows any 3.

    **Treat the result as best-known state, not live data.** Disney
    revises tier assignments periodically. The returned `updated_at`
    tells you when this snapshot was hand-verified. If the user
    mentions a ride that doesn't match, ASK rather than overruling them
    (the My Disney Experience app is the source of truth).

    Args:
        park: Park key or human name. Accepts 'magic_kingdom',
            'Magic Kingdom', 'MK', etc.

    Returns:
        Dict with park, has_tiers (bool), tier_1 / tier_2 lists for
        tiered parks OR ll_eligible list + any_three:true for Animal
        Kingdom, rules, updated_at, and a maintenance_note. Returns an
        error payload if the data file is missing.
    """
    park_key = _normalize_park(park)
    data = _mll_tiers()
    if not data:
        return {
            "error": "MLL tiers data file missing",
            "error_message": (
                "data/mll_tiers.json not found in the bundle. The tool "
                "degrades gracefully — use general tier knowledge or "
                "ask the user to check the Disney app."
            ),
        }

    park_data = data.get(park_key)
    if park_data is None:
        return {
            "error": "Park not in tier snapshot",
            "park_key": park_key,
            "available_parks": [
                k for k in data
                if isinstance(data.get(k), dict) and "has_tiers" in data[k]
            ],
        }

    return {
        "park": park_key,
        "updated_at": data.get("updated_at"),
        "rules": data.get("rules", {}),
        "maintenance_note": data.get("maintenance_note"),
        **park_data,
    }


# ─── OAuth discovery + DCR routes ───────────────────────────────────
# Per RFC 9728 (Protected Resource Metadata) + RFC 8414 (Authorization
# Server Metadata) + RFC 7591 (Dynamic Client Registration). These
# three endpoints are PUBLIC by spec — auth gate is bypassed for them
# so a client that hasn't registered yet can discover where to go.
#
# Routes are handled inside the middleware rather than registered on
# the Starlette app to avoid coordinating with FastMCP's internal
# router. Path matching here is exact; no wildcards.

_PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"
_AUTHORIZATION_SERVER_PATH = "/.well-known/oauth-authorization-server"
_REGISTER_PATH = "/register"


def _public_base_url() -> str:
    """The HTTPS base URL clients use to reach this server (no trailing /).

    Set by CDK to the API Gateway endpoint. Falls back to empty in
    local dev — the middleware does not require it for the auth gate,
    only the metadata endpoints reference it.
    """
    return os.environ.get("MCP_PUBLIC_BASE_URL", "").rstrip("/")


def _cognito_domain_url() -> str:
    return os.environ.get("COGNITO_DOMAIN_URL", "").rstrip("/")


def _cognito_jwks_url() -> str:
    region = os.environ.get("COGNITO_REGION", "us-east-2")
    pool_id = os.environ.get("COGNITO_USER_POOL_ID", "")
    return f"https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/jwks.json"


def _protected_resource_metadata() -> dict[str, Any]:
    """RFC 9728 — points clients at this server's authorization server."""
    base = _public_base_url()
    return {
        "resource": base,
        "authorization_servers": [base],
    }


def _authorization_server_metadata() -> dict[str, Any]:
    """RFC 8414 — the DCR-proxy quirk: `issuer` is our base URL, but
    `authorization_endpoint`/`token_endpoint` point at Cognito's hosted
    UI. Clients follow `jwks_uri` for verification rather than strict
    issuer matching, so the issuer mismatch with the token's `iss`
    claim (Cognito's URL) is tolerated by spec-compliant clients."""
    base = _public_base_url()
    cognito = _cognito_domain_url()
    return {
        "issuer": base,
        "authorization_endpoint": f"{cognito}/oauth2/authorize",
        "token_endpoint": f"{cognito}/oauth2/token",
        "registration_endpoint": f"{base}{_REGISTER_PATH}",
        "jwks_uri": _cognito_jwks_url(),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["openid", "email", "profile"],
    }


async def _handle_register(request: Request) -> JSONResponse:
    """POST /register — RFC 7591 DCR proxy to Cognito CreateUserPoolClient."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            {
                "error": "invalid_client_metadata",
                "error_description": "request body must be JSON",
            },
            status_code=400,
        )

    user_pool_id = os.environ.get("COGNITO_USER_POOL_ID", "")
    if not user_pool_id:
        return JSONResponse(
            {"error": "server not configured (missing COGNITO_USER_POOL_ID)"},
            status_code=503,
        )

    try:
        result = dcr_proxy.register_client(payload, user_pool_id=user_pool_id)
    except dcr_proxy.RegistrationError as e:
        return JSONResponse(
            {"error": e.code, "error_description": e.description},
            status_code=400,
        )

    return JSONResponse(result, status_code=201)


# ─── Cognito JWT middleware ─────────────────────────────────────────
# Replaces the v1 bearer-secret middleware. Every non-public request
# must carry `Authorization: Bearer <access_token>` where the access
# token is a Cognito-issued, RS256-signed JWT whose `sub` is in the
# allowlist (enforced inside jwt_verifier.verify_token()).
#
# Public bypass list, in order checked:
#   1. OPTIONS — CORS preflight
#   2. /.well-known/oauth-protected-resource — RFC 9728 discovery
#   3. /.well-known/oauth-authorization-server — RFC 8414 discovery
#   4. POST /register — RFC 7591 DCR
#
# Everything else (FastMCP's /mcp/* routes) requires a valid token.

class _CognitoJwtMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, verifier=None):
        super().__init__(app)
        # Injectable for tests. Production callers pass nothing and
        # we use the env-derived verifier from jwt_verifier.
        self._verify = verifier or jwt_verifier.verify_token

    async def dispatch(self, request: Request, call_next):
        method = request.method
        path = request.url.path

        if method == "OPTIONS":
            return await call_next(request)

        # Public OAuth discovery + DCR — handled here, no auth gate.
        if method == "GET" and path == _PROTECTED_RESOURCE_PATH:
            return JSONResponse(_protected_resource_metadata())
        if method == "GET" and path == _AUTHORIZATION_SERVER_PATH:
            return JSONResponse(_authorization_server_metadata())
        if method == "POST" and path == _REGISTER_PATH:
            return await _handle_register(request)

        # All other paths require a valid Cognito access token.
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse(
                {"error": "missing or malformed Authorization header"},
                status_code=401,
            )

        token = auth[len("Bearer "):].strip()
        try:
            self._verify(token)
        except jwt_verifier.VerifyError:
            # Don't leak which check failed — verifier logs the detail
            # server-side; client sees a generic 401.
            return JSONResponse({"error": "invalid token"}, status_code=401)
        except Exception:
            # Defensive: any unexpected verify-side error (e.g., JWKS
            # network failure) becomes a 503 so the client can retry.
            return JSONResponse(
                {"error": "auth verification failed"},
                status_code=503,
            )

        return await call_next(request)


# ─── Public ASGI app ────────────────────────────────────────────────
# Build the streamable-HTTP app and wrap it with the Cognito JWT
# middleware. The Lambda handler imports `app` and hands it to Mangum;
# nothing else in this module is part of the public surface.

app = mcp.streamable_http_app()
app.add_middleware(_CognitoJwtMiddleware)


if __name__ == "__main__":
    # Local debugging entrypoint — run with `python server_http.py`
    # to expose the HTTP server on :8000 for curl testing without
    # going through Lambda. Requires `pip install uvicorn` locally.
    # Lambda doesn't use this path; the handler imports `app`
    # directly.
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
