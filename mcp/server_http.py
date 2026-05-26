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

**v1 scope (read-only, no writes).**
- `hello_magic_monitor` — sanity ping
- `get_live_ride_status` — single-ride GetItem
- `get_park_live_status` — park-wide Scan with pagination

The full plan-feedback loop (record_plan / mark_ride_complete /
record_plan_outcome), the analytics-snapshot-bundled tools, and
the heavyweight `get_planning_context` follow in later sessions
once OAuth is in place and we've added write-side IAM.

**Auth (session 1 only — placeholder).** This file accepts a
single shared bearer secret via the `MCP_BEARER_SECRET` env var.
That is intentionally NOT production-grade: it's there to prove
the transport layer and IAM wiring before we layer Cognito
OAuth + DCR proxy on top in session 2. The CDK stack pulls the
secret from SSM and binds it as an env var on the Lambda; the
secret never sits in source.

**Stateless.** Lambda doesn't keep state across invocations, so
the streamable-HTTP transport must run in stateless mode — each
request is self-contained and doesn't rely on a server-side
session. FastMCP supports this via the `stateless_http=True`
setting; we pass it at construction.
"""

import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# ─── Config / constants ─────────────────────────────────────────────
# Mirror server.py's DDB region pin. In Lambda there's no profile —
# boto3 uses the execution role's credentials via the default chain.
_DDB_REGION = os.environ.get("DISNEY_REGION", "us-east-2")
_DDB_TABLE = os.environ.get("DISNEY_TABLE_NAME", "DisneyData")

# v1 auth: shared bearer secret. The CDK stack reads from SSM at
# deploy time and binds it as an env var on the Lambda. The secret
# itself never ships in this repo. Empty string == auth disabled
# (used in unit tests; never in production — the CDK stack enforces
# a non-empty value).
_BEARER_SECRET = os.environ.get("MCP_BEARER_SECRET", "")

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
        "v1 tools available: get_live_ride_status, get_park_live_status. "
        "Full tool surface ships in later sessions."
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


# ─── Bearer-token middleware ────────────────────────────────────────
# v1 only: a single shared bearer secret gates every request. Session
# 2 replaces this with the Cognito OAuth verifier that the MCP SDK
# already supports natively (FastMCP._token_verifier). The middleware
# pattern is intentional — when we swap to OAuth, the auth gate moves
# from this custom check to the SDK's built-in path, and this whole
# class can be deleted.

class _BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Don't gate health / OPTIONS — API Gateway sometimes pings
        # the root for CORS preflight or health-check probes.
        if request.method == "OPTIONS":
            return await call_next(request)

        if not _BEARER_SECRET:
            # Misconfigured — refuse every request rather than serve
            # unauthenticated. The CDK stack always sets this, so an
            # empty secret in production means something went wrong.
            return JSONResponse(
                {"error": "server not configured (missing bearer secret)"},
                status_code=503,
            )

        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse(
                {"error": "missing or malformed Authorization header"},
                status_code=401,
            )

        token = auth[len("Bearer "):].strip()
        # Constant-time compare to avoid timing side-channels on the
        # short v1 secret. secrets.compare_digest works on str.
        import secrets as _secrets
        if not _secrets.compare_digest(token, _BEARER_SECRET):
            return JSONResponse({"error": "invalid bearer token"}, status_code=401)

        return await call_next(request)


# ─── Public ASGI app ────────────────────────────────────────────────
# Build the streamable-HTTP app and wrap it with our bearer-auth
# middleware. The Lambda handler imports `app` and hands it to
# Mangum; nothing else in this module is part of the public surface.

app = mcp.streamable_http_app()
app.add_middleware(_BearerAuthMiddleware)


if __name__ == "__main__":
    # Local debugging entrypoint — run with `python server_http.py`
    # to expose the HTTP server on :8000 for curl testing without
    # going through Lambda. Requires `pip install uvicorn` locally.
    # Lambda doesn't use this path; the handler imports `app`
    # directly.
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
