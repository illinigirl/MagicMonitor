"""AWS Lambda entry point for the HTTPS MCP transport.

Wires API Gateway HTTP API → Lambda → Mangum (ASGI adapter) → the
Starlette ASGI app produced by FastMCP.streamable_http_app() (defined
in server_http.py).

**Init order matters.** server_http.py reads MCP_BEARER_SECRET from
the env at module-load time. We fetch it from SSM before importing
server_http so the auth middleware sees the live value. If we
imported first and fetched after, every request would 503 on a
"missing bearer secret" check until the next cold start.

**Lambda-vs-MCP-SDK lifespan mismatch.** This is the gnarliest part
of the integration; documenting in detail so the next person hitting
it doesn't repeat the debug arc:

1. FastMCP's streamable_http_app() returns a Starlette app whose
   lifespan handler calls `session_manager.run()` — a
   `@asynccontextmanager` that opens an anyio task group required
   for processing requests.

2. `StreamableHTTPSessionManager.run()` enforces single-use:
   `_has_started=True` is checked on entry; second entry raises
   `RuntimeError: ".run() can only be called once per instance"`.
   The SDK assumes a long-running server (uvicorn-style) where
   lifespan startup happens once.

3. Mangum's `lifespan="off"` skips lifespan entirely → task group
   never created → every request gets
   `RuntimeError: Task group is not initialized.`

4. Mangum's `lifespan="on"` runs the full lifespan startup+shutdown
   cycle on EVERY invocation (not once-per-cold-start). Second
   invocation hits the single-use guard → 500.

The fix: Mangum lifespan="off" + per-request entry into
`session_manager.run()` via our own ASGI wrapper, with a reset of
the `_has_started` flag after exit so the next invocation can
re-enter. The reset is a known intrusion into SDK internals; the
alternative would be re-constructing the FastMCP instance per
request (heavy) or moving off Mangum to AWS Lambda Web Adapter
(more rework than session 1 warrants).
"""

import os


def _bootstrap_bearer_secret() -> None:
    """Hydrate MCP_BEARER_SECRET from SSM if not already in env."""
    if os.environ.get("MCP_BEARER_SECRET"):
        return
    param_name = os.environ.get("MCP_BEARER_SECRET_PARAM")
    if not param_name:
        # Local-dev case: no SSM param, no secret. server_http's
        # middleware will 503 every request — that's the right
        # behavior, prevents accidentally serving unauthenticated.
        return
    import boto3
    ssm = boto3.client("ssm")
    resp = ssm.get_parameter(Name=param_name, WithDecryption=True)
    os.environ["MCP_BEARER_SECRET"] = resp["Parameter"]["Value"]


_bootstrap_bearer_secret()

# Imports MUST come after the bootstrap above — server_http reads
# the env var at module load.
from mangum import Mangum  # noqa: E402
from server_http import app, mcp  # noqa: E402


async def _asgi_with_per_request_session(scope, receive, send):
    """ASGI wrapper that manages the streamable-HTTP session manager
    on a per-request basis instead of via Starlette's lifespan.

    See the module docstring for why this exists.
    """
    if scope["type"] == "lifespan":
        # Mangum lifespan="off" should suppress these, but if any
        # do arrive (or if Mangum's behavior changes), respond
        # cleanly without invoking the SDK's lifespan handler.
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
            else:
                return

    # Reset the single-use guard so subsequent invocations in the
    # same warm container can re-enter run(). The task group + any
    # in-flight state from the previous request has already been
    # cleaned up by run()'s `finally` block.
    mcp.session_manager._has_started = False
    async with mcp.session_manager.run():
        await app(scope, receive, send)


# lifespan="off" is critical: it tells Mangum NOT to invoke
# Starlette's lifespan (which would call session_manager.run()
# from a path we don't control). Our wrapper manages the
# session lifecycle per-request.
handler = Mangum(_asgi_with_per_request_session, lifespan="off", api_gateway_base_path="/")
