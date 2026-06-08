"""JWT verifier for Cognito access tokens.

Verifies signature via the user pool's JWKS, checks issuer + expiration
+ token_use, and enforces a sub-claim allowlist as the hard cap on who
can hit the MCP server.

**Why an allowlist on top of valid Cognito auth?** The Cognito user
pool is shared between Magic Monitor and Watchtower (one pool, two app
clients). A valid Watchtower-only user could in theory walk through
DCR → /authorize → /token → /mcp/*. The allowlist is the bouncer:
even with a perfectly valid signed access token, `sub` must be one of
the family UUIDs explicitly bound at deploy time.

**Stateless verify, cached JWKS.** Each request re-verifies the token
against an in-memory JWKS cache. The cache survives across warm Lambda
invocations (module globals persist) and is repopulated on the first
verify after a cold start. JWKS rotation is rare (Cognito controls the
schedule); if a token arrives with a `kid` we don't have, we evict the
cache and re-fetch once before failing.

**Wired in session 2B.** `mcp/server_http.py` imports this module and
installs `_CognitoJwtMiddleware`, which calls `verify_token()` on every
non-public request and returns 401 on failure. The old shared-bearer
middleware was hard-replaced (no dual-auth path). `MCP_ALLOWED_SUBS` +
`COGNITO_*` env vars are set on the Lambda by the CDK stack.
"""

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

import jwt
import jwt.algorithms


class VerifyError(Exception):
    """Raised for any verification failure — signature, claims, allowlist.

    Callers should map this to HTTP 401 without leaking the message to
    the client (the message is for server-side logs only; an attacker
    probing the auth gate shouldn't learn which check failed).
    """


@dataclass(frozen=True)
class VerifierConfig:
    """All inputs to the verifier, derivable from env vars at deploy time."""

    issuer: str
    allowed_subs: frozenset[str]
    jwks_url: str


def config_from_env() -> VerifierConfig:
    """Build a VerifierConfig from the standard env vars.

    Required: COGNITO_USER_POOL_ID.
    Optional: COGNITO_REGION (default us-east-2), MCP_ALLOWED_SUBS
    (comma-separated; empty == deny-all, which is the safe default).
    """
    user_pool_id = os.environ["COGNITO_USER_POOL_ID"]
    region = os.environ.get("COGNITO_REGION", "us-east-2")
    raw_subs = os.environ.get("MCP_ALLOWED_SUBS", "")
    subs = frozenset(s.strip() for s in raw_subs.split(",") if s.strip())
    issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
    return VerifierConfig(
        issuer=issuer,
        allowed_subs=subs,
        jwks_url=f"{issuer}/.well-known/jwks.json",
    )


# Module-level JWKS cache. Keyed by URL so a future multi-pool setup
# (unlikely) wouldn't cross the streams. Warm Lambda containers reuse
# this across invocations; cold starts repopulate.
_jwks_cache: dict[str, dict[str, Any]] = {}


def _default_jwks_loader(url: str) -> dict[str, Any]:
    """HTTPS GET the JWKS, with in-memory caching. Injectable for tests."""
    if url in _jwks_cache:
        return _jwks_cache[url]
    import requests
    resp = requests.get(url, timeout=5)
    resp.raise_for_status()
    data = resp.json()
    _jwks_cache[url] = data
    return data


def verify_token(
    token: str,
    *,
    config: VerifierConfig | None = None,
    jwks_loader: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Verify a Cognito access token. Returns claims on success.

    Raises VerifyError on any failure: malformed token, bad signature,
    wrong issuer, expired, wrong token_use, sub not in allowlist.

    The optional `config` and `jwks_loader` knobs exist for tests —
    production callers pass nothing and pick up env-derived config and
    the HTTP loader.
    """
    cfg = config or config_from_env()
    loader = jwks_loader or _default_jwks_loader

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as e:
        raise VerifyError(f"malformed token header: {e}") from e

    kid = header.get("kid")
    if not kid:
        raise VerifyError("token header missing 'kid'")

    jwk = _resolve_jwk(loader, cfg.jwks_url, kid)
    if jwk is None:
        raise VerifyError(f"no JWK matching kid={kid}")

    public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))

    try:
        claims = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            issuer=cfg.issuer,
            # Cognito access tokens don't populate `aud` — they use
            # `client_id` instead. Skip aud verification; issuer +
            # signature + token_use are the real checks.
            options={"verify_aud": False, "require": ["exp", "iss", "sub"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise VerifyError("token expired") from e
    except jwt.InvalidIssuerError as e:
        raise VerifyError(f"wrong issuer: {e}") from e
    except jwt.PyJWTError as e:
        raise VerifyError(f"token verify failed: {e}") from e

    # Cognito mints two token shapes: id (for client identity) and
    # access (for resource calls). Only access tokens are valid here.
    if claims.get("token_use") != "access":
        raise VerifyError(
            f"unexpected token_use={claims.get('token_use')!r} (expected 'access')"
        )

    sub = claims.get("sub")
    if sub not in cfg.allowed_subs:
        raise VerifyError(f"sub {sub!r} not in allowlist")

    return claims


def _resolve_jwk(
    loader: Callable[[str], dict[str, Any]],
    url: str,
    kid: str,
) -> dict[str, Any] | None:
    """Find a JWK matching `kid`; refresh the cache once on miss.

    JWKS rotation is rare but real (Cognito re-keys without warning).
    A first miss could be either a stale cache or an unknown key; we
    pay one refresh to disambiguate, then give up.
    """
    jwks = loader(url)
    jwk = _find_jwk(jwks, kid)
    if jwk is not None:
        return jwk
    _jwks_cache.pop(url, None)
    jwks = loader(url)
    return _find_jwk(jwks, kid)


def _find_jwk(jwks: dict[str, Any], kid: str) -> dict[str, Any] | None:
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key
    return None
