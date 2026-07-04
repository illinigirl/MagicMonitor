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

import contextvars
import json
import logging
import os
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Module logger. The auth middleware below logs verification outcomes
# here (WARNING for a rejected token, ERROR for an unexpected verify-side
# failure). In Lambda these land in the function's CloudWatch log group.
# Previously the middleware comments claimed "the verifier logs the
# detail server-side" but NO logging existed anywhere on the auth path —
# a missing-diagnostics gap that conflicts with the project's
# "log at each boundary first" debugging doctrine (CLAUDE.md).
_logger = logging.getLogger("magicmonitor.mcp.auth")

from mcp.server.fastmcp import FastMCP

# Shared tool implementations + helpers live in _tool_impls (single
# source of truth, shared with server.py). Imported by name so existing
# call sites are unchanged. This HTTP host writes to ONE shared partition
# and derives created_by from the verified token; the moved helpers take
# user_id as a param, so that identity split stays in the write tools here.
import _tool_impls
from _tool_impls import (
    _AGGRESSION_SCORES,
    _AGGRESSION_VALUES,
    _BIAS_CONFIDENCE_HIGH,
    _BIAS_CONFIDENCE_MEDIUM,
    _BIAS_NEUTRAL_MINUTES,
    _DOW_INDEX,
    _DOW_NAMES,
    _EASTERN,
    _HIST_RETENTION_DAYS,
    _MUSIC_RX,
    _NAMED_ACT_OVERRIDES,
    _PARADE_RX,
    _PARK_DAY_BOUNDARY_HOUR,
    _PARK_KEYS,
    _PLAN_PENDING_BUFFER_DAYS,
    _PLAN_PENDING_TTL_SECS,
    _PLAN_RECORDED_TTL_SECS,
    _PLAN_STALENESS_DAYS,
    _SHOW_HEADLINER_CATEGORIES,
    _SHOW_PARK_IDS,
    _SPECTACULAR_RX,
    _STAGE_RX,
    _TIMING_VALUES,
    _TRIP_BUFFER_DAYS,
    _WDW_LAT,
    _WDW_LON,
    _all_park_state_rows_via_gsi,
    _apply_alert_subscription,
    _aws_error_payload,
    _bias_confidence,
    _build_plan_item,
    _classify_show,
    _coerce_plan_id_to_sk,
    _compute_calibration_summary,
    _compute_load_vs_forecast,
    _convert_decimals,
    _epoch_now,
    _fetch_park_currently_down,
    _fetch_park_hours_today,
    _fetch_park_showtimes,
    _fetch_weather_forecast,
    _find_ride,
    _floats_to_decimals,
    _forecast_peak_in_window,
    _next_upcoming_showtime,
    _normalize_park,
    _park_day_window_utc,
    _park_state_rows_via_gsi,
    _plan_pending_ttl,
    _pop_ride_from_sequence,
    _resolve_alert_member,
    _today_et_date_iso,
    get_planning_context,
)
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

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


# ─── Analytics snapshot (S3-backed) + reference data ────────────────
# The 1.2MB analytics snapshot and the short-wait baselines are
# regenerated nightly by the aggregator (.github/workflows/aggregate.yml)
# and uploaded to S3. We fetch them lazily on the first analytics tool
# call and cache in module globals for the container lifetime. See the
# module docstring for the why-S3-not-bundled rationale.
_SNAPSHOT_BUCKET = os.environ.get("MCP_SNAPSHOT_BUCKET", "")
_SNAPSHOT_KEY = os.environ.get("MCP_SNAPSHOT_KEY", "analytics-snapshot.json")
_BASELINES_KEY = os.environ.get("MCP_BASELINES_KEY", "baselines.json")


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
    # GSI partition Queries (one per park) instead of a full-table Scan —
    # ~150 STATE rows total, independent of table size. See
    # _all_park_state_rows_via_gsi.
    items = _convert_decimals(_all_park_state_rows_via_gsi(_ddb_table()))
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


# Static lat/lon snapshot, bundled into the asset (mcp/data/). Adds
# per-ride location to get_planning_context for walking-distance
# reasoning; degrades gracefully ({}) if absent.
_LOCATIONS_PATH = _DATA_DIR / "attraction-locations.json"
_locations_cache: dict[str, dict[str, Any]] | None = None


def _locations() -> dict[str, dict[str, Any]]:
    """Load the lat/lon snapshot once per container. Returns {} if the
    bundled file is missing (the consumer handles empty)."""
    global _locations_cache
    if _locations_cache is None:
        _locations_cache = (
            json.loads(_LOCATIONS_PATH.read_text())
            if _LOCATIONS_PATH.exists()
            else {}
        )
    return _locations_cache


# ─── Plan / trip write-side helpers (M5, ported from server.py) ─────
# Duplicate-first (per the locked decision): these mirror server.py's
# plan-feedback + multi-day-trip helpers verbatim, except the HTTP side
# writes to ONE shared partition and derives the writer's attribution
# from the verified token rather than a client-supplied user_id.

# Shared family trip space — every HTTP write lands here (see design
# doc §7). Reuses the stdio default partition so Megan's Desktop and
# mobile plans are unified and Jim/sister join the same trip.
_SHARED_USER_ID = "megan"


# ─── Identity (token → attribution) ─────────────────────────────────
# The Cognito middleware stores the verified `sub` here per request
# (ContextVar propagates into tool handlers — confirmed). The trip space
# is SHARED, so identity does NOT route partitions; it only labels rows
# (`created_by`) so a shared trip shows who recorded/edited what.
_authenticated_sub: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "authenticated_sub", default=None
)


def _parse_sub_user_map(raw: str) -> dict[str, str]:
    """Parse "sub1:megan,sub2:jim" into {sub: friendly_id}."""
    out: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        sub, name = pair.split(":", 1)
        if sub.strip() and name.strip():
            out[sub.strip()] = name.strip()
    return out


_SUB_USER_MAP = _parse_sub_user_map(os.environ.get("MCP_SUB_USER_MAP", ""))


def _created_by_from_context() -> str:
    """Attribution label for the current request's writer.

    Maps the verified Cognito sub → friendly id. Falls back to the raw
    sub if unmapped (still attributable — the shared partition means an
    unmapped-but-allowlisted user can't misroute, so we don't hard-fail
    on a missing label), then to the shared id if there's no sub at all
    (only reachable off-request, e.g. local tests).
    """
    sub = _authenticated_sub.get()
    if not sub:
        return _SHARED_USER_ID
    return _SUB_USER_MAP.get(sub, sub)


def _creator_alert_seed() -> set[str] | None:
    """Default alert_subscribers for a NEW plan row: the verified caller's
    sub, when the caller isn't the shared-partition owner — so a family
    member's plans alert them without a separate opt-in step. The owner is
    an implicit recipient and is never stored (see _build_plan_item).
    """
    sub = _authenticated_sub.get()
    if not sub or _SUB_USER_MAP.get(sub) == _SHARED_USER_ID:
        return None
    return {sub}


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

# Wire shared impls to this host's accessors (S3 snapshot, Lambda-role
# table, bundled locations), then register the shared get_planning_context
# tool whose docstring/signature are defined once in _tool_impls.
_tool_impls.configure(
    # Late-bound (lambdas, not bare refs) so test monkeypatching of these
    # accessors on this module is picked up by the shared _tool_impls helpers.
    ddb_table=lambda: _ddb_table(),
    snapshot=lambda: _snapshot(),
    locations=lambda: _locations(),
)
mcp.tool()(get_planning_context)


@mcp.tool()
def hello_magic_monitor() -> str:
    """Sanity-check that the Magic Monitor MCP server is loaded and reachable.

    Returns a short greeting confirming the wiring works. Useful first
    call from any new client to verify auth + transport layers.
    """
    return (
        "Hello from Magic Monitor (HTTP transport) — MCP wiring works "
        "(auth + transport verified). The full tool surface is live over "
        "this transport: live-status + analytics reads, get_planning_context "
        "(one-shot planner context), and the multi-day trip-planner write "
        "tools (record_plan, create_trip, activate_plan, the plan-edit + "
        "outcome tools, and get_plan_for_day / get_upcoming_trip / "
        "get_user_plan_history). Call tools/list for the authoritative set — "
        "don't infer tool availability from this message."
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
        # The HTTP v1 doesn't ship the analytics snapshot to Lambda, so we
        # resolve ride_name against live STATE rows. Fetch them via GSI
        # partition Queries (one per park, ~150 rows total) — NOT a
        # full-table Scan, which pages the whole multi-GB table (~20s by
        # mid-2026) to find them.
        items = _all_park_state_rows_via_gsi(_ddb_table())
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
        # GSI Query on park_key+SK="STATE" — the per-park partition read web
        # getParkRides moved to on 2026-05-25. Replaces the paginated
        # full-table Scan (correct but O(table size) — ~20s once the table
        # passed ~3M rows / 0.69GB in mid-2026).
        items = _park_state_rows_via_gsi(table, park_key)
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
            the HIST# retention window (~5 years).

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


# ─── Plan / trip write tools (M5) ───────────────────────────────────
# Shared trip space: every write lands in USER#<_SHARED_USER_ID>. The
# writer's identity is NOT a client param (a crafted call mustn't write
# as someone else) — it's the verified token's sub, mapped to a friendly
# created_by label. Otherwise these mirror server.py's plan tools.


@mcp.tool()
def record_plan(
    park: str,
    ride_sequence: list[dict[str, Any]],
    show_selections: list[dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
    notes: str | None = None,
    planned_for_date: str | None = None,
    trip_id: str | None = None,
    plan_window: dict[str, Any] | None = None,
    active: bool | None = None,
    ll_holds: dict[str, str] | None = None,
    reservations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Persist a plan the user accepted (same-day live-assist, or a
    future day of a trip).

    Call AFTER the user accepts a plan you laid out. By default records a
    plan for TODAY, which auto-activates (the poller immediately watches
    it for live disruption alerts). To pre-build a FUTURE trip day, pass
    `planned_for_date` (+ usually `trip_id`); that row stays DORMANT (no
    alerts) until activate_plan is called on the day. For a whole trip at
    once, prefer create_trip.

    The plan is written to the shared family trip space and stamped with
    who recorded it (from your login). TTL keys to the trip day: an
    un-recorded plan survives a couple days past it, then auto-cleans.

    Args:
        park: Park key or human name.
        ride_sequence: Ordered rides; each {"ride_name","ride_id",
            "predicted_wait_min"?, "position"?}. Include ride_id so the
            poller can match plans against live DOWN/UP events.
            Two more per-ride fields the alert engine + trip page READ
            (put them here, NOT in notes — free text is invisible to
            both): `target_time` — when you suggest doing the ride
            ("10:00 AM", "14:30", or ISO; shown on the trip page);
            `ll_planned` (bool) — you INTEND to ride via a Lightning
            Lane not yet booked (predicted_wait_min is LL-priced).
            ll_planned rides are excluded from the "busier than
            planned" drift math — without the flag their tiny LL
            prediction is compared to standby and fires false "running
            behind" alerts. Once booked, use ll_holds / set_held_ll.
        show_selections: Optional shows being fitted in.
        context: Optional planner-side snapshot (park_load_ratio, weather,
            planned_at, ...).
        notes: Optional user constraints ("dining at 6pm").
        planned_for_date: ISO date the plan is FOR. Defaults to today (ET).
        trip_id: Optional. Groups this day into a multi-day trip.
        plan_window: Optional {"open","close"} ET window; once set +
            activated, alerts fire only inside it.
        active: Override the default (same-day active / future dormant).
        ll_holds: Lightning Lanes the party ALREADY HOLDS (pre-booked
            MLL/ILL), as {ride name or ride_id: return time} — times in
            any natural form ("10:00 AM", "14:30", full ISO). **If the
            plan mentions a booked LL, it MUST go here (or via a
            set_held_ll call after)** — LL times written only into
            `notes` or per-ride notes are INVISIBLE to the trip page and
            the alert engine (earlier-LL suppression, plan-drift math,
            nudge timing all read the structured ll_holds field, not
            free text). Only include LLs actually booked — aspirational
            "grab it later" LLs stay out so earlier-slot alerts still
            fire while hunting them. Bad entries (unknown ride,
            unparseable time) fail the whole call loudly.
        reservations: Dining and other booked reservations for the day:
            [{"name": str, "time": "12:30 PM" | ISO, "type"?: str,
            "notes"?: str}]. **Booked meals / reservations MUST go
            here, not into notes** — the trip page renders this field.
            Same fail-loud rule for bad entries.

    Returns:
        Dict with plan_id, planned_for_date, trip_id, active,
        expires_at_epoch, created_by, and a next-step hint.
    """
    try:
        park_key = _normalize_park(park)
    except ValueError as e:
        return {"error": str(e)}

    now_utc = datetime.now(timezone.utc)
    # Normalize context.planned_at before it becomes the PLAN# sort key.
    # A naive timestamp here (e.g. "2026-06-09T18:00") parses fine but later
    # crashes get_user_plan_history's aware-minus-naive date math; a reused
    # snapshot across two days collides SKs. Require an aware ISO-8601
    # datetime (coerce naive → UTC), reject anything unparseable.
    raw_planned_at = (context or {}).get("planned_at")
    if raw_planned_at:
        try:
            parsed_planned_at = datetime.fromisoformat(raw_planned_at)
        except (ValueError, TypeError):
            return {
                "error": "Invalid context.planned_at",
                "error_message": (
                    f"Could not parse planned_at {raw_planned_at!r}. Use an "
                    "ISO-8601 timestamp like 2026-06-09T18:00:00+00:00."
                ),
            }
        if parsed_planned_at.tzinfo is None:
            parsed_planned_at = parsed_planned_at.replace(tzinfo=timezone.utc)
        plan_ts = parsed_planned_at.isoformat()
    else:
        plan_ts = now_utc.isoformat()
    pfd = planned_for_date or _today_et_date_iso()
    # Normalize to bare YYYY-MM-DD so a datetime form can't be stored
    # verbatim and silently never match the date string-equality used for
    # activation + get_plan_for_day. (Mirrors server.py.)
    try:
        pfd = datetime.fromisoformat(pfd).date().isoformat()
    except ValueError:
        return {
            "error": "Invalid planned_for_date",
            "error_message": f"Could not parse '{planned_for_date}'. Use YYYY-MM-DD.",
        }

    # Resolve pre-booked Lightning Lanes against the plan's own rides
    # BEFORE any write — a bad entry fails the whole call (see
    # resolve_ll_holds: silent hold loss is the 2026-07-04 bug class).
    resolved_holds, holds_err = _tool_impls.resolve_ll_holds(
        ll_holds, ride_sequence, pfd
    )
    if holds_err is not None:
        return holds_err
    # Normalize per-ride target_time (+ ll_planned) and reservations the
    # same fail-loud way.
    targets_err = _tool_impls.normalize_ride_targets(ride_sequence, pfd)
    if targets_err is not None:
        return targets_err
    resolved_res, res_err = _tool_impls.resolve_reservations(reservations, pfd)
    if res_err is not None:
        return res_err

    try:
        table = _ddb_table()
    except Exception as e:
        err = _aws_error_payload(e)
        return err if err is not None else {
            "error": "Plan write failed", "error_message": str(e),
        }

    # Upsert per day: update an existing un-recorded plan for this same
    # (planned_for_date, trip_id) in place rather than appending a dup
    # row (re-recording a pre-built trip day, e.g. to add shows, used to
    # create a second row). Prefer active, else most-recent, if dups exist.
    try:
        existing = [
            r for r in (
                _convert_decimals(x) for x in _query_shared_prefix(table, "PLAN#")
            )
            if r.get("planned_for_date") == pfd
            and r.get("trip_id") == trip_id
            and not r.get("outcome_recorded")
        ]
        dedup_unchecked = False
    except Exception:
        # Dedup read failed → can't tell if a row for this day exists, so we
        # insert (possible duplicate day). Surface a warning rather than do
        # it silently. (Mirrors server.py; /trips dedupes on read.)
        existing = []
        dedup_unchecked = True
    prior = None
    if existing:
        existing.sort(
            key=lambda r: (bool(r.get("active")), r.get("planned_at") or r["SK"]),
            reverse=True,
        )
        prior = existing[0]
        plan_ts = prior["SK"][len("PLAN#"):]

    if active is None:
        active = pfd == _today_et_date_iso()
    activated_at = now_utc.isoformat() if active else None
    created_by = _created_by_from_context()

    item = _build_plan_item(
        user_id=_SHARED_USER_ID,
        park_key=park_key,
        ride_sequence=ride_sequence,
        planned_for_date=pfd,
        plan_ts=plan_ts,
        show_selections=show_selections,
        context=context,
        notes=notes,
        trip_id=trip_id,
        plan_window=plan_window,
        active=active,
        activated_at=activated_at,
        created_by=created_by,
        alert_subscribers=_creator_alert_seed(),
    )
    if resolved_holds:
        item["ll_holds"] = resolved_holds
    if resolved_res:
        item["reservations"] = resolved_res

    if prior is not None:
        # Upsert means "set this day's plan", but put_item replaces the whole
        # row and _build_plan_item hardcodes completed_rides / dropped_rides
        # to []. Without this merge, re-recording a day after rides were
        # marked complete silently wipes calibration data. Carry mid-trip
        # execution state forward, plus an already-resolved plan_window /
        # activation the caller didn't re-specify.
        item["completed_rides"] = prior.get("completed_rides") or []
        item["dropped_rides"] = prior.get("dropped_rides") or []
        # Held LLs set earlier (set_held_ll / the web) survive a re-record
        # unless this call explicitly provides its own ll_holds map.
        if ll_holds is None and prior.get("ll_holds"):
            item["ll_holds"] = prior.get("ll_holds")
        # Reservations follow the same keep-unless-respecified rule.
        if reservations is None and prior.get("reservations"):
            item["reservations"] = prior.get("reservations")
        if plan_window is None and prior.get("plan_window") is not None:
            item["plan_window"] = prior.get("plan_window")
        if active and prior.get("active") and prior.get("activated_at"):
            item["activated_at"] = prior.get("activated_at")
        # Same wipe hazard for alert opt-ins: subscribed family members
        # must survive a re-record of the day. Merge prior subs with any
        # fresh seed (e.g. a different creator re-recording).
        prior_subs = set(prior.get("alert_subscribers") or ())
        merged_subs = prior_subs | set(item.get("alert_subscribers") or ())
        if merged_subs:
            item["alert_subscribers"] = merged_subs

    try:
        table.put_item(Item=_floats_to_decimals(item))
    except Exception as e:
        err = _aws_error_payload(e)
        return err if err is not None else {
            "error": "Plan write failed", "error_message": str(e),
        }

    if active:
        hint = (
            "Plan saved and ACTIVE — Magic Monitor is now watching its "
            "rides for live disruptions. Call record_plan_outcome at "
            "end-of-day."
        )
    else:
        hint = (
            f"Future plan saved as DORMANT for {pfd} (no alerts yet). On "
            f"that day, call activate_plan to re-evaluate against live "
            f"conditions and start monitoring."
        )

    result = {
        "plan_id": plan_ts,
        "planned_for_date": pfd,
        "trip_id": trip_id,
        "active": active,
        "park_key": park_key,
        "created_by": created_by,
        "expires_at_epoch": item["ttl"],
        "next_step_hint": hint,
    }
    if resolved_holds:
        result["ll_holds_recorded"] = resolved_holds
    if dedup_unchecked:
        result["warning"] = (
            "Couldn't check for an existing plan on this day (read failed), "
            "so this was inserted as a new row — a duplicate day is possible."
        )
    return result


@mcp.tool()
def create_trip(name: str, days: list[dict[str, Any]]) -> dict[str, Any]:
    """Pre-build a whole multi-day trip: a TRIP header + one dormant
    day-plan per date, in the shared trip space.

    Use this when the user wants to plan an upcoming trip ahead of time.
    Each day is DORMANT (no alerts) until activate_plan on its day.

    Args:
        name: Human label ("June 2026 family trip").
        days: Ordered list, one per day. Each: {"date":"YYYY-MM-DD",
            "park": key/name, "ride_sequence"?:[...], "show_selections"?,
            "plan_window"?:{open,close}, "notes"?}.

    Returns:
        Dict with trip_id, name, start_date, end_date, days [{date,
        park_key, plan_id}], created_by. All validation happens before
        any write — no partial trip on error.
    """
    if not days:
        return {"error": "A trip needs at least one day",
                "error_message": "days was empty."}

    normalized: list[dict[str, Any]] = []
    for i, day in enumerate(days):
        date_str = (day or {}).get("date")
        park = (day or {}).get("park")
        if not date_str or not park:
            return {"error": "Each day needs 'date' and 'park'",
                    "error_message": f"day index {i} missing date/park: {day!r}"}
        try:
            date_str = datetime.fromisoformat(date_str).date().isoformat()
        except ValueError:
            return {"error": "Invalid day date",
                    "error_message": f"day {i}: could not parse '{date_str}'. Use YYYY-MM-DD."}
        try:
            park_key = _normalize_park(park)
        except ValueError as e:
            return {"error": "Invalid day park", "error_message": f"day {i}: {e}"}
        normalized.append({
            "date": date_str,
            "park_key": park_key,
            "ride_sequence": day.get("ride_sequence") or [],
            "show_selections": day.get("show_selections") or [],
            "plan_window": day.get("plan_window"),
            "notes": day.get("notes"),
        })

    normalized.sort(key=lambda d: d["date"])
    start_date = normalized[0]["date"]
    end_date = normalized[-1]["date"]
    now_utc = datetime.now(timezone.utc)
    trip_id = f"{start_date}_{int(now_utc.timestamp())}"
    created_by = _created_by_from_context()

    try:
        end_d = datetime.fromisoformat(end_date).date()
        header_ttl = int(datetime.combine(
            end_d + timedelta(days=_TRIP_BUFFER_DAYS + 1),
            time(_PARK_DAY_BOUNDARY_HOUR), tzinfo=_EASTERN,
        ).astimezone(timezone.utc).timestamp())
    except ValueError:
        header_ttl = _epoch_now() + _PLAN_RECORDED_TTL_SECS

    header = {
        "PK": f"USER#{_SHARED_USER_ID}",
        "SK": f"TRIP#{trip_id}",
        "name": name,
        "start_date": start_date,
        "end_date": end_date,
        "days": [{"date": d["date"], "park_key": d["park_key"]} for d in normalized],
        "created_by": created_by,
        "created_at": now_utc.isoformat(),
        "ttl": header_ttl,
    }

    day_results: list[dict[str, Any]] = []
    try:
        table = _ddb_table()
        with table.batch_writer() as batch:
            batch.put_item(Item=_floats_to_decimals(header))
            for i, d in enumerate(normalized):
                plan_ts = (now_utc + timedelta(microseconds=i + 1)).isoformat()
                item = _build_plan_item(
                    user_id=_SHARED_USER_ID,
                    park_key=d["park_key"],
                    ride_sequence=d["ride_sequence"],
                    planned_for_date=d["date"],
                    plan_ts=plan_ts,
                    show_selections=d["show_selections"],
                    notes=d["notes"],
                    trip_id=trip_id,
                    plan_window=d["plan_window"],
                    active=False,
                    created_by=created_by,
                    alert_subscribers=_creator_alert_seed(),
                )
                batch.put_item(Item=_floats_to_decimals(item))
                day_results.append({"date": d["date"], "park_key": d["park_key"],
                                    "plan_id": plan_ts})
    except Exception as e:
        err = _aws_error_payload(e)
        return err if err is not None else {
            "error": "Trip write failed", "error_message": str(e),
        }

    return {
        "trip_id": trip_id,
        "name": name,
        "start_date": start_date,
        "end_date": end_date,
        "days": day_results,
        "created_by": created_by,
        "next_step_hint": (
            "Trip created — all days dormant (no alerts). Refine each day's "
            "rides with record_plan (same trip_id) or add_ride_to_plan. On "
            "each trip day, call activate_plan to re-evaluate + monitor."
        ),
    }


@mcp.tool()
def get_plan_for_day(date: str | None = None) -> dict[str, Any]:
    """Return the shared plan recorded for a day (default today).

    Works for ANY date — pass `date` to answer "what's the plan for
    June 14?". Prefers the ACTIVE plan for the date, else the most
    recently recorded. A future date is a READ-ONLY lookup: show the
    pre-built ride list, don't activate it (activation happens on the
    day), and don't present today's live waits as that future date's. On
    the trip day: pull up, re-check vs live conditions, then activate.
    Mid-day: see what's left.

    Args:
        date: ISO date (YYYY-MM-DD). Defaults to today (ET).

    Returns:
        Dict with date, found (bool), and when found plan_id + full plan
        body (park_key, trip_id, active, activated_at, plan_window,
        ride_sequence, completed_rides, dropped_rides, show_selections,
        notes, created_by, outcome_recorded).

        `ride_sequence` is the EFFECTIVE plan: rides the family dropped
        from the phone via the /replan flow are already removed and listed
        separately under `dropped_via_replan`. Re-plan around what's in
        ride_sequence; adding one of the dropped rides back (add_ride_to_
        plan) automatically un-drops it.
    """
    target = date or _today_et_date_iso()
    try:
        target = datetime.fromisoformat(target).date().isoformat()
    except ValueError:
        return {"error": "Invalid date",
                "error_message": f"Could not parse '{date}'. Use YYYY-MM-DD."}

    try:
        table = _ddb_table()
        items: list[dict] = []
        kwargs = {
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
            "ExpressionAttributeValues": {":pk": f"USER#{_SHARED_USER_ID}", ":sk": "PLAN#"},
        }
        while True:
            resp = table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except Exception as e:
        err = _aws_error_payload(e)
        return err if err is not None else {
            "error": "Plan read failed", "error_message": str(e),
        }

    matches = [
        _convert_decimals(it) for it in items
        if it.get("planned_for_date") == target
    ]
    if not matches:
        return {"date": target, "found": False, "note": f"No plan recorded for {target}."}

    matches.sort(key=lambda it: it.get("planned_at") or it["SK"], reverse=True)
    chosen = next((m for m in matches if m.get("active")), matches[0])

    # Present the EFFECTIVE sequence: rides the family dropped from the
    # phone via /replan are removed from ride_sequence and surfaced under
    # dropped_via_replan, so this matches what the poller actually watches.
    still_planned, dropped_via_replan = _tool_impls.split_dropped_rides(chosen)

    return {
        "date": target,
        "found": True,
        "plan_id": chosen["SK"][len("PLAN#"):],
        "trip_id": chosen.get("trip_id"),
        "park_key": chosen.get("park_key"),
        "active": bool(chosen.get("active")),
        "activated_at": chosen.get("activated_at"),
        "plan_window": chosen.get("plan_window"),
        "ride_sequence": still_planned,
        "dropped_via_replan": dropped_via_replan,
        # ride_id the family marked "do next" from the phone (/replan), or
        # None — honor it when re-sequencing.
        "next_up": chosen.get("next_up"),
        # {ride_id: LL return ISO} for rides the party HOLDS a Lightning
        # Lane on (set via set_held_ll). Those rides' predicted waits are
        # LL returns, not standby — don't treat their standby as a signal.
        "held_lls": chosen.get("ll_holds", {}),
        # {ride_id: actual wait min} captured from the phone on Mark done —
        # calibration signal (predicted vs actual) for done-via-web rides.
        "actual_waits": chosen.get("actual_waits", {}),
        "completed_rides": chosen.get("completed_rides", []),
        "dropped_rides": chosen.get("dropped_rides", []),
        "show_selections": chosen.get("show_selections", []),
        "notes": chosen.get("notes"),
        "created_by": chosen.get("created_by"),
        "outcome_recorded": bool(chosen.get("outcome_recorded")),
        "other_plans_for_day": len(matches) - 1,
    }


def _query_shared_prefix(table, sk_prefix: str) -> list[dict]:
    """All items under the shared USER# partition whose SK begins_with
    sk_prefix, fully paginated. Used by the trip read/delete tools."""
    items: list[dict] = []
    kwargs = {
        "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
        "ExpressionAttributeValues": {
            ":pk": f"USER#{_SHARED_USER_ID}", ":sk": sk_prefix
        },
    }
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return items


@mcp.tool()
def get_upcoming_trip() -> dict[str, Any]:
    """Return the soonest upcoming (or in-progress) shared trip + its days.

    Use at session start to surface a trip in progress. Returns the
    nearest trip whose end_date >= today with each day's park + whether
    that day's plan is active or still dormant.

    Returns:
        Dict with found (bool); when found: trip_id, name, start_date,
        end_date, days [{date, park_key, plan_id, active, ride_count,
        outcome_recorded}].
    """
    today = _today_et_date_iso()
    try:
        table = _ddb_table()
        trip_items = _query_shared_prefix(table, "TRIP#")
        plan_items = _query_shared_prefix(table, "PLAN#")
    except Exception as e:
        err = _aws_error_payload(e)
        return err if err is not None else {
            "error": "Trip read failed", "error_message": str(e),
        }

    # PLAN# rows are the source of truth for trip membership + date range;
    # the TRIP# header's days/start/end are a creation-time snapshot that
    # can drift, so derive rather than trust them.
    by_trip: dict[str, list[dict]] = {}
    for it in plan_items:
        it = _convert_decimals(it)
        tid = it.get("trip_id")
        if tid:
            by_trip.setdefault(tid, []).append(it)

    candidates = []
    for hdr in trip_items:
        hdr = _convert_decimals(hdr)
        trip_id = hdr["SK"][len("TRIP#"):]
        rows = by_trip.get(trip_id, [])
        dates = sorted(r["planned_for_date"] for r in rows if r.get("planned_for_date"))
        if not dates or dates[-1] < today:
            continue
        candidates.append((dates[0], trip_id, hdr, rows))

    if not candidates:
        return {"found": False, "note": "No upcoming trip."}
    candidates.sort(key=lambda c: c[0])
    _start, trip_id, hdr, rows = candidates[0]

    # Collapse to one row per date (prefer active, else most-recent) —
    # defensive against duplicate day rows.
    by_date: dict[str, dict] = {}
    for r in rows:
        d = r.get("planned_for_date")
        cur = by_date.get(d)
        if cur is None or (bool(r.get("active")), r["SK"]) > (
            bool(cur.get("active")), cur["SK"]
        ):
            by_date[d] = r
    rows = sorted(by_date.values(), key=lambda r: r.get("planned_for_date") or "")

    days_out = [{
        "date": r.get("planned_for_date"),
        "park_key": r.get("park_key"),
        "plan_id": r["SK"][len("PLAN#"):],
        "active": bool(r.get("active")),
        # Effective count — rides dropped via /replan don't count.
        "ride_count": len(_tool_impls.split_dropped_rides(r)[0]),
        "outcome_recorded": bool(r.get("outcome_recorded")),
    } for r in rows]

    return {
        "found": True,
        "trip_id": trip_id,
        "name": hdr.get("name"),
        "start_date": days_out[0]["date"],
        "end_date": days_out[-1]["date"],
        "days": days_out,
    }


@mcp.tool()
def delete_trip(trip_id: str, force: bool = False) -> dict[str, Any]:
    """Delete a whole shared trip — its TRIP# header AND every PLAN# day
    row under it, in one cascade.

    Use when the user cancels/scraps a trip. To drop just one day, use
    delete_plan. Guardrail: REFUSES if any day has a recorded outcome
    (calibration history) unless force=True — surface the refusal and
    confirm before retrying with force.

    Args:
        trip_id: The trip to delete.
        force: Delete even if some days have recorded outcomes (default False).

    Returns:
        Dict with ok, trip_id, deleted_days, deleted_header. On the
        guardrail trip: error + days_with_outcomes.
    """
    try:
        table = _ddb_table()
        plan_rows = [
            it for it in (
                _convert_decimals(x) for x in _query_shared_prefix(table, "PLAN#")
            )
            if it.get("trip_id") == trip_id
        ]
        header = table.get_item(
            Key={"PK": f"USER#{_SHARED_USER_ID}", "SK": f"TRIP#{trip_id}"}
        ).get("Item")
    except Exception as e:
        err = _aws_error_payload(e)
        return err if err is not None else {"error": "Trip read failed", "error_message": str(e)}

    if header is None and not plan_rows:
        return {"error": "Trip not found",
                "error_message": f"No trip '{trip_id}'.", "trip_id": trip_id}

    recorded = sorted(
        d for d in (r.get("planned_for_date") for r in plan_rows
                    if r.get("outcome_recorded")) if d
    )
    if recorded and not force:
        return {
            "error": "Trip has recorded outcomes",
            "error_message": (
                f"{len(recorded)} day(s) have recorded outcomes "
                f"({', '.join(recorded)}) — deleting loses calibration history. "
                f"Confirm with the user, then retry with force=True."
            ),
            "trip_id": trip_id,
            "days_with_outcomes": recorded,
        }

    try:
        with table.batch_writer() as batch:
            for r in plan_rows:
                batch.delete_item(Key={"PK": r["PK"], "SK": r["SK"]})
            if header is not None:
                batch.delete_item(Key={"PK": f"USER#{_SHARED_USER_ID}", "SK": f"TRIP#{trip_id}"})
    except Exception as e:
        err = _aws_error_payload(e)
        return err if err is not None else {"error": "Trip delete failed", "error_message": str(e)}

    return {"ok": True, "trip_id": trip_id, "deleted_days": len(plan_rows),
            "deleted_header": header is not None}


@mcp.tool()
def delete_plan(plan_id: str, force: bool = False) -> dict[str, Any]:
    """Delete a single day's plan (one PLAN# row) from the shared space.

    Use to drop one day from a trip or remove a standalone plan; for a
    whole trip use delete_trip. Guardrail: refuses if the plan has a
    recorded outcome unless force=True.

    Args:
        plan_id: The plan to delete (the PLAN# suffix).
        force: Delete even if an outcome is recorded (default False).

    Returns:
        Dict with ok, plan_id, planned_for_date, trip_id. Error payload if
        not found or blocked by the outcome guardrail.
    """
    sk = _coerce_plan_id_to_sk(plan_id)
    try:
        table = _ddb_table()
        item = table.get_item(Key={"PK": f"USER#{_SHARED_USER_ID}", "SK": sk}).get("Item")
    except Exception as e:
        err = _aws_error_payload(e)
        return err if err is not None else {"error": "Plan read failed", "error_message": str(e)}
    if item is None:
        return {"error": "Plan not found",
                "error_message": f"No plan with id '{plan_id}'.", "plan_id": plan_id}
    item = _convert_decimals(item)
    if item.get("outcome_recorded") and not force:
        return {
            "error": "Plan has a recorded outcome",
            "error_message": (
                f"Plan '{plan_id}' ({item.get('planned_for_date')}) has a recorded "
                f"outcome — deleting loses calibration history. Confirm, then retry "
                f"with force=True."
            ),
            "plan_id": plan_id,
        }
    try:
        table.delete_item(Key={"PK": f"USER#{_SHARED_USER_ID}", "SK": sk})
    except Exception as e:
        err = _aws_error_payload(e)
        return err if err is not None else {"error": "Plan delete failed", "error_message": str(e)}
    return {"ok": True, "plan_id": plan_id,
            "planned_for_date": item.get("planned_for_date"), "trip_id": item.get("trip_id")}


@mcp.tool()
def update_trip(trip_id: str, name: str) -> dict[str, Any]:
    """Rename a shared trip (sets the TRIP# header's name).

    A trip's days + dates are DERIVED from its day plans, so they change
    by adding/removing days (record_plan with the trip_id / delete_plan),
    not here — this only updates the human label.

    Args:
        trip_id: The trip to rename.
        name: New label ("June 2026 family trip").

    Returns:
        Dict with ok, trip_id, name. Error payload if the trip isn't found.
    """
    try:
        table = _ddb_table()
        table.update_item(
            Key={"PK": f"USER#{_SHARED_USER_ID}", "SK": f"TRIP#{trip_id}"},
            UpdateExpression="SET #n = :n",
            ExpressionAttributeNames={"#n": "name"},
            ExpressionAttributeValues={":n": name},
            ConditionExpression="attribute_exists(PK)",
        )
    except Exception as e:
        err = _aws_error_payload(e)
        if err is not None:
            return err
        if "ConditionalCheckFailedException" in str(e):
            return {"error": "Trip not found",
                    "error_message": f"No trip '{trip_id}'.", "trip_id": trip_id}
        return {"error": "Trip update failed", "error_message": str(e)}
    return {"ok": True, "trip_id": trip_id, "name": name}


@mcp.tool()
def activate_plan(
    plan_id: str | None = None,
    date: str | None = None,
    ride_sequence: list[dict[str, Any]] | None = None,
    plan_window: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Activate a day's plan: turn on live disruption monitoring after
    re-evaluating against live conditions.

    On the trip day, AFTER pulling the plan up (get_plan_for_day) and
    re-checking it against get_planning_context (what's DOWN now, today's
    forecast, weather, hours) and the user accepts the adjusted plan,
    call this to flip the plan ACTIVE (poller starts firing disruption
    alerts) and store the re-evaluated `ride_sequence` + resolved
    `plan_window`. A dormant future plan fires nothing until activated.

    Args:
        plan_id: The plan to activate. If omitted, the plan for `date`
            (default today) is looked up.
        date: ISO date to look up the plan by, if plan_id isn't given.
        ride_sequence: Optional accepted re-evaluated ride order.
        plan_window: Optional {"open","close"} resolved ET window.

    Returns:
        Dict with plan_id, active=true, activated_at, planned_for_date,
        plan_window, ride_count.
    """
    planned_for_date = None
    if not plan_id:
        lookup = get_plan_for_day(date=date)
        if lookup.get("error"):
            return lookup
        if not lookup.get("found"):
            return {"error": "No plan to activate",
                    "error_message": lookup.get("note") or f"No plan for {date or 'today'}."}
        plan_id = lookup["plan_id"]
        planned_for_date = lookup.get("planned_for_date")

    sk = _coerce_plan_id_to_sk(plan_id)

    # Refuse early activation of a future-dated plan (mirrors server.py): it
    # would fire disruption alerts weeks ahead. Best-effort read of the date
    # when plan_id was passed directly.
    if planned_for_date is None:
        try:
            row = _ddb_table().get_item(
                Key={"PK": f"USER#{_SHARED_USER_ID}", "SK": sk}
            ).get("Item")
            if row:
                planned_for_date = _convert_decimals(row).get("planned_for_date")
        except Exception:
            planned_for_date = None
    if planned_for_date and planned_for_date > _today_et_date_iso():
        return {
            "error": "Plan is future-dated",
            "error_message": (
                f"This plan is for {planned_for_date}, not today "
                f"({_today_et_date_iso()}). Activate it on the day so "
                f"monitoring doesn't start firing weeks early."
            ),
        }

    now_iso = datetime.now(timezone.utc).isoformat()
    set_parts = ["active = :a", "activated_at = :at"]
    expr_values: dict[str, Any] = {":a": True, ":at": now_iso}
    if ride_sequence is not None:
        set_parts.append("ride_sequence = :seq")
        expr_values[":seq"] = ride_sequence
    if plan_window is not None:
        set_parts.append("plan_window = :win")
        expr_values[":win"] = plan_window
    update_expr = "SET " + ", ".join(set_parts)

    try:
        resp = _ddb_table().update_item(
            Key={"PK": f"USER#{_SHARED_USER_ID}", "SK": sk},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=_floats_to_decimals(expr_values),
            ConditionExpression="attribute_exists(PK)",
            ReturnValues="ALL_NEW",
        )
    except Exception as e:
        err = _aws_error_payload(e)
        if err is not None:
            return err
        if "ConditionalCheckFailedException" in str(e):
            return {"error": "Plan not found",
                    "error_message": f"No plan with id '{plan_id}'.", "plan_id": plan_id}
        return {"error": "Plan activation failed", "error_message": str(e)}

    attrs = _convert_decimals(resp.get("Attributes", {}))
    return {
        "plan_id": plan_id,
        "active": True,
        "activated_at": now_iso,
        "planned_for_date": attrs.get("planned_for_date"),
        "plan_window": attrs.get("plan_window"),
        "ride_count": len(attrs.get("ride_sequence") or []),
        "note": (
            "Plan activated — Magic Monitor is now watching its rides for "
            "live disruptions"
            + (" within the plan window." if attrs.get("plan_window") else ".")
        ),
    }


@mcp.tool()
def record_plan_outcome(
    plan_id: str,
    aggression_rating: str | None = None,
    timing_rating: str | None = None,
    extra_time_minutes: int | None = None,
    per_item_feedback: dict[str, dict[str, Any]] | None = None,
    free_text: str | None = None,
) -> dict[str, Any]:
    """Log how a recorded plan actually went (feeds calibration).

    Call when the user reports outcomes ("Big Thunder was 60 not 40",
    "we're done, that worked great"). Needs a plan_id — find it via
    get_plan_for_day or get_user_plan_history if not in context.

    Args:
        plan_id: From record_plan / get_plan_for_day.
        aggression_rating: "too_aggressive" | "about_right" |
            "not_aggressive_enough".
        timing_rating: "ran_over" | "on_time" | "extra_time".
        extra_time_minutes: When timing="extra_time", ~how much was left.
        per_item_feedback: Optional per-ride/show {actual_wait_min,
            arrived_with_min, notes}, keyed by name.
        free_text: Catch-all notes.

    Returns:
        Dict with plan_id, outcome_recorded=true, new_expires_at_epoch.
    """
    # Validate rating enums before writing — an off-enum value is stored
    # then silently dropped by the calibration aggregator. (Mirrors
    # server.py.)
    if aggression_rating is not None and aggression_rating not in _AGGRESSION_VALUES:
        return {
            "error": "Invalid aggression_rating",
            "error_message": (
                f"{aggression_rating!r} is not valid. Use one of: "
                f"{sorted(_AGGRESSION_VALUES)}."
            ),
        }
    if timing_rating is not None and timing_rating not in _TIMING_VALUES:
        return {
            "error": "Invalid timing_rating",
            "error_message": (
                f"{timing_rating!r} is not valid. Use one of: "
                f"{sorted(_TIMING_VALUES)}."
            ),
        }

    sk = _coerce_plan_id_to_sk(plan_id)
    now_iso = datetime.now(timezone.utc).isoformat()
    new_ttl = _epoch_now() + _PLAN_RECORDED_TTL_SECS

    set_parts = ["outcome_recorded = :rec", "outcome_recorded_at = :rat", "#ttl = :ttl"]
    expr_values: dict[str, Any] = {":rec": True, ":rat": now_iso, ":ttl": new_ttl}
    expr_names = {"#ttl": "ttl"}
    if aggression_rating is not None:
        set_parts.append("aggression_rating = :agg")
        expr_values[":agg"] = aggression_rating
    if timing_rating is not None:
        set_parts.append("timing_rating = :tim")
        expr_values[":tim"] = timing_rating
    if extra_time_minutes is not None:
        set_parts.append("extra_time_minutes = :etm")
        expr_values[":etm"] = extra_time_minutes
    if per_item_feedback is not None:
        set_parts.append("per_item_feedback = :pif")
        expr_values[":pif"] = per_item_feedback
    if free_text is not None:
        set_parts.append("free_text = :ftx")
        expr_values[":ftx"] = free_text

    try:
        _ddb_table().update_item(
            Key={"PK": f"USER#{_SHARED_USER_ID}", "SK": sk},
            UpdateExpression="SET " + ", ".join(set_parts),
            ExpressionAttributeValues=_floats_to_decimals(expr_values),
            ExpressionAttributeNames=expr_names,
            ConditionExpression="attribute_exists(PK)",
        )
    except Exception as e:
        err = _aws_error_payload(e)
        if err is not None:
            return err
        if "ConditionalCheckFailedException" in str(e):
            return {"error": "Plan not found",
                    "error_message": f"No plan with id '{plan_id}'.", "plan_id": plan_id}
        return {"error": "Outcome write failed", "error_message": str(e)}

    return {
        "plan_id": plan_id,
        "outcome_recorded": True,
        "outcome_recorded_at": now_iso,
        "new_expires_at_epoch": new_ttl,
    }


@mcp.tool()
def set_plan_alert_subscription(
    member: str,
    subscribed: bool = True,
    trip_id: str | None = None,
    date: str | None = None,
) -> dict[str, Any]:
    """Opt a family member IN (or out) of the disruption/weather/low-wait
    alert pushes for a trip or a single day's plan.

    By default only the shared-partition owner receives plan alerts (the
    owner is always subscribed and can't be removed); a plan's creator is
    auto-subscribed at record time. This adds `member` as an additional
    recipient on the matching plan day rows — same DOWN / BACK UP / storm /
    low-wait pushes.

    Args:
        member: Who to subscribe — a family member's name as configured
            (e.g. "jim"), or their Cognito sub, or any id with a
            USER#<id>/PROFILE row. The member must have signed into the
            dashboard and saved /me once (that's where their Pushover key
            lives) — if they haven't, this errors with instructions.
        subscribed: True to opt in (default), False to opt out.
        trip_id: Apply to EVERY un-recorded day of this trip.
        date: Apply to the single plan for this date (YYYY-MM-DD).
            Provide trip_id or date (or both to filter to one trip day).

    Returns:
        Dict with member (resolved id), subscribed, days_updated, and a
        warning when the member's profile has no Pushover key yet.
    """
    if not trip_id and not date:
        return {
            "error": "Provide trip_id and/or date",
            "error_message": "Say which trip (trip_id) or day (date) to apply to.",
        }
    target_date = None
    if date:
        try:
            target_date = datetime.fromisoformat(date).date().isoformat()
        except ValueError:
            return {
                "error": "Invalid date",
                "error_message": f"Could not parse '{date}'. Use YYYY-MM-DD.",
            }

    try:
        table = _ddb_table()
        # Friendly-name → sub map (reverse of MCP_SUB_USER_MAP) so "jim"
        # resolves to the sub whose /me profile holds his Pushover key.
        friendly_to_sub = {
            friendly.strip().lower(): sub
            for sub, friendly in _SUB_USER_MAP.items()
        }
        member_id, has_key = _resolve_alert_member(table, member, friendly_to_sub)
        if member_id is None:
            return {
                "error": "Member has no profile",
                "error_message": (
                    f"No USER#<id>/PROFILE row found for {member!r}. They need "
                    "to sign into the dashboard once and save /me (name + "
                    "Pushover key) first."
                ),
            }
        if member_id == _SHARED_USER_ID or _SUB_USER_MAP.get(member_id) == _SHARED_USER_ID:
            return {
                "member": member_id,
                "subscribed": True,
                "days_updated": [],
                "note": "The plan owner always receives alerts — nothing to change.",
            }
        rows = [
            r for r in (
                _convert_decimals(x) for x in _query_shared_prefix(table, "PLAN#")
            )
            if not r.get("outcome_recorded")
            and (trip_id is None or r.get("trip_id") == trip_id)
            and (target_date is None or r.get("planned_for_date") == target_date)
        ]
        if not rows:
            return {
                "error": "No matching plans",
                "error_message": (
                    f"No un-recorded plan rows matched trip_id={trip_id!r} "
                    f"date={target_date!r}."
                ),
            }
        days = _apply_alert_subscription(
            table, _SHARED_USER_ID, member_id, subscribed, rows
        )
    except Exception as e:
        err = _aws_error_payload(e)
        return err if err is not None else {
            "error": "Subscription update failed",
            "error_message": str(e),
        }

    out: dict[str, Any] = {
        "member": member_id,
        "subscribed": subscribed,
        "days_updated": sorted(days),
    }
    if subscribed and not has_key:
        out["warning"] = (
            "Subscription stored, but this member's profile has no Pushover "
            "key — they won't receive pushes until they add one at /me."
        )
    return out


@mcp.tool()
def set_held_ll(
    ride: str,
    return_time: str | None = None,
    date: str | None = None,
) -> dict[str, Any]:
    """Record (or clear) a Lightning Lane you HOLD for a planned ride.

    This is the key that makes LL alerts useful. When MM knows you hold an
    LL for a ride at a given return time, it will:
      • only alert about an EARLIER LL when a slot beats the time you hold
        (no more "5 min earlier" pings on a ride you've got hours sooner);
      • exclude that ride from the "busier/lighter than planned" drift math
        (an LL'd ride isn't a standby wait, so its standby number is
        irrelevant to how the plan is going).

    Set this whenever a plan assumes a Lightning Lane for a ride — at plan
    time (the predicted wait is the LL return, not standby) or when the
    user books one during the day ("I got TRON at 3pm").

    Args:
        ride: Ride name or ride_id; must be in the day's plan.
        return_time: The LL return time — "3:00 PM", "3pm", "15:00", or a
            full ISO. OMIT (or null) to CLEAR a held LL for the ride.
        date: Plan date (YYYY-MM-DD). Defaults to today (ET).

    Returns:
        Dict with ride_id, held_return (resolved ISO or null when cleared),
        and days_updated.
    """
    target = date or _today_et_date_iso()
    try:
        target = datetime.fromisoformat(target).date().isoformat()
    except ValueError:
        return {"error": "Invalid date",
                "error_message": f"Could not parse '{date}'. Use YYYY-MM-DD."}
    try:
        table = _ddb_table()
        plans = [
            _convert_decimals(x) for x in _query_shared_prefix(table, "PLAN#")
            if x.get("planned_for_date") == target and not x.get("outcome_recorded")
        ]
        if not plans:
            return {"error": "No plan for that day",
                    "error_message": f"No un-recorded plan found for {target}."}
        plans.sort(key=lambda it: it.get("planned_at") or it["SK"], reverse=True)
        plan = next((p for p in plans if p.get("active")), plans[0])
        # Resolve the ride against the plan's own sequence (name or id).
        q = ride.strip().lower()
        match = next(
            (r for r in plan.get("ride_sequence", [])
             if r.get("ride_id") == ride or (r.get("ride_name") or "").lower() == q
             or q in (r.get("ride_name") or "").lower()),
            None,
        )
        if not match or not match.get("ride_id"):
            return {"error": "Ride not in plan",
                    "error_message": f"'{ride}' isn't in the plan for {target}."}
        ride_id = match["ride_id"]
        held_iso = None
        if return_time:
            held_iso = _tool_impls.parse_ll_time(return_time, target)
            if held_iso is None:
                return {"error": "Invalid return_time",
                        "error_message": f"Could not parse '{return_time}'. "
                                         "Try '3:00 PM' or '15:00'."}
        days = _tool_impls.apply_held_ll(
            table, _SHARED_USER_ID, ride_id, held_iso, [plan]
        )
    except Exception as e:
        err = _aws_error_payload(e)
        return err if err is not None else {
            "error": "Held-LL update failed", "error_message": str(e)}
    return {
        "ride_id": ride_id,
        "ride_name": match.get("ride_name"),
        "held_return": held_iso,
        "cleared": held_iso is None,
        "days_updated": sorted(days),
    }


@mcp.tool()
def mark_ride_complete(
    plan_id: str,
    ride_id: str,
    ride_name: str,
    actual_wait_min: int | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Mark a ride done — moves it from ride_sequence to completed_rides
    (stops its disruption alerts + captures actual_wait_min for
    calibration). Prefer this over remove_ride_from_plan when the user
    actually rode it.

    Args:
        plan_id, ride_id, ride_name: identify the plan + ride.
        actual_wait_min: Optional actual wait (strongest calibration signal).
        notes: Optional free-text.

    Returns:
        Dict with completed (0/1), remaining_rides, total_completed.
    """
    # CONCURRENCY: shared-row read-modify-write with no version check is an
    # accepted limitation (last-write-wins on simultaneous same-plan edits
    # by two family members). Deliberate — see the fuller note in
    # server.py's mark_ride_complete (decision 2026-06-11).
    sk = _coerce_plan_id_to_sk(plan_id)
    try:
        table = _ddb_table()
        item = table.get_item(Key={"PK": f"USER#{_SHARED_USER_ID}", "SK": sk}).get("Item")
    except Exception as e:
        err = _aws_error_payload(e)
        return err if err is not None else {"error": "Plan read failed", "error_message": str(e)}
    if not item:
        return {"error": "Plan not found", "error_message": f"No plan with id '{plan_id}'.",
                "plan_id": plan_id}

    item = _convert_decimals(item)
    ride_seq = list(item.get("ride_sequence") or [])
    completed_rides = list(item.get("completed_rides") or [])
    name_lc = (ride_name or "").lower()
    if any(r.get("ride_id") == ride_id or (r.get("ride_name") or "").lower() == name_lc
           for r in completed_rides):
        return {"plan_id": plan_id, "ride_name": ride_name, "ride_id": ride_id, "completed": 0,
                "remaining_rides": len(ride_seq), "total_completed": len(completed_rides),
                "note": f"'{ride_name}' is already marked complete."}

    new_seq, popped = _pop_ride_from_sequence(ride_seq, ride_id, ride_name)
    if popped is None:
        return {"plan_id": plan_id, "ride_name": ride_name, "ride_id": ride_id, "completed": 0,
                "remaining_rides": len(ride_seq), "total_completed": len(completed_rides),
                "note": f"'{ride_name}' is not in ride_sequence — nothing to complete."}

    entry = dict(popped)
    entry["completed_at"] = datetime.now(timezone.utc).isoformat()
    if actual_wait_min is not None:
        entry["actual_wait_min"] = actual_wait_min
    if notes:
        entry["notes"] = notes
    completed_rides.append(entry)

    try:
        table.update_item(
            Key={"PK": f"USER#{_SHARED_USER_ID}", "SK": sk},
            UpdateExpression="SET ride_sequence = :seq, completed_rides = :done",
            ExpressionAttributeValues=_floats_to_decimals({":seq": new_seq, ":done": completed_rides}),
            ConditionExpression="attribute_exists(PK)",
        )
    except Exception as e:
        err = _aws_error_payload(e)
        return err if err is not None else {"error": "Plan update failed", "error_message": str(e)}

    return {"plan_id": plan_id, "ride_name": ride_name, "ride_id": ride_id, "completed": 1,
            "remaining_rides": len(new_seq), "total_completed": len(completed_rides),
            "note": f"Marked '{ride_name}' complete. {len(new_seq)} ride(s) still planned."}


@mcp.tool()
def remove_ride_from_plan(
    plan_id: str,
    ride_id: str,
    ride_name: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Drop a ride from a plan — moves it to dropped_rides (stops its
    alerts). Use for skipped/abandoned rides (a "too aggressive" signal);
    use mark_ride_complete for rides actually ridden.

    Returns:
        Dict with dropped (0/1), remaining_rides, total_dropped.
    """
    sk = _coerce_plan_id_to_sk(plan_id)
    try:
        table = _ddb_table()
        item = table.get_item(Key={"PK": f"USER#{_SHARED_USER_ID}", "SK": sk}).get("Item")
    except Exception as e:
        err = _aws_error_payload(e)
        return err if err is not None else {"error": "Plan read failed", "error_message": str(e)}
    if not item:
        return {"error": "Plan not found", "error_message": f"No plan with id '{plan_id}'.",
                "plan_id": plan_id}

    item = _convert_decimals(item)
    ride_seq = list(item.get("ride_sequence") or [])
    dropped_rides = list(item.get("dropped_rides") or [])
    new_seq, popped = _pop_ride_from_sequence(ride_seq, ride_id, ride_name)
    if popped is None:
        return {"plan_id": plan_id, "ride_name": ride_name, "ride_id": ride_id, "dropped": 0,
                "remaining_rides": len(ride_seq), "total_dropped": len(dropped_rides),
                "note": f"'{ride_name}' was not in ride_sequence — nothing to drop."}

    entry = dict(popped)
    entry["dropped_at"] = datetime.now(timezone.utc).isoformat()
    if reason:
        entry["reason"] = reason
    dropped_rides.append(entry)

    try:
        table.update_item(
            Key={"PK": f"USER#{_SHARED_USER_ID}", "SK": sk},
            UpdateExpression="SET ride_sequence = :seq, dropped_rides = :gone",
            ExpressionAttributeValues=_floats_to_decimals({":seq": new_seq, ":gone": dropped_rides}),
            ConditionExpression="attribute_exists(PK)",
        )
    except Exception as e:
        err = _aws_error_payload(e)
        return err if err is not None else {"error": "Plan update failed", "error_message": str(e)}

    return {"plan_id": plan_id, "ride_name": ride_name, "ride_id": ride_id, "dropped": 1,
            "remaining_rides": len(new_seq), "total_dropped": len(dropped_rides),
            "note": f"Dropped '{ride_name}'. {len(new_seq)} ride(s) still planned."}


@mcp.tool()
def add_ride_to_plan(
    plan_id: str,
    ride_id: str,
    ride_name: str,
    predicted_wait_min: int | None = None,
    position: int | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Add a spontaneous ride to a plan's ride_sequence (starts
    monitoring it for disruptions). Idempotent on ride_id.

    Returns:
        Dict with added (0/1), total_rides.
    """
    sk = _coerce_plan_id_to_sk(plan_id)
    try:
        table = _ddb_table()
        item = table.get_item(Key={"PK": f"USER#{_SHARED_USER_ID}", "SK": sk}).get("Item")
    except Exception as e:
        err = _aws_error_payload(e)
        return err if err is not None else {"error": "Plan read failed", "error_message": str(e)}
    if not item:
        return {"error": "Plan not found", "error_message": f"No plan with id '{plan_id}'.",
                "plan_id": plan_id}

    item = _convert_decimals(item)
    ride_seq = list(item.get("ride_sequence") or [])
    if any(r.get("ride_id") == ride_id for r in ride_seq if r.get("ride_id")):
        return {"plan_id": plan_id, "ride_name": ride_name, "ride_id": ride_id, "added": 0,
                "total_rides": len(ride_seq),
                "note": f"'{ride_name}' is already in the plan."}

    entry: dict[str, Any] = {"ride_name": ride_name, "ride_id": ride_id}
    if predicted_wait_min is not None:
        entry["predicted_wait_min"] = predicted_wait_min
    if position is not None:
        entry["position"] = position
    if notes:
        entry["notes"] = notes
    ride_seq.append(entry)

    try:
        # Re-adding a ride also UN-DROPS it: clear it from dropped_ride_ids
        # (the /replan drop set) so a ride dropped from the phone and then
        # re-added by Claude isn't silently kept off the watch set. DELETE
        # of an absent member is a harmless no-op.
        vals = _floats_to_decimals({":seq": ride_seq})
        vals[":dropid"] = {ride_id}
        table.update_item(
            Key={"PK": f"USER#{_SHARED_USER_ID}", "SK": sk},
            UpdateExpression="SET ride_sequence = :seq DELETE dropped_ride_ids :dropid",
            ExpressionAttributeValues=vals,
            ConditionExpression="attribute_exists(PK)",
        )
    except Exception as e:
        err = _aws_error_payload(e)
        return err if err is not None else {"error": "Plan update failed", "error_message": str(e)}

    return {"plan_id": plan_id, "ride_name": ride_name, "ride_id": ride_id, "added": 1,
            "total_rides": len(ride_seq),
            "note": f"Added '{ride_name}'. Monitoring starts within ~2 min."}


@mcp.tool()
def get_user_plan_history(
    limit: int = 10,
    include_unrecorded_only: bool = False,
    include_calibration: bool = True,
) -> dict[str, Any]:
    """Recent plans in the shared trip space, with outcomes + a
    pre-computed calibration_summary from the recorded ones.

    Use this: (1) at the start of a planning session to catch an
    unrecorded plan from 1-14 days ago worth asking about; (2) before
    responding to an outcome report when no plan_id is in context (match
    by park + planned_for_date); (3) for calibration — READ the
    pre-computed calibration_summary rather than eyeballing raw plans.

    Args:
        limit: Max plans (1-50, default 10), newest first.
        include_unrecorded_only: Only outcome_recorded=false plans.
        include_calibration: Compute calibration_summary (default True).

    Returns:
        Dict with count, plans (each with plan_id, planned_at,
        planned_for_date, trip_id, active, park_key, ride_sequence,
        completed_rides, dropped_rides, show_selections, notes,
        outcome fields, days_since_plan, stale_for_recall, created_by),
        and calibration_summary (or null). See server.py for the full
        calibration_summary shape.
    """
    limit = max(1, min(limit, 50))
    try:
        table = _ddb_table()
        base_kwargs = {
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
            "ExpressionAttributeValues": {":pk": f"USER#{_SHARED_USER_ID}", ":sk": "PLAN#"},
            "ScanIndexForward": False,
        }
        if include_unrecorded_only:
            # Filter BEFORE the limit so an older unrecorded plan isn't
            # hidden behind newer recorded/dormant rows. (Mirrors server.py.)
            items = []
            kwargs = dict(base_kwargs)
            while len(items) < limit:
                resp = table.query(**kwargs)
                for raw in resp.get("Items", []):
                    it = _convert_decimals(raw)
                    if not it.get("outcome_recorded"):
                        items.append(it)
                        if len(items) >= limit:
                            break
                if "LastEvaluatedKey" not in resp:
                    break
                kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        else:
            resp = table.query(Limit=limit, **base_kwargs)
            items = [_convert_decimals(it) for it in resp.get("Items", [])]
    except Exception as e:
        err = _aws_error_payload(e)
        return err if err is not None else {
            "error": "Plan history read failed", "error_message": str(e),
        }

    now_utc = datetime.now(timezone.utc)
    plans = []
    for it in items:
        if include_unrecorded_only and it.get("outcome_recorded"):
            continue
        plan_ts = it.get("planned_at") or it["SK"][len("PLAN#"):]
        try:
            plan_dt = datetime.fromisoformat(plan_ts)
            # A legacy naive planned_at would make the subtraction raise
            # TypeError, not ValueError — coerce to UTC and catch both so one
            # bad row can't take down the whole history read.
            if plan_dt.tzinfo is None:
                plan_dt = plan_dt.replace(tzinfo=timezone.utc)
            days_since = (now_utc - plan_dt).days
        except (ValueError, TypeError):
            days_since = None
        plans.append({
            "plan_id": it["SK"][len("PLAN#"):],
            "planned_at": plan_ts,
            "planned_for_date": it.get("planned_for_date"),
            "trip_id": it.get("trip_id"),
            "active": bool(it.get("active")),
            "activated_at": it.get("activated_at"),
            "plan_window": it.get("plan_window"),
            "park_key": it.get("park_key"),
            "created_by": it.get("created_by"),
            "ride_sequence": it.get("ride_sequence", []),
            "completed_rides": it.get("completed_rides", []),
            "dropped_rides": it.get("dropped_rides", []),
            "show_selections": it.get("show_selections", []),
            "context": it.get("context", {}),
            "notes": it.get("notes"),
            "outcome_recorded": bool(it.get("outcome_recorded")),
            "outcome_recorded_at": it.get("outcome_recorded_at"),
            "aggression_rating": it.get("aggression_rating"),
            "timing_rating": it.get("timing_rating"),
            "extra_time_minutes": it.get("extra_time_minutes"),
            "per_item_feedback": it.get("per_item_feedback"),
            "free_text": it.get("free_text"),
            "days_since_plan": days_since,
            "stale_for_recall": (
                days_since is not None and days_since > _PLAN_STALENESS_DAYS
            ),
        })

    result: dict[str, Any] = {
        "user_id": _SHARED_USER_ID,
        "count": len(plans),
        "plans": plans,
    }
    if include_calibration:
        result["calibration_summary"] = _compute_calibration_summary(plans)
    return result


# ─── get_planning_context (verbatim port from server.py) ───────────
# The heavyweight planner tool. Body is identical to server.py (read-
# only; resolves rides via the S3 snapshot _find_ride + the egress/
# compute helpers above). Its ~1000-line agentic docstring IS the
# planner contract Claude reads at runtime, so it's kept verbatim.
# KEEP IN SYNC with server.py until the _tool_impls.py consolidation.


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


# ─── Registered-client tracking (review finding #8) ─────────────────
# The Cognito pool is shared with the web dashboard (it authenticates
# against the same pool), so a token minted by the dashboard's Cognito
# client carries a valid, allowlisted sub and would otherwise pass the
# auth gate here. We additionally require the token's client_id to belong
# to a client THIS server's DCR proxy created: each /register records an
# MCPCLIENT# marker row, and every request checks the token's client_id
# against it. The pool is imported read-only from another project, so this
# lives entirely in MM's own table rather than as a Cognito
# resource-server scope (which would mean modifying infra we don't own).

_MCP_CLIENT_PK_PREFIX = "MCPCLIENT#"


def _record_dcr_client(client_id: str, client_name: str | None) -> None:
    """Persist the marker row that lets `client_id` pass the per-request
    registered-client check. Raises on write failure (caller surfaces it)."""
    item: dict[str, Any] = {
        "PK": f"{_MCP_CLIENT_PK_PREFIX}{client_id}",
        "SK": "META",
        "registered_at": datetime.now(timezone.utc).isoformat(),
    }
    if client_name:
        item["client_name"] = client_name
    _ddb_table().put_item(Item=item)


def _ddb_client_is_registered(client_id: str | None) -> bool:
    """True iff `client_id` was created by this server's DCR proxy."""
    if not client_id:
        return False
    resp = _ddb_table().get_item(
        Key={"PK": f"{_MCP_CLIENT_PK_PREFIX}{client_id}", "SK": "META"}
    )
    return "Item" in resp


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

    # Record the new client so it passes the per-request registered-client
    # check (#8). If the marker write fails the Cognito client exists but
    # would be unusable, so surface a retryable error rather than return an
    # unauthorizable client_id. (The /register throttle bounds the orphan
    # Cognito clients a retry loop could create.)
    client_id = result.get("client_id")
    if client_id:
        try:
            _record_dcr_client(
                client_id, result.get("client_name") or payload.get("client_name")
            )
        except Exception:
            _logger.exception("failed to record DCR client_id %s", client_id)
            return JSONResponse(
                {"error": "registration incomplete, please retry"},
                status_code=503,
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
    def __init__(self, app, verifier=None, client_registered=None):
        super().__init__(app)
        # Injectable for tests. Production callers pass nothing and
        # we use the env-derived verifier from jwt_verifier.
        self._verify = verifier or jwt_verifier.verify_token
        # Injectable for tests; production uses the DDB-backed check that
        # only accepts client_ids minted by this server's DCR proxy (#8).
        self._client_registered = client_registered or _ddb_client_is_registered

    @staticmethod
    def _unauthorized(body: dict) -> JSONResponse:
        # RFC 9728 §5.1 / MCP auth spec: a 401 from a protected resource
        # must point the client at its protected-resource metadata so it
        # can discover the auth server and start the OAuth flow.
        challenge = (
            f'Bearer resource_metadata="{_public_base_url()}{_PROTECTED_RESOURCE_PATH}"'
        )
        return JSONResponse(
            body, status_code=401, headers={"WWW-Authenticate": challenge}
        )

    async def dispatch(self, request: Request, call_next):
        method = request.method
        path = request.url.path

        if method == "OPTIONS":
            # Answer preflight directly — don't forward an unauthenticated
            # request into the inner app. 204, no body.
            return Response(status_code=204)

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
            return self._unauthorized(
                {"error": "missing or malformed Authorization header"}
            )

        token = auth[len("Bearer "):].strip()
        try:
            claims = self._verify(token)
            # Stash the verified subject for write-tool attribution
            # (created_by). ContextVar propagates into the tool handlers
            # within this request. Defensive: injected test verifiers may
            # not return a dict.
            _authenticated_sub.set(
                claims.get("sub") if isinstance(claims, dict) else None
            )
        except jwt_verifier.VerifyError as e:
            # The detailed reason is safe to log server-side (it never
            # reaches the client, which only sees a generic 401). WARNING,
            # not ERROR: a rejected token is expected operational noise,
            # not a system fault.
            _logger.warning("token rejected for %s %s: %s", method, path, e)
            return self._unauthorized({"error": "invalid token"})
        except Exception:
            # Defensive: any unexpected verify-side error (e.g., JWKS
            # network failure or missing pool config) becomes a 503 so the
            # client can retry. Log with traceback at ERROR — without this
            # a JWKS outage 503s every request with no CloudWatch evidence
            # of why.
            _logger.exception("unexpected auth verification failure")
            return JSONResponse(
                {"error": "auth verification failed"},
                status_code=503,
            )

        # Defense-in-depth (#8): the token is validly signed and its sub is
        # allowlisted, but the shared pool also issues tokens to the web
        # dashboard's client. Require the token's client_id to belong to a
        # client our DCR proxy created. Fail CLOSED on a lookup error — a
        # 503 the client can retry beats letting an unverified client_id
        # through during a DDB blip.
        client_id = claims.get("client_id") if isinstance(claims, dict) else None
        try:
            registered = self._client_registered(client_id)
        except Exception:
            _logger.exception(
                "registered-client check failed for client_id=%r", client_id
            )
            return JSONResponse(
                {"error": "auth verification failed"}, status_code=503
            )
        if not registered:
            _logger.warning(
                "client_id %r not registered via DCR — rejecting %s %s",
                client_id,
                method,
                path,
            )
            return JSONResponse({"error": "client not registered"}, status_code=403)

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
