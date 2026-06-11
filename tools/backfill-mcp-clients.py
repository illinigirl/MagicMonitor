#!/usr/bin/env python3
"""One-time backfill of MCPCLIENT# marker rows for the registered-client
auth gate (review finding #8).

WHY THIS EXISTS
---------------
The MCP HTTPS server now requires every access token's `client_id` to
belong to a client this server's DCR proxy created — recorded as an
`MCPCLIENT#<client_id> / META` row written at /register time, checked on
every request (see mcp/server_http.py). Clients that registered BEFORE
that code shipped have no marker row, so the moment the gate deploys they
would be rejected (403) until each device reconnects and re-registers.

This script writes the missing markers for the EXISTING DCR/MCP clients
so the deploy is seamless — run it just before (or right after) the
DisneyMcpStack deploy that turns the gate on.

WHAT IT MARKS (and what it must NOT)
------------------------------------
The Cognito pool also backs the web dashboard, whose client is exactly
what #8 is meant to reject. So this script must mark ONLY the DCR/MCP
clients, never the dashboard client. The discriminator is public-vs-
confidential:

  • DCR/MCP clients are PUBLIC PKCE clients (no client secret) — the DCR
    proxy creates them with GenerateSecret=False.
  • The web dashboard client is CONFIDENTIAL (has a client secret; see
    web/src/auth.ts "uses a confidential client").

So: public client (no secret) -> mark it; confidential client -> skip.

SAFETY
------
Dry-run by default — prints the classification and exits without writing.
Re-run with --apply to write the markers (idempotent: put_item keyed on
MCPCLIENT#<id>/META, so re-running is harmless). Eyeball the dry-run
report first: confirm the dashboard client shows as SKIP (confidential)
and your + Jim's Claude installs show as MARK (public).

Usage:
    # SSO creds first:
    aws sso login --profile watchtower
    # Review what it would do:
    python tools/backfill-mcp-clients.py
    # Then apply:
    python tools/backfill-mcp-clients.py --apply
"""

import argparse
import sys
from datetime import datetime, timezone

import boto3

# Same constants the stack + server use. Overridable via flags.
DEFAULT_PROFILE = "watchtower"
DEFAULT_REGION = "us-east-2"
DEFAULT_TABLE = "DisneyData"
DEFAULT_POOL_ID = "us-east-2_ORhu761AY"

_MCP_CLIENT_PK_PREFIX = "MCPCLIENT#"  # mirrors mcp/server_http.py


def _list_clients(cognito, pool_id):
    """Yield every app client (ClientId, ClientName) on the pool."""
    kwargs = {"UserPoolId": pool_id, "MaxResults": 60}
    while True:
        resp = cognito.list_user_pool_clients(**kwargs)
        yield from resp.get("UserPoolClients", [])
        token = resp.get("NextToken")
        if not token:
            return
        kwargs["NextToken"] = token


def _is_public_client(cognito, pool_id, client_id):
    """A client is public (DCR/MCP) iff Cognito returns no ClientSecret."""
    desc = cognito.describe_user_pool_client(
        UserPoolId=pool_id, ClientId=client_id
    )["UserPoolClient"]
    return "ClientSecret" not in desc, desc


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="Write the marker rows (default: dry-run, write nothing).")
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--table", default=DEFAULT_TABLE)
    parser.add_argument("--pool-id", default=DEFAULT_POOL_ID)
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    cognito = session.client("cognito-idp")
    table = session.resource("dynamodb").Table(args.table)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] pool={args.pool_id} table={args.table} "
          f"(region {args.region}, profile {args.profile})\n")

    to_mark = []   # (client_id, client_name)
    skipped = []   # (client_id, client_name, reason)
    try:
        for c in _list_clients(cognito, args.pool_id):
            cid, name = c["ClientId"], c.get("ClientName", "")
            public, _desc = _is_public_client(cognito, args.pool_id, cid)
            if public:
                to_mark.append((cid, name))
            else:
                skipped.append((cid, name, "confidential (has client secret)"))
    except Exception as e:
        print(f"ERROR listing/describing clients: {e}", file=sys.stderr)
        print("Are your SSO creds fresh? `aws sso login --profile "
              f"{args.profile}`", file=sys.stderr)
        return 1

    print("SKIP (not an MCP/DCR client):")
    for cid, name, reason in skipped:
        print(f"  - {cid}  {name!r}  — {reason}")
    if not skipped:
        print("  (none)")

    print("\nMARK (public PKCE → DCR/MCP client):")
    for cid, name in to_mark:
        print(f"  - {cid}  {name!r}")
    if not to_mark:
        print("  (none)")

    print(f"\n{len(to_mark)} client(s) to mark, {len(skipped)} skipped.")

    if not args.apply:
        print("\nDry-run only — nothing written. Re-run with --apply once the "
              "SKIP/MARK split looks right (the dashboard client must be SKIP).")
        return 0

    now_iso = datetime.now(timezone.utc).isoformat()
    written = 0
    for cid, name in to_mark:
        item = {
            "PK": f"{_MCP_CLIENT_PK_PREFIX}{cid}",
            "SK": "META",
            "registered_at": now_iso,
            "backfilled": True,
        }
        if name:
            item["client_name"] = name
        table.put_item(Item=item)
        written += 1
    print(f"\nWrote {written} marker row(s). Existing MCP installs will pass "
          "the registered-client check after the gate deploys.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
