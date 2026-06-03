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


# ─── get_planning_context helpers (verbatim from server.py) ─────────
# WDW entrance-plaza coords — one weather fetch is representative of all
# four parks (all within ~6km).
_WDW_LAT = 28.3852
_WDW_LON = -81.5639

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


def _fetch_park_currently_down(table, park_key: str) -> list[dict] | None:
    """Return every DOWN ride in the park with its down-since timing.
    Verbatim from server.py — the park-wide "what's broken now" picture
    for weather-vs-mechanical reasoning. None on DDB failure."""
    if table is None:
        return None
    try:
        items = []
        scan_kwargs = {
            "FilterExpression": "SK = :sk AND park_key = :pk AND #s = :down",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {
                ":sk": "STATE", ":pk": park_key, ":down": "DOWN",
            },
        }
        while True:
            resp = table.scan(**scan_kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except Exception:
        return None

    items = _convert_decimals(items)
    out: list[dict] = []
    for item in items:
        rid = item.get("ride_id")
        entry: dict[str, Any] = {
            "ride_name": item.get("name"),
            "ride_id": rid,
            "last_seen": item.get("last_seen"),
        }
        try:
            ds_resp = table.get_item(Key={"PK": f"RIDE#{rid}", "SK": "DOWN_SINCE"})
            ds = ds_resp.get("Item")
            if ds and ds.get("down_since"):
                entry["down_since"] = ds["down_since"]
                try:
                    down_dt = datetime.fromisoformat(ds["down_since"])
                    elapsed = datetime.now(timezone.utc) - down_dt
                    entry["down_duration_mins"] = round(elapsed.total_seconds() / 60, 1)
                except ValueError:
                    pass
        except Exception:
            pass
        out.append(entry)
    return out


def _fetch_park_hours_today(park_key: str) -> dict[str, Any] | None:
    """Fetch today's open/close window for a park from themeparks.wiki.
    Verbatim from server.py. None on failure (planner degrades)."""
    park_ids = {
        "magic_kingdom":     "75ea578a-adc8-4116-a54d-dccb60765ef9",
        "epcot":             "47f90d2c-e191-4239-a466-5892ef59a88b",
        "hollywood_studios": "288747d1-8b4f-4a64-867e-ea7c9b27bad8",
        "animal_kingdom":    "1c84a229-8862-4648-9c71-378ddd2c7693",
    }
    park_id = park_ids.get(park_key)
    if not park_id:
        return None

    import requests
    today_local = datetime.now(_EASTERN).date().isoformat()
    url = f"https://api.themeparks.wiki/v1/entity/{park_id}/schedule"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None

    open_dt: datetime | None = None
    close_dt: datetime | None = None
    for entry in data.get("schedule", []):
        if entry.get("date") != today_local:
            continue
        if entry.get("type") not in ("OPERATING", "EXTRA_HOURS"):
            continue
        try:
            o = datetime.fromisoformat(entry["openingTime"])
            c = datetime.fromisoformat(entry["closingTime"])
        except (KeyError, ValueError):
            continue
        if open_dt is None or o < open_dt:
            open_dt = o
        if close_dt is None or c > close_dt:
            close_dt = c

    if open_dt is None or close_dt is None:
        return None
    now_et = datetime.now(_EASTERN)
    minutes_to_close = round((close_dt - now_et).total_seconds() / 60)
    return {
        "open": open_dt.isoformat(),
        "close": close_dt.isoformat(),
        "minutes_until_close": minutes_to_close,
    }


def _fetch_weather_forecast() -> dict[str, Any] | None:
    """Current conditions + 6-hour forecast from Open-Meteo (no key).
    Verbatim from server.py. None on failure."""
    import requests
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={_WDW_LAT}&longitude={_WDW_LON}"
        "&current=temperature_2m,precipitation,weather_code"
        "&hourly=temperature_2m,precipitation_probability,weather_code"
        "&temperature_unit=fahrenheit&precipitation_unit=inch"
        "&forecast_hours=6&timezone=America/New_York"
    )
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None

    current = data.get("current") or {}
    hourly = data.get("hourly") or {}
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    precip = hourly.get("precipitation_probability", [])
    codes = hourly.get("weather_code", [])
    next_hours = [
        {
            "time": t,
            "temp_f": temps[i] if i < len(temps) else None,
            "precipitation_chance_pct": precip[i] if i < len(precip) else None,
            "weather_code": codes[i] if i < len(codes) else None,
        }
        for i, t in enumerate(times)
    ]
    return {
        "current": {
            "temp_f": current.get("temperature_2m"),
            "precipitation_inches": current.get("precipitation"),
            "weather_code": current.get("weather_code"),
        },
        "next_6h": next_hours,
        "weather_code_legend": (
            "Open-Meteo WMO codes: 0=clear, 1-3=mainly clear/partly cloudy/"
            "overcast, 45/48=fog, 51-67=drizzle/rain (light to heavy), "
            "71-77=snow, 80-82=rain showers, 95=thunderstorm, "
            "96/99=thunderstorm with hail. >=80 means rides may close "
            "(Disney pauses outdoor rides for lightning); 51-67 means "
            "ride continues but the queue/seats get wet."
        ),
    }


def _compute_load_vs_forecast(rides_out: list[dict]) -> dict[str, Any] | None:
    """Park-level "today is running X% above/below forecast" signal,
    wait-weighted across sampled operating rides. Verbatim from server.py.
    None if no rides survive the exclusions."""
    now_et = datetime.now(_EASTERN)
    current_hour = now_et.hour
    today_iso = now_et.date().isoformat()

    per_ride: list[dict[str, Any]] = []
    total_actual = 0
    total_predicted = 0

    for r in rides_out:
        if r.get("status") != "OPERATING":
            continue
        actual = r.get("wait_mins")
        forecast = r.get("forecast")
        if not actual or not forecast:
            continue
        predicted: int | None = None
        for entry in forecast:
            try:
                t = datetime.fromisoformat(entry["time"])
            except (KeyError, ValueError):
                continue
            t_et = t.astimezone(_EASTERN)
            if t_et.date().isoformat() == today_iso and t_et.hour == current_hour:
                predicted = entry.get("wait_mins")
                break
        if predicted is None or predicted < 10:
            continue
        ratio = round(actual / predicted, 2)
        per_ride.append({
            "ride_name": r["ride_name"],
            "actual_wait_mins": actual,
            "predicted_wait_this_hour": predicted,
            "ratio": ratio,
        })
        total_actual += actual
        total_predicted += predicted

    if not per_ride or total_predicted == 0:
        return None

    park_ratio = round(total_actual / total_predicted, 2)
    n = len(per_ride)

    if n < 3:
        confidence = "low"
        interp = (
            f"Only {n} ride(s) sampled — treat the ratio as "
            f"directional only, don't lean on it heavily."
        )
    elif abs(park_ratio - 1.0) < 0.10:
        confidence = "high"
        interp = (
            f"Today running close to forecast ({int(park_ratio * 100)}% "
            f"of predicted, {n} rides sampled). Use forecast values as-is."
        )
    elif park_ratio > 1.0:
        pct = int((park_ratio - 1.0) * 100)
        confidence = "high" if n >= 5 else "medium"
        interp = (
            f"Today running ~{pct}% ABOVE forecast across {n} rides — "
            f"crowds heavier than predicted. Scale forecast peak values "
            f"by ~{park_ratio:.2f} when reasoning about cost-of-delay."
        )
    else:
        pct = int((1.0 - park_ratio) * 100)
        confidence = "high" if n >= 5 else "medium"
        interp = (
            f"Today running ~{pct}% BELOW forecast across {n} rides — "
            f"crowds lighter than predicted. Scale forecast peak values "
            f"by ~{park_ratio:.2f} when reasoning about cost-of-delay."
        )

    return {
        "park_load_ratio": park_ratio,
        "rides_sampled": n,
        "confidence": confidence,
        "interpretation": interp,
        "per_ride": per_ride,
    }


def _forecast_peak_in_window(
    forecast: list[dict], hours_ahead: int = 3
) -> dict[str, Any] | None:
    """Peak forecasted wait in the next N hours from now (cost-of-delay
    signal). Verbatim from server.py. None if no forward entries."""
    if not forecast:
        return None
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc + timedelta(hours=hours_ahead)

    peak_wait: int | None = None
    peak_time: datetime | None = None
    for entry in forecast:
        try:
            t = datetime.fromisoformat(entry["time"])
        except (KeyError, ValueError):
            continue
        t_utc = t.astimezone(timezone.utc)
        if t_utc < now_utc or t_utc > cutoff:
            continue
        wait = entry.get("wait_mins")
        if not isinstance(wait, (int, float)):
            continue
        if peak_wait is None or wait > peak_wait:
            peak_wait = wait
            peak_time = t

    if peak_wait is None or peak_time is None:
        return None
    minutes_until = round(
        (peak_time.astimezone(timezone.utc) - now_utc).total_seconds() / 60, 1
    )
    return {
        "peak_wait_mins": peak_wait,
        "minutes_until_peak": minutes_until,
        "peak_at": peak_time.isoformat(),
    }


# ─── Plan / trip write-side helpers (M5, ported from server.py) ─────
# Duplicate-first (per the locked decision): these mirror server.py's
# plan-feedback + multi-day-trip helpers verbatim, except the HTTP side
# writes to ONE shared partition and derives the writer's attribution
# from the verified token rather than a client-supplied user_id.

# Shared family trip space — every HTTP write lands here (see design
# doc §7). Reuses the stdio default partition so Megan's Desktop and
# mobile plans are unified and Jim/sister join the same trip.
_SHARED_USER_ID = "megan"

# Plan TTLs (mirror server.py).
_PLAN_PENDING_TTL_SECS = 24 * 60 * 60
_PLAN_RECORDED_TTL_SECS = 365 * 24 * 60 * 60
_PLAN_STALENESS_DAYS = 14
_PLAN_PENDING_BUFFER_DAYS = 2
_TRIP_BUFFER_DAYS = 3


def _floats_to_decimals(obj: Any) -> Any:
    """Recursively convert Python floats to Decimal for DDB writes.

    boto3's resource interface refuses native floats. Reverse of
    _convert_decimals; used on the write side. Verbatim from server.py.
    """
    from decimal import Decimal
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _floats_to_decimals(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_floats_to_decimals(v) for v in obj]
    return obj


def _epoch_now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _coerce_plan_id_to_sk(plan_id: str) -> str:
    """plan_id is the ISO timestamp suffix; SK is `PLAN#<ts>`. Accept
    either form. Verbatim from server.py."""
    if plan_id.startswith("PLAN#"):
        return plan_id
    return f"PLAN#{plan_id}"


def _today_et_date_iso() -> str:
    """Calendar date in Eastern time, ISO. Matches server.py."""
    return datetime.now(_EASTERN).date().isoformat()


def _plan_pending_ttl(planned_for_date: str) -> int:
    """Date-based pending TTL — a future plan survives past its day.
    Verbatim from server.py."""
    try:
        d = datetime.fromisoformat(planned_for_date).date()
    except ValueError:
        return _epoch_now() + _PLAN_PENDING_TTL_SECS
    expiry_et = datetime.combine(
        d + timedelta(days=_PLAN_PENDING_BUFFER_DAYS + 1),
        time(_PARK_DAY_BOUNDARY_HOUR),
        tzinfo=_EASTERN,
    )
    return int(expiry_et.astimezone(timezone.utc).timestamp())


def _build_plan_item(
    *,
    user_id: str,
    park_key: str,
    ride_sequence: list[dict[str, Any]],
    planned_for_date: str,
    plan_ts: str,
    show_selections: list[dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
    notes: str | None = None,
    trip_id: str | None = None,
    plan_window: dict[str, Any] | None = None,
    active: bool = False,
    activated_at: str | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    """Assemble a PLAN# row. Verbatim from server.py — see that file's
    docstring for the multi-day field semantics."""
    return {
        "PK": f"USER#{user_id}",
        "SK": f"PLAN#{plan_ts}",
        "park_key": park_key,
        "planned_at": plan_ts,
        "planned_for_date": planned_for_date,
        "trip_id": trip_id,
        "ride_sequence": ride_sequence,
        "completed_rides": [],
        "dropped_rides": [],
        "show_selections": show_selections or [],
        "context": context or {},
        "notes": notes,
        "plan_window": plan_window,
        "active": active,
        "activated_at": activated_at,
        "created_by": created_by or user_id,
        "outcome_recorded": False,
        "ttl": _plan_pending_ttl(planned_for_date),
    }


def _pop_ride_from_sequence(
    ride_sequence: list[dict[str, Any]],
    ride_id: str,
    ride_name: str,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Find+remove a ride (id match first, name fallback). Verbatim."""
    name_lc = (ride_name or "").lower()
    popped: dict[str, Any] | None = None
    new_seq: list[dict[str, Any]] = []
    for r in ride_sequence:
        if popped is None and (
            r.get("ride_id") == ride_id
            or (r.get("ride_name") or "").lower() == name_lc
        ):
            popped = r
            continue
        new_seq.append(r)
    return new_seq, popped


# ─── Calibration summary (verbatim from server.py) ─────────────────
_AGGRESSION_SCORES = {
    "too_aggressive": -1.0,
    "about_right": 0.0,
    "not_aggressive_enough": 1.0,
}
_BIAS_CONFIDENCE_HIGH = 5
_BIAS_CONFIDENCE_MEDIUM = 3
_BIAS_NEUTRAL_MINUTES = 5


def _bias_confidence(n: int) -> str:
    if n >= _BIAS_CONFIDENCE_HIGH:
        return "high"
    if n >= _BIAS_CONFIDENCE_MEDIUM:
        return "medium"
    return "low"


def _compute_calibration_summary(
    recorded_plans: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Aggregate recorded plans into per-user calibration signals.

    Verbatim from server.py. Pre-computes aggression/timing aggregates +
    per-ride/per-show prediction bias with confidence labels and ready
    interpretation strings. Returns None if no plans have outcomes.
    """
    plans = [p for p in recorded_plans if p.get("outcome_recorded")]
    if not plans:
        return None

    agg_scores = [
        _AGGRESSION_SCORES[p["aggression_rating"]]
        for p in plans
        if p.get("aggression_rating") in _AGGRESSION_SCORES
    ]
    if agg_scores:
        avg_agg = sum(agg_scores) / len(agg_scores)
        if avg_agg < -0.3:
            agg_interp = (
                "User's recent plans tend to run too aggressive — they "
                "didn't fit. Be more conservative today: longer buffers, "
                "fewer rides, or both."
            )
        elif avg_agg > 0.3:
            agg_interp = (
                "User's recent plans tend to finish with time to spare. "
                "Pack more in today — shorter buffers, add a ride, or "
                "fit a show that wouldn't normally make the cut."
            )
        else:
            agg_interp = (
                "User's recent plans have been balanced — predicted "
                "aggression has been about right. Use baseline buffers."
            )
        aggression = {
            "avg_score": round(avg_agg, 2),
            "interpretation": agg_interp,
            "n_samples": len(agg_scores),
        }
    else:
        aggression = None

    timing_buckets = {"ran_over": 0, "on_time": 0, "extra_time": 0}
    extra_times: list[float] = []
    for p in plans:
        t = p.get("timing_rating")
        if t in timing_buckets:
            timing_buckets[t] += 1
        if t == "extra_time" and p.get("extra_time_minutes") is not None:
            extra_times.append(float(p["extra_time_minutes"]))
    n_timing = sum(timing_buckets.values())
    if n_timing > 0:
        avg_extra = (
            round(sum(extra_times) / len(extra_times), 1)
            if extra_times else None
        )
        share_extra = timing_buckets["extra_time"] / n_timing
        share_over = timing_buckets["ran_over"] / n_timing
        if share_extra >= 0.6:
            extra_blurb = (
                f" (averaging ~{avg_extra:.0f} min spare on those days)"
                if avg_extra else ""
            )
            timing_interp = (
                f"User finishes with extra time on {timing_buckets['extra_time']}/"
                f"{n_timing} recent plans{extra_blurb} — pack today's plan "
                f"more aggressively."
            )
        elif share_over >= 0.5:
            timing_interp = (
                f"User runs over time on {timing_buckets['ran_over']}/"
                f"{n_timing} recent plans — be more conservative today, "
                f"cut a ride or extend buffers."
            )
        elif timing_buckets["on_time"] / n_timing >= 0.5:
            timing_interp = (
                f"User finishes on time on {timing_buckets['on_time']}/"
                f"{n_timing} recent plans — current calibration is working."
            )
        else:
            timing_interp = (
                f"Mixed timing pattern across {n_timing} recent plans "
                f"(ran_over={timing_buckets['ran_over']}, "
                f"on_time={timing_buckets['on_time']}, "
                f"extra_time={timing_buckets['extra_time']}) — no clear "
                f"adjustment. Use baselines."
            )
        timing = {
            "distribution": timing_buckets,
            "avg_extra_time_minutes": avg_extra,
            "interpretation": timing_interp,
            "n_samples": n_timing,
        }
    else:
        timing = None

    ride_deltas: dict[str, list[float]] = {}
    show_deltas: dict[str, list[float]] = {}
    for p in plans:
        for r in (p.get("completed_rides") or []):
            predicted = r.get("predicted_wait_min")
            actual = r.get("actual_wait_min")
            ride_name = r.get("ride_name")
            if predicted is None or actual is None or not ride_name:
                continue
            try:
                delta = float(actual) - float(predicted)
            except (TypeError, ValueError):
                continue
            ride_deltas.setdefault(ride_name, []).append(delta)

        feedback = p.get("per_item_feedback") or {}
        if not isinstance(feedback, dict):
            continue
        ride_predictions: dict[str, Any] = {}
        for r in (p.get("ride_sequence") or []) + (p.get("completed_rides") or []):
            name = r.get("ride_name")
            pred = r.get("predicted_wait_min")
            if name and pred is not None:
                ride_predictions[name] = pred
        for ride_name, predicted in ride_predictions.items():
            fb = feedback.get(ride_name)
            if not isinstance(fb, dict):
                continue
            actual = fb.get("actual_wait_min")
            if actual is None:
                continue
            try:
                delta = float(actual) - float(predicted)
            except (TypeError, ValueError):
                continue
            ride_deltas.setdefault(ride_name, []).append(delta)

        show_predictions = {
            s["show_name"]: s.get("predicted_arrival_min")
            for s in (p.get("show_selections") or [])
            if s.get("show_name") and s.get("predicted_arrival_min") is not None
        }
        for show_name, predicted in show_predictions.items():
            fb = feedback.get(show_name)
            if not isinstance(fb, dict):
                continue
            actual = fb.get("arrived_with_min")
            if actual is None:
                continue
            try:
                delta = float(actual) - float(predicted)
            except (TypeError, ValueError):
                continue
            show_deltas.setdefault(show_name, []).append(delta)

    def _bias_entries(deltas, item_label, kind):
        out = []
        for name, ds in deltas.items():
            n = len(ds)
            avg = round(sum(ds) / n, 1)
            confidence = _bias_confidence(n)
            if abs(avg) <= _BIAS_NEUTRAL_MINUTES:
                interp = f"Predictions for {name} have been roughly accurate ({avg:+.0f} min avg)."
            elif kind == "ride_wait":
                if avg > 0:
                    interp = (
                        f"{name} tends to wait LONGER than predicted "
                        f"(+{avg:.0f} min avg, {confidence} confidence "
                        f"on n={n}). Scale this ride's prediction up by "
                        f"~{avg:.0f} min for this user."
                    )
                else:
                    interp = (
                        f"{name} tends to wait SHORTER than predicted "
                        f"({avg:.0f} min avg, {confidence} confidence "
                        f"on n={n}). Scale this ride's prediction down "
                        f"by ~{abs(avg):.0f} min for this user."
                    )
            else:
                if avg > 0:
                    interp = (
                        f"User arrives at {name} LATER than recommended "
                        f"(+{avg:.0f} min avg). Either they cut it close "
                        f"(no problem if they got a fine spot) or your "
                        f"recommendation was more conservative than needed."
                    )
                else:
                    interp = (
                        f"User arrives at {name} EARLIER than recommended "
                        f"({avg:.0f} min avg) — your arrival recommendation "
                        f"may be padding too much for this user."
                    )
            out.append({
                f"{item_label}_name": name,
                "n_samples": n,
                "avg_delta_min": avg,
                "confidence": confidence,
                "interpretation": interp,
            })
        out.sort(key=lambda x: -x["n_samples"])
        return out

    return {
        "n_recorded_plans": len(plans),
        "aggression": aggression,
        "timing": timing,
        "per_ride_prediction_bias": _bias_entries(ride_deltas, "ride", "ride_wait"),
        "per_show_arrival_bias": _bias_entries(show_deltas, "show", "show_arrival"),
        "usage_hint": (
            "Apply aggression + timing interpretations as ONE upfront "
            "calibration note in your response. Apply per-ride / per-show "
            "bias entries selectively: high-confidence biases (n>=5) can be "
            "quoted to the user; medium (n=3-4) applied silently; low (n<3) "
            "ignored — too noisy to be useful."
        ),
    }


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
        show_selections: Optional shows being fitted in.
        context: Optional planner-side snapshot (park_load_ratio, weather,
            planned_at, ...).
        notes: Optional user constraints ("dining at 6pm").
        planned_for_date: ISO date the plan is FOR. Defaults to today (ET).
        trip_id: Optional. Groups this day into a multi-day trip.
        plan_window: Optional {"open","close"} ET window; once set +
            activated, alerts fire only inside it.
        active: Override the default (same-day active / future dormant).

    Returns:
        Dict with plan_id, planned_for_date, trip_id, active,
        expires_at_epoch, created_by, and a next-step hint.
    """
    try:
        park_key = _normalize_park(park)
    except ValueError as e:
        return {"error": str(e)}

    now_utc = datetime.now(timezone.utc)
    plan_ts = (context or {}).get("planned_at") or now_utc.isoformat()
    pfd = planned_for_date or _today_et_date_iso()
    try:
        datetime.fromisoformat(pfd)
    except ValueError:
        return {
            "error": "Invalid planned_for_date",
            "error_message": f"Could not parse '{planned_for_date}'. Use YYYY-MM-DD.",
        }

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
    )

    try:
        _ddb_table().put_item(Item=_floats_to_decimals(item))
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

    return {
        "plan_id": plan_ts,
        "planned_for_date": pfd,
        "trip_id": trip_id,
        "active": active,
        "park_key": park_key,
        "created_by": created_by,
        "expires_at_epoch": item["ttl"],
        "next_step_hint": hint,
    }


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
            datetime.fromisoformat(date_str)
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

    Use on a trip day ("what's my plan today?") to pull the plan up for
    re-evaluation + activation, or mid-day to see what's left. Prefers
    the ACTIVE plan for the date, else the most recently recorded.

    Args:
        date: ISO date (YYYY-MM-DD). Defaults to today (ET).

    Returns:
        Dict with date, found (bool), and when found plan_id + full plan
        body (park_key, trip_id, active, activated_at, plan_window,
        ride_sequence, completed_rides, dropped_rides, show_selections,
        notes, created_by, outcome_recorded).
    """
    target = date or _today_et_date_iso()
    try:
        datetime.fromisoformat(target)
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

    return {
        "date": target,
        "found": True,
        "plan_id": chosen["SK"][len("PLAN#"):],
        "trip_id": chosen.get("trip_id"),
        "park_key": chosen.get("park_key"),
        "active": bool(chosen.get("active")),
        "activated_at": chosen.get("activated_at"),
        "plan_window": chosen.get("plan_window"),
        "ride_sequence": chosen.get("ride_sequence", []),
        "completed_rides": chosen.get("completed_rides", []),
        "dropped_rides": chosen.get("dropped_rides", []),
        "show_selections": chosen.get("show_selections", []),
        "notes": chosen.get("notes"),
        "created_by": chosen.get("created_by"),
        "outcome_recorded": bool(chosen.get("outcome_recorded")),
        "other_plans_for_day": len(matches) - 1,
    }


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
        trip_items: list[dict] = []
        kwargs = {
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
            "ExpressionAttributeValues": {":pk": f"USER#{_SHARED_USER_ID}", ":sk": "TRIP#"},
        }
        while True:
            resp = table.query(**kwargs)
            trip_items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except Exception as e:
        err = _aws_error_payload(e)
        return err if err is not None else {
            "error": "Trip read failed", "error_message": str(e),
        }

    trips = [
        _convert_decimals(it) for it in trip_items
        if (it.get("end_date") or "") >= today
    ]
    if not trips:
        return {"found": False, "note": "No upcoming trip."}
    trips.sort(key=lambda t: t.get("start_date") or "")
    trip = trips[0]
    trip_id = trip["SK"][len("TRIP#"):]

    day_status: dict[str, dict[str, Any]] = {}
    try:
        plan_items: list[dict] = []
        kwargs = {
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
            "ExpressionAttributeValues": {":pk": f"USER#{_SHARED_USER_ID}", ":sk": "PLAN#"},
        }
        while True:
            resp = table.query(**kwargs)
            plan_items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        for it in plan_items:
            it = _convert_decimals(it)
            if it.get("trip_id") != trip_id:
                continue
            day_status[it.get("planned_for_date")] = {
                "plan_id": it["SK"][len("PLAN#"):],
                "active": bool(it.get("active")),
                "ride_count": len(it.get("ride_sequence") or []),
                "outcome_recorded": bool(it.get("outcome_recorded")),
            }
    except Exception:
        pass

    days_out = []
    for d in trip.get("days", []):
        st = day_status.get(d.get("date"), {})
        days_out.append({
            "date": d.get("date"),
            "park_key": d.get("park_key"),
            "plan_id": st.get("plan_id"),
            "active": st.get("active", False),
            "ride_count": st.get("ride_count", 0),
            "outcome_recorded": st.get("outcome_recorded", False),
        })

    return {
        "found": True,
        "trip_id": trip_id,
        "name": trip.get("name"),
        "start_date": trip.get("start_date"),
        "end_date": trip.get("end_date"),
        "days": days_out,
    }


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
    if not plan_id:
        lookup = get_plan_for_day(date=date)
        if lookup.get("error"):
            return lookup
        if not lookup.get("found"):
            return {"error": "No plan to activate",
                    "error_message": lookup.get("note") or f"No plan for {date or 'today'}."}
        plan_id = lookup["plan_id"]

    sk = _coerce_plan_id_to_sk(plan_id)
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
        table.update_item(
            Key={"PK": f"USER#{_SHARED_USER_ID}", "SK": sk},
            UpdateExpression="SET ride_sequence = :seq",
            ExpressionAttributeValues=_floats_to_decimals({":seq": ride_seq}),
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
        resp = _ddb_table().query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={":pk": f"USER#{_SHARED_USER_ID}", ":sk": "PLAN#"},
            ScanIndexForward=False,
            Limit=limit,
        )
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
            days_since = (now_utc - datetime.fromisoformat(plan_ts)).days
        except ValueError:
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


@mcp.tool()
def get_planning_context(
    park: str, ride_names: list[str]
) -> dict[str, Any]:
    """One-shot planner context: live status + forecast + DOWN history
    + location + park hours + weather, all for a list of rides.

    Use this when the user is planning what to ride next. Replaces
    5-10 separate calls to get_live_ride_status / get_ride_forecast /
    etc. Single round trip; consistent timestamp across all rides.

    HOW TO USE THE RESPONSE WHEN ORDERING RIDES:

    **CRITICAL DATA CAVEAT — read this first.** Every live field in
    this response (`status`, `wait_mins`, `current_ll_offer`,
    `forecast`, `weather`, `today_vs_forecast`, `currently_down_in_park`,
    `showtimes`) reflects RIGHT NOW, TODAY. Nothing here predicts
    tomorrow or any future date. If the user is asking about a
    future-day plan ("we're going Saturday", "tomorrow's our EPCOT
    day"), this data still helps for typical-pattern reasoning
    (historical analytics, drop patterns, baselines), but DO NOT
    treat live values as predictions for that date — and DO NOT
    suggest specific actions that depend on availability data we
    don't have for that date (see the LL section below for the most
    common version of this trap). Building a future day or whole trip
    ahead of time IS supported, though — persist it dormant and
    activate it on the day; see section 0d.

    0a. **Before planning, check for unrecorded prior plans + calibrate
       against the user's track record.** Call get_user_plan_history
       (defaults to user_id="megan" for this single-user setup) BEFORE
       laying out today's plan. Two things to do with what you get back:

       - **Pending feedback prompt.** If a plan has outcome_recorded=false
         AND stale_for_recall=false (planned 1-14 days ago), ask once:
         "Before we plan today — how did your <park> day on <planned_for_date>
         go? Was it about right, did you run over, or have extra time?"
         Then call record_plan_outcome with what they say. If they reply
         "don't really remember," still call record_plan_outcome with
         free_text="user couldn't recall" so the row stops generating
         prompts. For plans where stale_for_recall=true, don't ask —
         briefly acknowledge ("I see you also planned at MK on the 1st,
         too long ago to ask about") and move on.

       - **Calibration via the pre-computed summary.** Read
         `calibration_summary` from the response (server-side
         aggregation; you don't need to derive anything from raw plans).
         Apply each bucket according to its `confidence` label and the
         summary's `usage_hint`:

           - **aggression + timing aggregates** — if either has an
             interpretation that's actionable (i.e., not the "balanced"
             / "current calibration is working" wording), surface it
             ONCE up front in your response: "Your last N plans
             finished with extra time, so I'm packing more in today."
             Don't restate per ride. Same convention as the section
             1.5 today_vs_forecast calibration mention.
           - **per_ride_prediction_bias entries** — apply by
             confidence:
               · high (n>=5): quote directly to the user when
                 relevant ("Big Thunder runs ~15 min longer than
                 forecast for you, so I'm scheduling 60 min for it
                 instead of 45")
               · medium (n=3-4): apply silently to your prediction
                 (don't mention; sample size doesn't justify a callout)
               · low (n<3): IGNORE. The signal is too noisy.
           - **per_show_arrival_bias entries** — same confidence
             rules. A "user arrives later than recommended" signal
             with high confidence may justify trimming arrival
             buffers; with low confidence, ignore.

       If `calibration_summary` is null, this is the first recorded
       trip — proceed with baselines and don't fabricate calibration.

    0b. **Check for after-hours parties on the planning date.** Call
       `get_party_calendar(date=<plan_date>)` early — before laying
       out the order — for any Magic Kingdom OR Hollywood Studios
       plan. Three parties currently tracked:
         - **MNSSHP** (Mickey's Not So Scary Halloween Party) — MK,
           Aug-Nov
         - **MVMCP** (Mickey's Very Merry Christmas Party) — MK,
           mid-Nov through late Dec
         - **Jollywood Nights** — Hollywood Studios, mid-Nov through
           mid-Dec, ~6-10 nights total
       Parties affect the plan in two big ways:

       - **Park closes early (typically 6pm) for non-party guests
         on party days.** If the user is planning MK on a party
         date AND hasn't mentioned a party ticket: surface this
         FIRST, before sequencing. "MK closes for non-party guests
         at 6pm on this date — your plan needs to fit in the
         9am-6pm window, or move to another park for the evening."
         Don't bury this in a footnote; it's a hard ceiling on the
         day's length.
       - **Daytime crowds run lighter than typical on party days**
         (locals avoid the early close). When `is_party_day_on_target_date`
         is true, treat predicted ride waits as if today_vs_forecast
         park_load_ratio were ~0.80-0.85. If you ALSO have a real
         load_ratio signal from the live data, combine them — don't
         double-count, just use the lower value.
       - **Evening party hours are the sweet spot for guests WITH
         party tickets.** Capped attendance, shorter waits than
         typical evening waits. Tell the user this if they're
         heading into the party.
       - **December non-party days are some of the highest-crowd
         days of the year.** If the user is planning a December
         MK day, the get_party_calendar response tells you which
         dates are party days nearby — and the surrounding
         non-party days are NOT lighter, they're the opposite
         (holiday tourists piling in). Worth flagging when the
         user has flexibility on which day to go.

       Hedge the language when the tool's `dates_status` is
       `estimated_from_typical_pattern_for_<year>` (the file hasn't
       been verified against Disney's published calendar yet): say
       "this LOOKS like it'll be a party day based on typical
       Disney scheduling — worth double-checking the official
       calendar before booking." Only assert confidently when
       `dates_status` is `verified_from_disney_calendar`.

       Skip the call for EPCOT or Animal Kingdom plans — neither
       park hosts after-hours parties. Always call for MK and HS.

       **When dates_status is "pending_disney_announcement"** (the
       Jollywood Nights case until Disney publishes the schedule):
       the dates array is empty, so the tool naturally returns no
       matches. But if the user is planning HS for Nov-Dec, mention
       that Jollywood Nights could fall during their trip dates and
       you can't verify yet — suggest they check the official
       schedule before locking in evening plans for those months.

    0c. **Fetch the living wisdom + preferences docs from Google Drive
       before planning.** Two Google Docs in the user's Drive carry
       context that's deliberately kept editable outside the codebase:

       - **"Disney Wisdom"** — global operational tactics the user has
         learned and wants applied to every plan: LL strategy
         (SLL vs MLL distinction, the burner-ride trick, scan-in
         timing windows), park-mechanics gotchas (SLL scan-in doesn't
         unlock tier-1 bookings), annual-passholder workarounds.
         Shared across all family members.
       - **"Disney Planner Preferences"** — per-person personal
         preferences. Sectioned by name (e.g., `## Megan`,
         `## Mark`, `## Karen`). Read the section matching the
         intended user — default to Megan (matches `user_id="megan"`);
         if the prompt names someone else ("plan for my husband"),
         switch to their section. If it says "for the family" or
         "for all of us," read every section and synthesize.

       **How to fetch.** Use the user's Google Drive MCP tools
       (typically prefixed `mcp__claude_ai_Google_Drive__` or similar
       depending on their setup). The flow is:
         1. `search_files` with `title contains 'Disney Wisdom'` →
            note the file id from the result.
         2. `read_file_content` with that id → get the doc text.
         3. Repeat for `'Disney Planner Preferences'`.

       **Fail-soft.** If the Drive MCP isn't loaded, the search
       returns no matches, or the read fails, proceed without that
       doc's content. The docstring already carries the load-bearing
       planning rules; the docs are enhancement, not blocking. Note
       in your response that you couldn't access the doc if it would
       have mattered (e.g., user asks about LL strategy and you
       couldn't fetch wisdom).

       **Precedence hierarchy when sources conflict** (apply highest
       authority first, fall through to lower tiers when silent):

       1. **Park reality** — wisdom-doc facts about how Disney's
          systems actually work, plus hard physical constraints (park
          hours, ride heights, ride-down state). These describe the
          world, not anyone's preferences. Non-negotiable. If the
          prompt or preferences appear to demand the impossible (e.g.,
          "use the SLL scan to unlock my MLL tier-1 booking"), surface
          the real constraint politely and offer the closest viable
          alternative.
       2. **Current prompt** — what the user just typed wins for
          today's intent. "Plan for 4 hours" trumps a preferences
          entry saying they typically do 6-hour days. Can selectively
          override docstring assumptions if the user reasons through
          it ("I know this is aggressive, I'm willing to skip lunch").
       3. **Preferences doc (per-person section)** — standing personal
          tendencies. Apply as defaults when the prompt is silent.
          "Skip spinning rides" applies unless today's prompt says
          "I want to ride Mad Tea Party for once."
       4. **Wisdom doc tactics** — operational strategies like the
          burner-ride trick or LL pre-booking sequencing. Apply when
          they fit the situation. The prompt or preferences can opt
          out ("I don't want to bother with the burner trick today").
       5. **Planner framework (this docstring)** — the cost-of-delay
          math, today_vs_forecast scaling, sequencing approach, and
          tool-routing rules. Base method. Everything above tunes it
          but rarely overturns it entirely.

       When you detect a conflict you can't auto-resolve (e.g.,
       preferences say "no thrill rides," prompt says "plan TRON"),
       go with the higher-authority source and briefly mention the
       conflict you noticed so the user can confirm. Don't silently
       drop either signal.

    0d. **Multi-day trips: build the days ahead, activate each on its
       day.** Magic Monitor supports laying out a FUTURE trip in advance
       and only switching on live monitoring once each day arrives.
       Three moments to recognize:

       - **Session start — is a trip already in flight?** Alongside the
         get_user_plan_history check in 0a, call get_upcoming_trip once.
         If it returns a trip (today is on or before its end_date), lead
         with it: "You've got your <name> trip, <start>–<end> — want to
         keep building it, or is today one of its days?" Skip the prompt
         only when the user's message already makes the day's intent
         obvious.
       - **Building ahead (a date that isn't today).** When the user
         wants a day they're not at yet ("plan our June 23–25 trip",
         "rough out next Saturday's MK day"), PERSIST it dormant instead
         of narrating this call's live data as that date's reality: use
         create_trip for several days at once, or record_plan with
         planned_for_date for a single future day. Dormant plans fire NO
         disruption alerts (see section 7). Lean on this call's data only
         for typical-pattern reasoning (analytics, drop patterns,
         baselines), and say so — "waits below are typical for that day,
         not a live read."
       - **On the trip day — re-evaluate, THEN activate.** When the user
         asks "what's my plan today?" on a day that was pre-built, pull
         the stored plan with get_plan_for_day, then re-check it against
         THIS call's live data for that park (what's DOWN now, today's
         real forecast + weather, confirmed hours). Walk the user through
         any changes; once they accept, call activate_plan with the
         re-evaluated ride_sequence + a resolved plan_window. That flip is
         what starts disruption monitoring — a dormant plan stays silent
         until you activate it. Don't activate a future day early (it
         would fire alerts for rides nobody's near yet) — activate on the
         day, after re-evaluation.

    0. **Discover hard constraints first.** If the user gives you a
       multi-ride list without mentioning any of these, ASK ONCE
       before laying out the order — they materially change the plan
       and users often forget to volunteer them:
       - Table-service dining reservations (fixed start time, ~60-90
         min hold; missing the slot loses the reservation entirely)
       - Genie+ / Multi-Pass Lightning Lane reservations (1-hour
         return window per ride; standby wait effectively drops to
         ~10-15 min for that ride during its window)
       - Individual Lightning Lane / ILL (paid per-ride for top-tier
         attractions like TRON or Guardians; same 1-hour shape)
       - Virtual queue boarding groups (TRON, Tiana's, etc. — return
         window is set when the boarding group is called)
       - Shows worth planning around. The top-level `showtimes` field
         lists today's headliner spectaculars / parades / stage shows
         that haven't started yet. If any look marquee (Happily Ever
         After, Festival of Fantasy, Fantasmic, Festival of the Lion
         King, etc.) and the user hasn't said one way or the other,
         ASK ONCE: "I see X at <time> and Y at <time> running tonight
         — want to fit either in?" Treat any selected show as a
         ~20-45 min fixed hold (see section 5.5 for the full mechanics).
       Skip the question if the user already mentioned them. Treat
       each as a hard slot in the schedule and sequence other rides
       around it. When the user has an LL/ILL for a ride, note that
       you're skipping the standby line entirely for that ride.

       **Lightning Lane scheduling mechanics (practical detail
       Claude might not naturally know):**
       - **LL window grace — three layers, use the right one.**
         The 1-hour window has both stated and unofficial buffers:
           - **5 min before window opens** — Disney consistently
             allows early entry. Always safe to plan against.
           - **15 min after window closes** — stated policy. Always
             safe to plan against.
           - **Up to ~2 hours after window closes** — widely tested
             and consistently honored at the tap point, but NOT
             stated Disney policy. Enforcement varies by day, park,
             and CM. Treat this as crisis-mode capacity, not a
             default planning buffer.
         **Default policy:** use 5-min-early + 15-min-late as the
         "always reliable" buffer that doesn't need flagging.
         Mention the assumption ONCE per itinerary: "Plan assumes
         typical Disney grace on LL windows (~5 min early / ~15 min
         late). If grace tightens on a given day, fall back to the
         nominal windows." Don't re-flag for every individual ride.
         **For the 15-45 min late zone:** mention the extension
         where it's load-bearing for the plan ("running ~30 min late
         to this slot — still within informal grace") but don't
         re-flag per ride.
         **For the 45 min - 2 hr late zone:** explicitly warn the
         user that they're leaning on informal-informal grace that
         Disney doesn't publish: "This pushes your 8:30 LL window
         to ~10:25 arrival — widely tested but unofficial. If a CM
         enforces strictly on this day, that slot won't be honored.
         Reserve this for situations where strictness would cost
         you the marquee ride entirely; otherwise sequence earlier."
         Switch to conservative-only mode (no grace at all) only
         when the user explicitly asks.
       - **Pre-arrival booking — tier mechanics + the 3-ride
         allocation.** Multi-Pass / Genie+ lets guests pre-book up
         to 3 LL rides before arriving at the park. The allocation
         rule depends on park:
           - **Magic Kingdom, EPCOT, Hollywood Studios:** rides are
             split into Tier 1 (the marquees) and Tier 2 (everything
             else). Of the 3 pre-bookings: exactly **1 Tier 1 + 2
             Tier 2**. You cannot pre-book 3 Tier 1 rides.
           - **Animal Kingdom:** no tiers — any 3 rides.
         **Call `get_mll_tiers(park)` for the authoritative current
         tier snapshot** rather than relying on the examples below.
         The tool returns the full Tier 1 / Tier 2 lists (or the
         no-tiers AK note), plus an `updated_at` date so you can
         tell the user how fresh the data is. The examples below
         are illustrative; the tool is the source of truth.

         Tier 1 examples (Disney revises these periodically; verify
         in the user's app if uncertain):
           - MK: TRON Lightcycle/Run (also an ILL), Seven Dwarfs
             Mine Train, Jungle Cruise, Peter Pan's Flight, Big
             Thunder, Space Mountain, Tiana's Bayou Adventure
           - EPCOT: Test Track, Frozen Ever After, Remy's Ratatouille
             Adventure, Guardians of the Galaxy: Cosmic Rewind (also
             an ILL)
           - HS: Slinky Dog Dash, Mickey & Minnie's Runaway Railway,
             Star Tours, Toy Story Mania, Rise of the Resistance
             (also an ILL)
         ILL (paid per-ride) rides like TRON, Cosmic Rewind, and
         Rise of the Resistance are sometimes listed under Tier 1
         in the booking UI but they're a SEPARATE product —
         purchasing ILL does NOT count against the 3-ride MLL pre-
         book allocation. A guest can hold 3 MLL pre-bookings AND
         however many ILLs they buy.
         When recommending pre-arrival picks for MK/EPCOT/HS:
         honor the 1+2 split. Pick the user's highest-value Tier 1
         (highest typical wait + most-wanted) and two strong Tier 2
         picks. Don't suggest 2 Tier 1 + 1 Tier 2 — they cannot
         book that combination.
       - **Day-of booking unlocks — scan into MLL to refill the
         queue.** Once the guest scans into an MLL pre-booking at
         the tap point, the LL system unlocks the next booking:
         they can book ONE additional MLL ride **from any tier**
         (the 1-Tier-1 restriction lifts entirely after the first
         MLL scan). This loops — each subsequent MLL scan unlocks
         another. Two constraints:
           - **Scanning into an SLL (paid ILL) ride does NOT
             unlock more MLL bookings or lift tier restrictions.**
             Riding Cosmic Rewind, TRON Lightcycle, or Rise of the
             Resistance via the ILL purchase doesn't help the MLL
             cadence at all.
           - **Sequence MLL pre-bookings BEFORE any SLL/ILL when
             possible.** If a guest has both MLL pre-bookings and
             ILL purchases for the same morning, route them to the
             MLL FIRST so the unlock fires immediately and they can
             start building toward booking #4, #5, etc. Burning the
             ILL first wastes the morning window where you could be
             accumulating MLL slots.
         When planning a guest's day with active MLL pre-bookings:
         the recommended next LL (see the recommendation engine
         below) typically targets right after the first scan, when
         the tier restriction has lifted and the strongest available
         Tier 1 wait becomes bookable.
       - The LL line itself is NOT zero-wait. Plan ~10-15 min for
         the LL queue (can hit 20-25 min during peak hours). Total
         time from "tap in" to "off the ride" is usually ~25-30 min.
       - Factor in walking time to reach the LL window. If the user
         is across the park (>500m by the lat/lon data) when the
         window opens, build in the walk — don't have them book-end
         the window with travel time on both sides.
       - If two LL windows overlap, the user has to pick one. Flag
         the conflict and ask which is the priority.
       - **When you have to push past the end of one LL window,
         prefer pushing the Individual Lightning Lane (ILL, paid
         per-ride) over the Multi-Pass / Genie+ (bundled).** Empirical
         pattern: ILL CMs tend to be more lenient about late
         arrivals than Multi-Pass CMs — paid-per-ride creates a
         "we charged you $20, we're not turning you away" dynamic,
         and ILL queues are typically shorter so a late guest is
         less disruptive. Multi-Pass enforcement is more variable
         and often stricter. So when sequencing forces a late
         arrival on one of two competing LLs, route on-time to the
         Multi-Pass and apply the late grace to the ILL. Same logic
         when only one of two LLs is reachable in time without
         skipping a planned ride: be on-time to the Multi-Pass,
         late to the ILL. (User-stated experience as of 2026-05-10
         — if Disney standardizes enforcement across products this
         rule may need revisiting.)
       - **Recommend the next LL to book when the user has Multi-Pass
         active.** Multi-Pass users can hold one LL at a time and
         book the next once the current one is used (or tapped in).
         When the user mentions they have Multi-Pass / Genie+ active
         — or is asking for a plan after a long park stretch where
         it's reasonable to assume — proactively suggest which ride
         to book next. Don't wait for them to ask.

         Selection criteria, in order:

         1. **Filter:** rides on the user's wishlist that are
            (a) currently OPERATING, (b) have a `current_ll_offer`
            with a `return_start` falling within the user's planned
            remaining park time, (c) haven't already been done today
            (ask if uncertain — Claude has no built-in "done" signal
            beyond what the user reports).

         2. **Score each candidate by time saved:**
              ll_value = current wait_mins - 15 min LL queue
            Higher is better — an LL on a 60-min standby saves ~45
            min; an LL on a 25-min standby only saves ~10. If the
            current wait is below ~25 min, the LL is rarely worth
            burning on that ride.

         3. **Tiebreakers (apply when top picks are within ~10 min
            of each other on time saved):**
            - **Plan compatibility:** does the LL return window fall
              during a stretch the user would already be in that
              land? Bonus if yes (e.g., Big Thunder LL returning at
              3 PM while the user planned to be in Frontierland
              anyway).
            - **Proximity:** walking distance from the user's current
              location or their next planned ride. Use lat/lon and
              haversine. Closer = bonus.
            - **Cost-of-delay survivor:** if the ride's
              `forecast_peak_next_3h_mins` is much higher than its
              current wait, the LL locks in today's lower value
              against the predicted peak.
            - **Down-risk avoidance:** rides with high
              `downtime_pct` in their historical analytics carry
              more risk if you commit an LL slot to them. Slight
              negative weight.

         4. **Recommend top 1-2 with explicit reasoning.** Don't
            just name the ride; show the math: "I'd book Big Thunder
            next — it's showing 65 min standby and its LL is
            returning at 3:15 PM. That's ~50 min saved vs standby,
            and you'd be in Frontierland for Pirates around that
            time anyway. Second choice would be Space Mountain (40
            min saved, less convenient return window)."

         **How to incorporate into the plan you propose:**

         - Lay out the plan ASSUMING the recommended LL gets booked.
           Mark that ride's slot in the sequence with an explicit
           "assuming Big Thunder LL at 3:15 PM" note rather than
           hiding the assumption. Predicted wait for that ride
           drops to ~15 min (LL queue) for plan-time purposes.
         - When you call record_plan to persist the plan, encode
           the assumption in the ride_sequence entry — e.g.,
           {"ride_name": "Big Thunder", "predicted_wait_min": 15,
           "position": 3, "notes": "assumes Multi-Pass LL booked
           at 3:15 PM"} — so the feedback loop can later compare
           predicted-vs-actual if the booking diverged.
         - Tell the user upfront that the plan is contingent on
           the booking: "If you book something else when the window
           opens, tell me what you got and I'll re-sequence the
           rest of the day. I'm assuming Big Thunder at 3:15 here
           — if Disney offers you a different time slot or you
           pick another ride, that's the trigger for a quick
           replan."

         **When the user reports back what they actually booked:**

         - If they booked the recommendation: nothing to do. Plan
           continues as written; the predicted_wait_min on that
           ride stays at LL-queue-time.
         - If they booked something different: prefer to PATCH the
           existing plan in place rather than creating a fresh one.
           See "Mid-trip plan adjustments" below for the
           remove_ride_from_plan / add_ride_to_plan flow.

       - **Mid-trip plan adjustments — use the right tool per
         situation.** Four tools mutate the active PLAN# row in
         place (no new row, no outcome recorded; the plan stays
         in-flight). Choose the one that matches the user's
         actual signal:

           - `mark_ride_complete(plan_id, ride_id, ride_name,
             actual_wait_min?, notes?)` — user RODE the ride. Use
             this whenever the user reports finishing a ride
             ("we just got off Pirates", "Big Thunder was 35 min,
             not bad"). Moves the ride from ride_sequence into
             completed_rides with a completed_at timestamp. Pass
             actual_wait_min when the user mentions a wait — that's
             the strongest signal for the calibration loop's
             per-ride prediction bias. **Prefer this to
             remove_ride_from_plan whenever the user actually
             rode the thing.**
           - `remove_ride_from_plan(plan_id, ride_id, ride_name,
             reason?)` — user SKIPPED the ride. Use this only when
             the user explicitly abandons a ride from their day
             ("we're not doing Space Mountain, wait's too long",
             "let's drop Mansion, we ran out of time"). Moves the
             ride into dropped_rides with a dropped_at timestamp
             + optional reason. NEGATIVE signal for the calibration
             loop — contributes to "plan was too aggressive"
             pattern. Calling this for a ride the user actually
             rode undercounts completions in their history.
           - `add_ride_to_plan(plan_id, ride_id, ride_name,
             predicted_wait_min?, position?, notes?)` — user added a
             SPONTANEOUS ride that wasn't in the original ("we're
             grabbing Pirates while we're nearby"). Adds to
             ride_sequence; poller starts watching within ~2 min.
           - `add_ride_to_plan` + `mark_ride_complete` —
             retroactively log a ride the user did that wasn't
             planned. Add it first (so it's recorded with predicted
             wait if you remember what it was), then mark complete
             with the actual wait.

         **Common in-trip narrative flow** Claude should reach for:
           - "we just finished Pirates, 12 min wait" → mark_ride_complete
           - "we're skipping Space Mountain" → remove_ride_from_plan
           - "we're grabbing Carousel too" → add_ride_to_plan
           - "I booked TRON instead of the Big Thunder LL you
             recommended" → remove_ride_from_plan(Big Thunder,
             reason="swapped LL") + add_ride_to_plan(TRON, notes=
             "actual ILL booked")
           - "Big Thunder went down, we'll come back later" → leave
             it alone — the existing plan-disruption alert covers
             this; the ride is still in ride_sequence and the user
             may still ride it once it's back.

         **When to use add/remove vs. a full replan:**
           - **Use add/remove** for small in-the-moment changes:
             user swaps one LL booking, decides to skip a single
             ride, adds a spontaneous one. Preserves the plan
             history including predictions for unchanged rides
             (matters for the feedback loop's calibration data).
           - **Use full replan** (record_plan_outcome on the prior
             plan + fresh get_planning_context + new record_plan)
             when the day fundamentally shifts: weather pivots
             everything indoor, user changes parks mid-day, half
             the wishlist gets dropped. Anything that changes 3+
             items in one go is a fresh plan.

         **Sequence for the most common case — user swaps an LL
         booking:** `remove_ride_from_plan` (the ride whose LL you
         recommended but they didn't get) → `add_ride_to_plan`
         (the ride they actually got, with notes="actual LL booked
         instead of recommended X"). Both calls return cleanly; the
         poller catches the change on its next 2-min cycle.

       - **Suggest modifying LLs ONLY when the data supports it.**
         Disney Genie+ / Multi-Pass / ILL reservations can be
         modified through the app, but the planner only has visibility
         into one signal: each ride's `current_ll_offer`, which is
         the GLOBAL next-available LL return time being offered RIGHT
         NOW for TODAY (not user-specific — same number Disney shows
         anyone booking fresh). Disney's API does NOT expose a full
         inventory map, future-day inventory, or per-slot availability.

         **Hard refusal rule:** never suggest moving an LL to a
         specific time without a concrete signal supporting it. The
         three valid scenarios:

         **(A) Today, target ≥ current_ll_offer.return_start** —
         the only "we have data" case. Phrase with appropriate
         hedging: "The 7-8 PM range looks LIKELY available — current
         next-available is 6:30 PM, so anything ≥ 6:30 should be
         bookable. Specific times within that range can still be
         sold out though, so check the app to confirm 7-8 PM
         specifically before committing." Do NOT phrase as "is
         open" or "is available" — that overstates what we know.

         **(B) Today, target < current_ll_offer.return_start** —
         the target is gone. Tell the user honestly: "7-8 PM appears
         to be sold out — earliest available now is 8:30 PM. Your
         5-6 PM is still the best slot you have access to."

         **(C) `current_ll_offer` is missing OR the user is planning
         a future date** — REFUSE to suggest specific times. We have
         no data. Do this instead:
           - Acknowledge the gap honestly: "I can't see tomorrow's
             LL inventory — Disney doesn't publish that, and the live
             data here is today-only."
           - Reference `ll_drop_pattern.top_drop_hours_et` as TYPICAL
             refresh windows (NOT availability claims):
             "Big Thunder's LL slot most commonly refreshes around
             11 AM, 2 PM, and 5 PM ET — when you're booking, those
             are the historically best times to check the app for an
             earlier slot." Pure pattern-of-life data, no claim
             about specific dates.
           - For tomorrow specifically, also note the booking timing:
             Multi-Pass opens at 7am day-of for most guests; Deluxe/
             DVC guests can book Multi-Pass 7 days ahead and ILL up
             to 7 days ahead. The user should react to actual
             availability when the booking window opens, not to any
             plan we propose.

         **When to proactively trigger this reasoning:** when the
         current LL window creates an awkward fit for TODAY
         (requires backtracking, conflicts with dining, forces a
         worse ride order, pushes against park close). Don't suggest
         a swap if the current slot is fine. Don't suggest a swap
         AT ALL for a future-date plan — wait until they're at the
         park and ask in real time.

         **`ll_drop_pattern` field reference (historical only):**
         `drops_per_active_day` tells you how frequently this ride's
         LL refreshes (8+ per day is "very active," <1 is "rare").
         `typical_shift_minutes` tells you how big a refresh usually
         is — if it's 30 min, the swap window opens half an hour
         when refreshes happen; if it's 4 hours, the slot can jump
         dramatically. These describe the ride's typical refresh
         behavior, not today's specific inventory state.

    1. **Cost-of-delay rule** (most important). The fields you want:
       `forecast_peak_next_3h_mins` (worst forecasted wait in the next
       3 hours) and `forecast_minutes_until_peak` (how soon that peak
       hits). For each ride, marginal cost of deferring it ≈
       max(0, forecast_at_deferred_time - current_wait_mins). Order
       by DESCENDING cost-of-delay, NOT by ascending current wait.
       Show your math: "If I do TRON first (~80 min round trip),
       Pirates' wait at +80 min would be ~40 (its peak hits in 60
       min and holds), so deferring Pirates costs +30 min. If I do
       Pirates first (~25 min), TRON's wait at +25 min is still ~85
       (flat over the next 3h), so deferring TRON costs ~0. Pirates
       first." The full `forecast` array is also returned so you can
       look up exact future-wait values at specific times when needed.

    1.5. **Today-vs-forecast correction.** The top-level
       `today_vs_forecast` field compares each operating ride's
       CURRENT wait to what today's forecast predicted for the
       current ET hour, aggregated park-wide. If `park_load_ratio`
       is materially different from 1.0 (>10% off), today's actual
       crowd is heavier or lighter than the forecast model expected.
       When reasoning about cost-of-delay, scale forecast peak
       values by this ratio. Example: ratio=1.23, forecast says
       Pirates peaks at 40 in 60 min → expected actual peak is
       ~49 min. Note the calibration ONCE in your response ("Today
       is running 23% above forecast — I've adjusted the peak
       estimates accordingly") rather than re-mentioning it per
       ride. Confidence levels: `low` (<3 rides sampled, treat as
       directional only); `medium`/`high` (5+ rides, reliable
       enough to scale by). If confidence is low or the field is
       absent, use forecast values as-is.

    2. **DOWN-state rides.** Two different causes, two different
       return-time models — diagnose before predicting.

       a) **Weather-caused downtime (outdoor rides during a storm).**
          Diagnostic checklist, in order of confidence:

          - **Strongest signal: multiple simultaneous outdoor downs.**
            Check `currently_down_in_park`. If 3+ outdoor rides went
            DOWN within ~20 min of each other AND it's currently
            storming (weather.current.weather_code 95/96/99 or high
            precip), weather causation is near-certain. Single-ride
            DOWN during a storm could be coincidence; concurrent
            outdoor downs is essentially proof.
          - **Medium signal: one outdoor ride DOWN, recent down_since,
            storm now.** Likely weather, but acknowledge uncertainty
            ("could be weather or coincident mechanical").
          - **Weak signal: outdoor ride DOWN for hours, storm just
            arrived.** Cause is probably mechanical-then-weather-
            prolongs; reopening still waits for storm clear regardless.

          When weather causation applies, the historical cluster
          median does NOT — that data mostly captures mechanical
          breakdowns. Return-time prediction follows Disney's lightning
          rule: outdoor rides resume ~30 min after the last lightning
          strike. Use weather.next_6h to find when the storm clears
          (weather_code drops back to <80) and add ~30 min.

          Example: "Big Thunder, TRON, and Splash all went DOWN
          within 15 minutes of each other and it's currently
          thunderstorming — this is a park-wide weather closure, not
          mechanical. Forecast shows storms clearing by 5:45 PM, so
          expect all three back around 6:15 PM. Indoor rides are
          unaffected — do Mansion or Pirates during the closure."

          Indoor rides DOWN during the same storm are still mechanical
          (the rain isn't why they're broken) — apply cluster math.

       b) **Mechanical downtime (everything else).** Use
          `down_duration_mins`, `typical_down_cluster_mins`, and
          `cluster_progress_pct` as before. Near 0% = early, plan
          ~typical more min; near 100% = could come back any minute.

       c) **Pre-closing rule — DOWN rides late in the park day.** If
          a ride transitions to DOWN within the last ~30-45 minutes
          of park close (`park_hours.minutes_until_close <= 45` at
          the moment it went down, derivable from `down_since` +
          park_hours), **assume it won't reopen for the day** and
          treat it as gone in the plan. Disney's late-day operational
          posture favors keeping a ride down over a hurried restart
          for a few more minutes of cycles. Communicate this to the
          user as "given how late this went down, plan as if it's
          out for the night — if it does come back, that's a bonus."

          Exception: rides on a known short-recovery pattern (Pirates,
          Mansion, Carousel-style mechanical resets typically back
          within 15 min — check `typical_down_cluster_mins` for the
          specific ride; if the historical median is <20 min, the
          pre-closing rule is weaker because the ride genuinely can
          come back fast).

          Pre-closing rule does NOT apply to weather-caused downtime
          — that's already handled by 2a (the storm-clearing-time
          model). Apply the pre-closing rule only to mechanical-
          downtime cases (2b).

       d) **Time-of-day calibration for return predictions.** The
          historical `typical_down_cluster_mins` value is a single
          all-time average across hours. Real downtime patterns
          vary by hour-of-day: a ride going DOWN at 11 AM has a
          very different expected recovery profile than one going
          DOWN at 9 PM. When the predicted return time is decision-
          load-bearing for the plan (e.g., user is deciding whether
          to wait or sequence around it), call
          `get_ride_dow_pattern(ride_name)` and look at the cell
          for the current (day-of-week, hour). The per-hour
          downtime pattern gives a tighter prediction than the
          all-time median.

          Apply the hour-adjusted estimate visibly when the
          difference vs the all-time median is meaningful (>15 min
          or >30% relative). Quote both numbers to the user:
          "Historically this ride averages 45 min outages, but for
          the 8 PM hour specifically it's averaged 75 min on
          previous Saturdays — given that, I'd sequence around it."

          When the per-hour sample is thin (`get_ride_dow_pattern`
          reports `n` below ~5 for the cell), fall back to the
          all-time median and don't make a confidence claim. Same
          confidence-by-sample-size convention as the calibration
          loop in `get_user_plan_history`.

       NEVER infer return time from `current_ll_offer.return_start` —
       Lightning Lane offers are unrelated to operational status.

       Note for users: the planner doesn't currently store historical
       weather, so we can't perfectly correlate "when the ride went
       down" with "when the storm started." Use current weather +
       down_since timing as the heuristic and acknowledge the
       uncertainty when relevant ("if the storm started before this
       ride went down, weather is the likely cause").

    3. **Proximity grouping.** Each ride has `location.lat/lon`. Use
       haversine distance to identify clusters (rides within ~250m
       are in adjacent lands). Other things equal, prefer back-to-back
       rides in the same cluster to reduce walking.

    4. **Feasibility check — warn the user if the wishlist won't fit.**
       BEFORE presenting the order, sanity-check that the plan fits
       in the time available. Estimate per ride:
         time_per_ride ≈ current_wait_mins + 10 min ride duration
                         + walking time to next ride
       Walking: ~5 min for adjacent lands (<300m), ~10 min for cross-
       park hops (>500m, e.g. Adventureland to Tomorrowland). Use the
       lat/lon distances to be specific.
       Total budget = `park_hours.minutes_until_close` minus dining
       hold(s) minus reservation/LL window buffers minus a 30-min
       safety margin (bathroom, longer-than-forecast queues, etc.).
       If total_estimated > total_budget, **flag it explicitly and
       generate 2-3 alternate full plans** rather than asking the
       user "which to drop." Each alternate should be a complete
       ordered itinerary so the user can compare lived experiences,
       not just lists of dropped rides. Format each as a short
       labeled bundle:

         **Plan A — "Skip TRON, do everything else"**
         1. Pirates (10 min, 6:00 PM)
         2. Big Thunder (60 min, 6:30 PM)
         3. Haunted Mansion (recovers ~6:30, do at 7:00)
         4. Space Mountain (last, recovers ~8:30)
         Tradeoff: skips the marquee coaster but fits 4 rides
         comfortably.

         **Plan B — "TRON-focused: 3 rides including TRON"**
         1. TRON (65 min, NOW)
         2. Big Thunder (next door after TRON, 60 min ~7:30)
         3. Space Mountain (last, recovers ~8:30)
         Tradeoff: skips both Mansion and Pirates to lock in TRON
         + late-night Space Mountain.

         **Plan C — "Adventureland cluster: 3 quick rides nearby"**
         1. Pirates (10 min)
         2. Haunted Mansion (when it recovers ~6:30)
         3. Big Thunder (adjacent to Mansion)
         Tradeoff: tightest walking, drops the Tomorrowland rides
         but you finish with energy left for fireworks/dining.

       Pick alternatives that highlight DIFFERENT tradeoffs — drop
       one big ride vs drop several small ones; cluster by proximity
       vs spread across the park; drop based on uncertainty (DOWN
       rides) vs drop based on cost. 2-3 plans is the right number,
       not 5. Then ask "Which plan fits your priorities?" so the
       user picks intent rather than reverse-engineering a list of
       skipped rides.

    5. **Meal/break windows.** If the user mentions wanting to eat or
       take a break in a specific time window ("quick-service dinner
       between 5-7pm" / "let's stop for snacks around 3"), treat it
       as a fixed ~30-45 min hold in the schedule and sequence rides
       to put them in the right area when the window starts. Use your
       general knowledge of WDW dining locations (Pecos Bill's in
       Frontierland, Cosmic Ray's in Tomorrowland near Space Mountain,
       Pinocchio Village Haus in Fantasyland, Columbia Harbour House
       in Liberty Square near Haunted Mansion, etc.) to suggest a
       specific spot near whichever ride you'd be at when the window
       opens. Acknowledge this is a general-knowledge suggestion, not
       a live-data lookup — wait times and operating status of
       restaurants aren't currently in MM's data.

    5.5. **Showtimes the user wants to catch.** The top-level `showtimes`
       field is a headliner-only subset of today's entertainment lineup
       (spectacular / parade / stage), filtered to performances that
       haven't started yet. Each entry has `category`, `name`, and
       `remaining_today` — a list of {start, end} ISO timestamps for
       this show's upcoming performances.

       Treat selected shows as fixed time-blocks. Walk backward from
       the start time when sequencing: the previous ride must finish
       (wait + ride duration + walk to viewing spot + early-arrival
       buffer) before showtime. If the math doesn't work, swap a
       different ride into the slot or warn the user the show is at
       risk.

       **Crowd-scale the arrival recommendations.** The arrival times
       below are baselines for an average-crowd day. The same
       `today_vs_forecast.park_load_ratio` signal that scales ride
       waits in section 1.5 applies to show arrival times — popular
       viewing spots fill up proportionally to the park's actual
       crowd level, not its forecast. Apply roughly:
         - ratio > 1.20 (heavy) → multiply baseline arrival by ~1.4-1.5x
           (60-90 min Fantasmic baseline becomes 90-130 min)
         - 1.05 ≤ ratio ≤ 1.20 (slightly heavy) → multiply by ~1.2x
         - 0.85 ≤ ratio ≤ 1.05 (typical) → use baseline as-is
         - ratio < 0.85 (light) → multiply by ~0.7-0.8x
       If `today_vs_forecast` is None or `confidence` is "low", use
       baselines as-is and acknowledge the uncertainty ("hard to gauge
       crowds today — these are typical-day arrival times, add 30 min
       on top if the park feels packed when you arrive"). Mention the
       scaling ONCE per response (same convention as section 1.5
       calibration) rather than per-show.

       Weather is a secondary modifier: if `weather.next_6h` shows
       rain clearing right before showtime, expect the venue to fill
       faster than usual once the rain stops (people who were waiting
       it out indoors all converge at once). For Fantasmic
       specifically, heavy rain can cancel the show — don't promise
       a 90-min hold if the forecast looks thunderstorm-y.

       **Spectaculars (fireworks / projection finales)** — ~15-30 min
       performance. These typically gate park-departure timing: most
       guests leave shortly after, so the post-finale ~30 min is the
       worst time to queue (mass exit crowds). Don't put a long-wait
       ride immediately after a spectacular. Per-park notes:

       - **Happily Ever After (Magic Kingdom):** ~18 min, projection
         mapping on the castle. The iconic spot is the Hub directly
         in front of the castle (best castle view, gets crowded
         60-90 min before). Main Street curbs are the popular
         alternative — arrive 30-45 min early for a clear sightline.
         The Tomorrowland bridge is an off-angle alternative with
         less crowd if the user is okay with a side view. After the
         finale, Main Street becomes a slow-moving river of exits
         for ~30-45 min. If the user has a Tomorrowland ride after,
         that's fine — the queues there clear quickly. Avoid
         scheduling a Frontierland or Adventureland ride post-finale
         unless they're willing to fight the exit crowd to get there.
       - **Disney Starlight: Dream the Night Away (Magic Kingdom):**
         ~20 min, a newer nighttime parade-spectacular hybrid that
         winds the parade route. Same viewing-spot logic as Festival
         of Fantasy below applies, with the added consideration that
         this is a nighttime show — Frontierland start position has
         the LEAST castle ambient light, Main Street near the castle
         has the most. Often runs a second performance ~2 hours
         after the first on busy nights.
       - **Luminous The Symphony of Us (EPCOT):** ~17 min, fireworks
         and barges on World Showcase Lagoon. Best viewing is
         anywhere along the lagoon perimeter — the show is designed
         to look good from all sides. Showcase Plaza (front of WS)
         is the densest viewing area. Japan, Italy, and UK pavilions
         are popular alternative spots. International Gateway side
         (between France and UK) usually has more breathing room and
         is the closer exit if leaving via Boardwalk hotels. Arrive
         30-45 min early for a clear lagoon sightline at the popular
         spots; 15 min for the lesser-trafficked sides.
       - **Fantasmic! (Hollywood Studios):** ~30 min, in the 6,900-
         seat Hollywood Hills Amphitheater (uniquely sit-down — the
         only WDW spectacular with assigned seating). Arrive 60-90
         min early for a good seat, 30 min for standing room.
         Dining packages with a few HS table-service restaurants
         include reserved seating. After Fantasmic, the amphitheater
         empties onto a single narrow path — expect 20-30 min just
         to clear the venue, longer to reach the front of the park.
       - **Disney Movie Magic + Wonderful World of Animation
         (Hollywood Studios):** projected on the Chinese Theatre at
         the end of Hollywood Blvd. Stand anywhere along Hollywood
         Blvd facing the theater. Less of a planning anchor than
         Fantasmic — these run back-to-back at the front of the park
         and are easy to walk up to 10-15 min before.
       - **Animal Kingdom:** no traditional nighttime spectacular
         currently. Tree of Life Awakenings is a 5-min projection
         that loops every ~10 min after dark — ambient, no planning
         needed.

       **Parades** — ~12-15 min for the parade itself, but the parade
       route is closed off ~30-45 min total (crowd assembly + parade
       + dispersal). Currently only Magic Kingdom has daytime parades:

       - **Disney Festival of Fantasy Parade (Magic Kingdom):** the
         marquee parade. ~12 min performance. Route: starts at
         Frontierland (the steps near Tiana's Bayou Adventure / Big
         Thunder), winds through Liberty Square, past Cinderella
         Castle, down Main Street USA, ends at Town Square. Common
         viewing spots and tradeoffs:
           - **Frontierland (start)** — lightest crowd, easy to leave
             toward Big Thunder / Splash / Pirates afterward. Good
             pick if the user's next ride is in Frontierland or
             Adventureland — they're already there. Arrive 15-20 min
             early.
           - **Liberty Square bridge / in front of Haunted Mansion** —
             same route side as Frontierland. Easy walk to Mansion or
             back to Frontierland after. ~15-20 min early.
           - **Hub in front of castle** — best castle backdrop and
             photo opportunity, most crowded. 30-45 min early.
           - **Main Street curb** — flat, easy viewing, lots of curb
             space. Arrive 20-30 min early for prime spots, 10 min
             for back-row standing.
           - **Town Square (end)** — parade ends here, easy exit to
             park entrance if leaving after. 10-15 min early.
         The route blocks crossing through Liberty Square / past the
         castle / down Main Street for the full ~30-45 min window.
         If the user wants a ride on the OPPOSITE side of the route
         from the parade (e.g., they're at Town Square and want to
         get to Frontierland), sequence that ride BEFORE the parade
         or wait until ~15 min after it ends. Use the ride lat/lon
         to find a viewing spot near the user's next ride: if their
         next ride is Big Thunder, suggest the Frontierland start;
         if it's Haunted Mansion, suggest the Liberty Square bridge.
       - **Disney Adventure Friends Cavalcades:** mini-parades that
         run multiple times throughout the day at MK (often AK too).
         ~5 min each, much shorter route than FoF. Less of a
         planning anchor — easy to catch incidentally if the user is
         on Main Street when one passes. Don't reorganize the day
         around these unless the user specifically asks.
       - **EPCOT, Hollywood Studios, Animal Kingdom:** no traditional
         daytime parade currently running. Don't suggest parade
         viewing for those parks even if `remaining_today` is empty
         (the absence of parade entries is the signal — don't
         hallucinate a parade).

       **Stage shows** — typically 25-35 min for the performance plus
       15-20 min queueing in (these are indoor-theater shows with set
       seatings). Treat as a ~45-min hold. Examples:
       - **Festival of the Lion King (Animal Kingdom):** in-the-round
         theater, ~30 min, very popular — arrive 20 min early.
       - **Indiana Jones Epic Stunt Spectacular (Hollywood Studios):**
         ~30 min outdoor stunt show, 5x daily. Easy walk-up; arrive
         10-15 min early.
       - **Festival of Fantasy / Mickey's Magical Friendship Faire
         on the Castle Forecourt Stage (Magic Kingdom):** ~20 min,
         outdoor castle stage. Sightlines from in front of the
         castle are fine; arrive at start time.
       - **Beauty and the Beast Live on Stage (Hollywood Studios):**
         ~25 min, outdoor amphitheater. Arrive 15 min early.

       The web app at `/parks/<park>/today` shows the full lineup
       (including atmosphere acts and character meets) — mention it
       as the place to browse what else is running if the user wants
       more detail than the headliner list here.

    6. **Weather + heat.** Outdoor rides (coasters like Big Thunder,
       TRON, Splash, Slinky Dog; water rides like Pirates, Splash,
       Kali) close for lightning (weather_code 95/96/99). Heavy rain
       (weather_code 80+ or precipitation_chance > 70%) means outdoor
       rides become miserable even when they stay open. Florida
       afternoon heat is its own factor — outdoor queues with no
       shade are uncomfortable above ~85°F and brutal above ~90°F.
       Use general Disney knowledge to classify each ride as
       indoor / outdoor / partially-covered. Then sequence:
       - Imminent thunderstorms in `weather.next_6h` → push outdoor
         rides to the clear hours, do indoor rides during the storm.
       - Hot now (>~88°F) but cooler later in the forecast → defer
         outdoor rides to the cooler window, do indoor rides now.
       - Currently cooler than later → do outdoor rides now while
         comfortable, save indoor for when heat peaks.
       - Always-comfortable day → ignore temperature, decide on
         cost-of-delay + proximity alone.

    6.5. **Water rides are the hot-day exception** — and have their
       own scheduling caveats. The two significant soak rides at WDW
       are Tiana's Bayou Adventure (Magic Kingdom, formerly Splash
       Mountain) and Kali River Rapids (Animal Kingdom). Both
       genuinely soak you — Kali is the more aggressive of the two
       (drenched, not damp).

       **Heat + sun = optimal conditions for these, not the time to
       avoid them.** Invert the normal "defer outdoor rides when
       hot" logic for water rides specifically: treat them as
       INDOOR-equivalent (or better) during the hot-afternoon
       window. The whole appeal is the cooldown.

       But two constraints that matter for sequencing:

       - **Don't schedule a water ride immediately before any
         extended indoor AC stop.** Table-service dining, indoor
         stage shows (Festival of the Lion King at AK, Beauty and
         the Beast Live at HS, Carousel of Progress at MK), or
         long indoor queues with strong AC will make a soaked
         guest genuinely miserable for 30+ minutes. Allow
         ~30-60 min in sun/warm air to dry before the next indoor
         stop, OR sequence another outdoor activity (a coaster, a
         walking break, an outdoor quick-service stop) in between
         to bridge the dry-out window.
       - **Don't schedule water rides when it's cool.** Below
         ~70°F, getting soaked is unpleasant; below ~60°F (rare in
         FL but possible Dec-Feb mornings), actively avoid. Check
         `weather.current.temp_f` before recommending.

       When a user has a water ride on their list:
         1. Find a hot-afternoon slot (warmer than 80°F ideally)
         2. Verify what's scheduled immediately after — if it's
            indoor dining or an indoor show, push the water ride
            earlier or insert a 30-60 min outdoor buffer
         3. If the day is cool throughout, mention it to the user
            and ask if they still want to ride — don't silently
            include a soak ride on a 65°F day

    7. **After the user accepts a plan, persist it for the feedback
       loop.** Once the user signals acceptance ("let's do that",
       "starting with Pirates", "sounds good"), call record_plan with
       a compact snapshot:
         - park
         - ride_sequence: ordered list of {ride_name,
           predicted_wait_min, position} from the plan you just laid
           out (use today_vs_forecast-adjusted predictions, not raw
           forecast values)
         - show_selections: any shows being fitted in (with
           performance_start + your predicted_arrival_min)
         - context: small dict like {park_load_ratio:
           today_vs_forecast.park_load_ratio, weather_summary:
           "<temp>F, <conditions>"}
         - notes: any user-stated constraints ("dining at 6pm",
           "skipping water rides")
       record_plan returns a plan_id; mention it briefly to the user
       so they can reference it later if needed ("logged as plan
       2026-05-10T18:00; you can give feedback next time we plan").

       DO NOT call record_plan for plans the user didn't accept
       (alternates they asked about and rejected, hypothetical "what
       if" planning, or pure information queries with no commitment).
       The point is to capture plans the user actually intends to
       follow.

       **Future days + multi-day trips.** Everything above is the
       same-day path — record_plan defaults to today and auto-activates,
       so the poller watches it immediately. To build a day the user
       ISN'T at yet, pass planned_for_date to record_plan, or use
       create_trip to mint a whole trip (one dormant day per date) in a
       single call. Dormant rows fire no alerts and survive until just
       past their trip day. On the trip day, after you re-evaluate
       against fresh live conditions (section 0d) and the user accepts,
       call activate_plan to turn on monitoring. Don't pre-activate
       future days, and don't record this session's live waits as if
       they were a future date's.

       Then LATER in the same conversation, if the user signals
       end-of-trip ("we're heading out", "thanks, that worked",
       "we're done") OR reports outcomes incrementally throughout
       the day, call record_plan_outcome with the same plan_id and
       whatever feedback you've gathered. If they don't, the next
       planning session will pick it up via the section 0a check.

    Args:
        park: Park key or human name. Accepts 'magic_kingdom',
            'Magic Kingdom', 'MK', etc.
        ride_names: List of ride names (substring match, case
            insensitive). Each name resolved against the historical
            snapshot. Unresolved names appear in `unresolved` instead
            of being silently dropped.

    Returns:
        Dict with park, current_time_et, park_hours (open/close/
        minutes_until_close), weather (current + 6h forecast),
        showtimes (headliner-only, remaining-today performances; None
        if the showtimes API fetch failed), rides (list with full
        per-ride context), and unresolved (list of ride_names that
        couldn't be matched). On AWS auth failure for the live-data
        portion, falls back to per-ride error blocks rather than
        dropping the whole call.
    """
    park_key = _normalize_park(park)
    now_et = datetime.now(_EASTERN)

    # Resolve every ride name first (offline lookup against the
    # snapshot). Unmatched names go into `unresolved` so the model
    # can flag them to the user instead of silently planning around
    # the rides it could resolve.
    resolved: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for name in ride_names:
        try:
            r = _find_ride(name)
        except ValueError:
            unresolved.append(name)
            continue
        resolved.append(r)

    table = None
    table_error: dict[str, Any] | None = None
    try:
        table = _ddb_table()
    except Exception as e:
        err = _aws_error_payload(e)
        table_error = err if err is not None else {
            "error": "DDB connection failed",
            "error_message": str(e),
        }

    locations = _locations()
    rides_out: list[dict[str, Any]] = []

    for ride in resolved:
        rid = ride["ride_id"]
        out: dict[str, Any] = {
            "ride_name": ride["ride_name"],
            "ride_id": rid,
            "park_key": ride.get("park_key"),
        }
        # Static location lookup — cheap, always fill it in if we have it
        loc = locations.get(rid)
        if loc:
            out["location"] = {"lat": loc["lat"], "lon": loc["lon"]}

        # LL drop pattern (from the historical snapshot) — used by the
        # planner to suggest when to check the app for a better slot.
        # Surface a compact summary; full histograms available via
        # get_ride_ll_drops if Claude wants the breakdown.
        ll_drops_total = ride.get("ll_drops_total")
        if ll_drops_total:
            drop_hours = ride.get("ll_drop_hours") or []
            top_hours = sorted(drop_hours, key=lambda x: -x["count"])[:3]
            out["ll_drop_pattern"] = {
                "drops_per_active_day": ride.get("ll_drops_per_active_day"),
                "typical_shift_minutes": ride.get("ll_typical_shift_mins"),
                "top_drop_hours_et": [h["hour"] for h in top_hours],
                "sample_size_days": ride.get("ll_active_days"),
            }

        if table_error is not None or table is None:
            out.update(table_error or {"error": "DDB unavailable"})
            rides_out.append(out)
            continue

        # 1. STATE row (current status, wait, LL, last_seen, last_forecast_at)
        try:
            state_resp = table.get_item(
                Key={"PK": f"RIDE#{rid}", "SK": "STATE"}
            )
            state = _convert_decimals(state_resp.get("Item")) if state_resp.get("Item") else None
        except Exception as e:
            err = _aws_error_payload(e)
            out.update(err or {"error": "STATE read failed", "error_message": str(e)})
            rides_out.append(out)
            continue

        if not state:
            out["live_state_available"] = False
            rides_out.append(out)
            continue

        out["status"] = state.get("status")
        out["wait_mins"] = state.get("wait_mins")
        out["current_ll_offer"] = state.get("ll")
        out["last_seen"] = state.get("last_seen")
        out["last_forecast_at"] = state.get("last_forecast_at")

        # 2. DOWN enrichment (only when status=DOWN)
        if state.get("status") == "DOWN":
            try:
                ds_resp = table.get_item(
                    Key={"PK": f"RIDE#{rid}", "SK": "DOWN_SINCE"}
                )
                ds_item = ds_resp.get("Item")
                if ds_item and ds_item.get("down_since"):
                    out["down_since"] = ds_item["down_since"]
                    try:
                        down_dt = datetime.fromisoformat(ds_item["down_since"])
                        elapsed = datetime.now(timezone.utc) - down_dt
                        out["down_duration_mins"] = round(
                            elapsed.total_seconds() / 60, 1
                        )
                    except ValueError:
                        pass
            except Exception:
                pass

            clusters = ride.get("down_clusters", [])
            durations = sorted(
                c["duration_minutes"] for c in clusters
                if isinstance(c.get("duration_minutes"), (int, float))
            )
            if durations:
                mid = len(durations) // 2
                if len(durations) % 2 == 1:
                    median = durations[mid]
                else:
                    median = (durations[mid - 1] + durations[mid]) / 2
                out["typical_down_cluster_mins"] = round(median, 1)
                out["historical_cluster_count"] = len(durations)
                cur = out.get("down_duration_mins")
                if cur is not None and median > 0:
                    out["cluster_progress_pct"] = round(100.0 * cur / median, 1)

        # 3. Latest forecast snapshot
        try:
            f_resp = table.query(
                KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
                ExpressionAttributeValues={
                    ":pk": f"RIDE#{rid}",
                    ":sk": "FORECAST#",
                },
                ScanIndexForward=False,
                Limit=1,
            )
            f_items = f_resp.get("Items", [])
            if f_items:
                f_item = _convert_decimals(f_items[0])
                forecast = f_item.get("forecast", []) or []
                out["forecast_polled_at"] = f_item.get("polled_at")
                out["forecast"] = forecast
                # Peak-in-next-3h is the cost-of-delay signal that
                # matters for planning. The old full-horizon slope
                # was misleading for hump-shaped curves like Pirates
                # of the Caribbean (afternoon peak → drops by close).
                peak = _forecast_peak_in_window(forecast, hours_ahead=3)
                if peak is not None:
                    out["forecast_peak_next_3h_mins"] = peak["peak_wait_mins"]
                    out["forecast_minutes_until_peak"] = peak["minutes_until_peak"]
                    out["forecast_peak_at"] = peak["peak_at"]
            else:
                # Common when ride is currently DOWN — themeparks.wiki
                # stops forecasting DOWN rides. Note that explicitly
                # rather than leaving the field absent ambiguously.
                out["forecast"] = None
                out["forecast_unavailable_reason"] = (
                    "No forecast row in DDB. Usually means the upstream "
                    "API isn't predicting this ride right now (most often "
                    "because it's DOWN, walk-up, or no-queue)."
                )
        except Exception:
            out["forecast"] = None

        rides_out.append(out)

    # Headliner-only showtimes with remaining-today performances. We
    # filter aggressively here (vs. exposing the full park lineup via
    # get_park_showtimes) because get_planning_context is already
    # token-heavy and atmosphere acts / character meets aren't
    # planner-relevant. The model can fall back to get_park_showtimes
    # if it needs the rest.
    now_iso = now_et.isoformat()
    all_shows = _fetch_park_showtimes(park_key)
    headliner_showtimes: list[dict[str, Any]] | None
    if all_shows is None:
        headliner_showtimes = None
    else:
        headliner_showtimes = []
        for show in all_shows:
            if show["category"] not in _SHOW_HEADLINER_CATEGORIES:
                continue
            remaining = [t for t in show["showtimes"] if t["start"] > now_iso]
            if not remaining:
                continue
            headliner_showtimes.append({
                "name": show["name"],
                "category": show["category"],
                "remaining_today": remaining,
            })

    return {
        "park": park_key,
        "current_time_et": now_et.isoformat(),
        "park_hours": _fetch_park_hours_today(park_key),
        "weather": _fetch_weather_forecast(),
        "today_vs_forecast": _compute_load_vs_forecast(rides_out),
        "currently_down_in_park": _fetch_park_currently_down(table, park_key),
        "showtimes": headliner_showtimes,
        "rides": rides_out,
        "unresolved": unresolved,
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
            claims = self._verify(token)
            # Stash the verified subject for write-tool attribution
            # (created_by). ContextVar propagates into the tool handlers
            # within this request. Defensive: injected test verifiers may
            # not return a dict.
            _authenticated_sub.set(
                claims.get("sub") if isinstance(claims, dict) else None
            )
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
