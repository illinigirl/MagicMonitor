"""Tests for the OAuth + DCR routes and Cognito JWT middleware in server_http.

Covers the four public surfaces session 2B added:

1. `/.well-known/oauth-protected-resource` returns RFC 9728 shape.
2. `/.well-known/oauth-authorization-server` returns RFC 8414 shape
   with the DCR-proxy quirk (issuer = our URL, endpoints = Cognito).
3. `POST /register` happy path + invalid-payload error path.
4. `_CognitoJwtMiddleware`:
   - lets OPTIONS + well-known + /register through unauthenticated
   - rejects missing/malformed Authorization
   - rejects verifier failure (VerifyError → 401)
   - converts unexpected verifier exceptions to 503 (JWKS network blip)
   - accepts a valid token via injected verifier → request reaches the app

The middleware is exercised via a tiny Starlette test app wrapping a
single `/mcp/echo` route so we don't have to spin up the full FastMCP
streamable-HTTP machinery.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

# Env vars referenced by the metadata helpers must be set BEFORE
# importing server_http so the helpers see them on first call. The
# helpers read os.environ dynamically (per call), so we can also
# monkeypatch in individual tests if we need different values.
os.environ.setdefault("MCP_PUBLIC_BASE_URL", "https://mcp.example.com")
os.environ.setdefault("COGNITO_USER_POOL_ID", "us-east-2_TESTPOOL")
os.environ.setdefault("COGNITO_REGION", "us-east-2")
os.environ.setdefault("COGNITO_DOMAIN_URL", "https://auth.example.com")

import jwt_verifier  # noqa: E402
import server_http  # noqa: E402


# ─── Metadata route shape tests ─────────────────────────────────────


class TestProtectedResourceMetadata:
    def test_shape_matches_rfc9728(self):
        meta = server_http._protected_resource_metadata()
        assert meta["resource"] == "https://mcp.example.com"
        assert meta["authorization_servers"] == ["https://mcp.example.com"]


class TestAuthorizationServerMetadata:
    def test_issuer_is_our_base_url(self):
        meta = server_http._authorization_server_metadata()
        assert meta["issuer"] == "https://mcp.example.com"

    def test_authorize_token_endpoints_point_at_cognito(self):
        meta = server_http._authorization_server_metadata()
        assert meta["authorization_endpoint"] == "https://auth.example.com/oauth2/authorize"
        assert meta["token_endpoint"] == "https://auth.example.com/oauth2/token"

    def test_registration_endpoint_points_at_us(self):
        meta = server_http._authorization_server_metadata()
        assert meta["registration_endpoint"] == "https://mcp.example.com/register"

    def test_jwks_uri_points_at_cognito_pool(self):
        meta = server_http._authorization_server_metadata()
        assert meta["jwks_uri"] == (
            "https://cognito-idp.us-east-2.amazonaws.com/us-east-2_TESTPOOL/.well-known/jwks.json"
        )

    def test_advertises_pkce_and_public_client(self):
        meta = server_http._authorization_server_metadata()
        assert meta["code_challenge_methods_supported"] == ["S256"]
        assert meta["token_endpoint_auth_methods_supported"] == ["none"]


# ─── DCR /register route tests ──────────────────────────────────────


@pytest.fixture
def cognito_stub():
    """Stub the boto3 cognito-idp client used inside dcr_proxy."""
    import datetime
    stub = MagicMock()
    stub.create_user_pool_client.return_value = {
        "UserPoolClient": {
            "ClientId": "test-client-id-12345",
            "ClientName": "test-client",
            "CreationDate": datetime.datetime(
                2026, 5, 28, 12, 0, 0, tzinfo=datetime.timezone.utc
            ),
            "CallbackURLs": ["https://claude.ai/mcp/callback"],
        }
    }
    return stub


def _make_test_client(verifier=None):
    """Build a Starlette TestClient over a minimal app + the middleware."""
    async def echo(request):
        return JSONResponse({"ok": True, "path": request.url.path})

    app = Starlette(routes=[
        Route("/mcp/echo", echo, methods=["GET", "POST"]),
    ])
    app.add_middleware(server_http._CognitoJwtMiddleware, verifier=verifier)
    return TestClient(app)


class TestRegisterRoute:
    def test_happy_path_returns_201_and_client_id(self, cognito_stub):
        client = _make_test_client()
        with patch("boto3.client", return_value=cognito_stub):
            resp = client.post(
                "/register",
                json={"redirect_uris": ["https://claude.ai/mcp/callback"]},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["client_id"] == "test-client-id-12345"
        assert body["token_endpoint_auth_method"] == "none"

    def test_invalid_payload_returns_400(self, cognito_stub):
        client = _make_test_client()
        with patch("boto3.client", return_value=cognito_stub):
            # Missing required `redirect_uris`
            resp = client.post("/register", json={"client_name": "x"})
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"] == "invalid_client_metadata"
        assert "redirect_uris" in body["error_description"]

    def test_non_json_body_returns_400(self):
        client = _make_test_client()
        resp = client.post(
            "/register",
            content=b"not json at all",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_client_metadata"


# ─── Middleware bypass + auth tests ─────────────────────────────────


class TestMiddlewarePublicRoutes:
    def test_well_known_protected_resource_no_auth(self):
        client = _make_test_client()
        resp = client.get("/.well-known/oauth-protected-resource")
        assert resp.status_code == 200
        assert resp.json()["resource"] == "https://mcp.example.com"

    def test_well_known_authorization_server_no_auth(self):
        client = _make_test_client()
        resp = client.get("/.well-known/oauth-authorization-server")
        assert resp.status_code == 200
        assert "authorization_endpoint" in resp.json()

    def test_options_bypass(self):
        # OPTIONS to a protected path should pass to the inner app
        # without an auth check (the inner app 405s, which is fine —
        # we're only verifying we got past the auth gate).
        client = _make_test_client()
        resp = client.options("/mcp/echo")
        # Starlette's default OPTIONS handling returns 405 for routes
        # without an OPTIONS handler. The important check is "not 401".
        assert resp.status_code != 401


class TestMiddlewareAuthGate:
    def test_missing_authorization_header_returns_401(self):
        client = _make_test_client()
        resp = client.get("/mcp/echo")
        assert resp.status_code == 401
        assert "Authorization" in resp.json()["error"]

    def test_non_bearer_scheme_returns_401(self):
        client = _make_test_client()
        resp = client.get(
            "/mcp/echo",
            headers={"authorization": "Basic dGVzdDp0ZXN0"},
        )
        assert resp.status_code == 401

    def test_verify_error_returns_401(self):
        def reject(token):
            raise jwt_verifier.VerifyError("sub not in allowlist")

        client = _make_test_client(verifier=reject)
        resp = client.get(
            "/mcp/echo",
            headers={"authorization": "Bearer some.jwt.token"},
        )
        assert resp.status_code == 401
        # Generic error — middleware must not leak which check failed
        assert resp.json()["error"] == "invalid token"

    def test_unexpected_verifier_exception_returns_503(self):
        def jwks_blip(token):
            raise RuntimeError("connection reset to JWKS endpoint")

        client = _make_test_client(verifier=jwks_blip)
        resp = client.get(
            "/mcp/echo",
            headers={"authorization": "Bearer some.jwt.token"},
        )
        assert resp.status_code == 503

    def test_valid_token_reaches_app(self):
        def accept(token):
            return {"sub": "test-sub", "token_use": "access"}

        client = _make_test_client(verifier=accept)
        resp = client.get(
            "/mcp/echo",
            headers={"authorization": "Bearer some.jwt.token"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


class TestMiddlewareDoesNotMisroute:
    """Public routes use exact path match — don't accidentally match
    longer paths or wrong methods that should hit the auth gate."""

    def test_post_to_well_known_still_requires_auth(self):
        client = _make_test_client()
        resp = client.post("/.well-known/oauth-protected-resource")
        # Method mismatch → falls through to auth gate
        assert resp.status_code == 401

    def test_get_to_register_still_requires_auth(self):
        client = _make_test_client()
        resp = client.get("/register")
        assert resp.status_code == 401

    def test_unknown_path_requires_auth(self):
        client = _make_test_client()
        resp = client.get("/some/random/path")
        assert resp.status_code == 401
