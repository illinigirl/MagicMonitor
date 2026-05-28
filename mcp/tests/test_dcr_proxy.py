"""Tests for mcp/dcr_proxy.py.

Stubs the boto3 Cognito client so the DCR validation + response-shaping
logic runs end-to-end without touching AWS. The stub records the
arguments passed to `create_user_pool_client` so the tests can assert
on the Cognito knobs (PKCE, no secret, callback allowlist) instead of
just the returned shape.
"""

import datetime
from unittest.mock import MagicMock

import pytest

from dcr_proxy import RegistrationError, register_client


_USER_POOL = "us-east-2_TESTPOOL"


@pytest.fixture
def cognito_stub():
    stub = MagicMock()
    stub.create_user_pool_client.return_value = {
        "UserPoolClient": {
            "ClientId": "abc123-cognito-client-id",
            "ClientName": "mcp-dcr-client",
            "CreationDate": datetime.datetime(
                2026, 5, 27, 12, 0, 0, tzinfo=datetime.timezone.utc
            ),
            "CallbackURLs": ["https://claude.ai/mcp/callback"],
        }
    }
    return stub


class TestHappyPath:
    def test_minimal_payload_creates_client(self, cognito_stub):
        resp = register_client(
            {"redirect_uris": ["https://claude.ai/mcp/callback"]},
            user_pool_id=_USER_POOL,
            cognito_client=cognito_stub,
        )
        assert resp["client_id"] == "abc123-cognito-client-id"
        assert resp["token_endpoint_auth_method"] == "none"
        assert resp["redirect_uris"] == ["https://claude.ai/mcp/callback"]
        assert resp["client_id_issued_at"] > 0
        assert resp["scope"] == "openid email profile"

    def test_calls_cognito_with_public_client_settings(self, cognito_stub):
        register_client(
            {"redirect_uris": ["https://claude.ai/mcp/callback"]},
            user_pool_id=_USER_POOL,
            cognito_client=cognito_stub,
        )
        kwargs = cognito_stub.create_user_pool_client.call_args.kwargs
        assert kwargs["UserPoolId"] == _USER_POOL
        assert kwargs["GenerateSecret"] is False
        assert kwargs["AllowedOAuthFlows"] == ["code"]
        assert kwargs["AllowedOAuthFlowsUserPoolClient"] is True
        assert kwargs["CallbackURLs"] == ["https://claude.ai/mcp/callback"]
        assert "Google" in kwargs["SupportedIdentityProviders"]

    def test_client_name_passed_through(self, cognito_stub):
        register_client(
            {
                "redirect_uris": ["https://claude.ai/mcp/callback"],
                "client_name": "Claude Mobile",
            },
            user_pool_id=_USER_POOL,
            cognito_client=cognito_stub,
        )
        kwargs = cognito_stub.create_user_pool_client.call_args.kwargs
        assert kwargs["ClientName"] == "Claude Mobile"

    def test_long_client_name_truncated_to_cognito_limit(self, cognito_stub):
        register_client(
            {
                "redirect_uris": ["https://claude.ai/mcp/callback"],
                "client_name": "x" * 200,
            },
            user_pool_id=_USER_POOL,
            cognito_client=cognito_stub,
        )
        kwargs = cognito_stub.create_user_pool_client.call_args.kwargs
        assert len(kwargs["ClientName"]) == 128

    def test_empty_client_name_falls_back_to_default(self, cognito_stub):
        register_client(
            {
                "redirect_uris": ["https://claude.ai/mcp/callback"],
                "client_name": "   ",
            },
            user_pool_id=_USER_POOL,
            cognito_client=cognito_stub,
        )
        kwargs = cognito_stub.create_user_pool_client.call_args.kwargs
        assert kwargs["ClientName"] == "mcp-dcr-client"

    def test_multiple_redirect_uris_passed_through(self, cognito_stub):
        uris = [
            "https://claude.ai/mcp/callback",
            "https://desktop.claude.ai/cb",
        ]
        register_client(
            {"redirect_uris": uris},
            user_pool_id=_USER_POOL,
            cognito_client=cognito_stub,
        )
        kwargs = cognito_stub.create_user_pool_client.call_args.kwargs
        assert kwargs["CallbackURLs"] == uris


class TestValidation:
    def test_missing_redirect_uris_rejected(self, cognito_stub):
        with pytest.raises(RegistrationError) as exc:
            register_client(
                {}, user_pool_id=_USER_POOL, cognito_client=cognito_stub
            )
        assert exc.value.code == "invalid_client_metadata"
        cognito_stub.create_user_pool_client.assert_not_called()

    def test_empty_redirect_uris_rejected(self, cognito_stub):
        with pytest.raises(RegistrationError) as exc:
            register_client(
                {"redirect_uris": []},
                user_pool_id=_USER_POOL,
                cognito_client=cognito_stub,
            )
        assert exc.value.code == "invalid_redirect_uri"
        cognito_stub.create_user_pool_client.assert_not_called()

    def test_non_https_remote_redirect_uri_rejected(self, cognito_stub):
        with pytest.raises(RegistrationError) as exc:
            register_client(
                {"redirect_uris": ["http://evil.example.com/cb"]},
                user_pool_id=_USER_POOL,
                cognito_client=cognito_stub,
            )
        assert exc.value.code == "invalid_redirect_uri"
        cognito_stub.create_user_pool_client.assert_not_called()

    def test_localhost_http_allowed(self, cognito_stub):
        """Loopback http is the RFC 8252 §7.3 desktop OAuth pattern —
        the only http:// case that should be allowed."""
        register_client(
            {"redirect_uris": ["http://localhost:3000/callback"]},
            user_pool_id=_USER_POOL,
            cognito_client=cognito_stub,
        )
        cognito_stub.create_user_pool_client.assert_called_once()

    def test_127_loopback_http_allowed(self, cognito_stub):
        register_client(
            {"redirect_uris": ["http://127.0.0.1:8080/cb"]},
            user_pool_id=_USER_POOL,
            cognito_client=cognito_stub,
        )
        cognito_stub.create_user_pool_client.assert_called_once()

    def test_non_dict_payload_rejected(self, cognito_stub):
        with pytest.raises(RegistrationError) as exc:
            register_client(
                ["not", "a", "dict"],
                user_pool_id=_USER_POOL,
                cognito_client=cognito_stub,
            )
        assert exc.value.code == "invalid_client_metadata"

    def test_redirect_uri_not_string_rejected(self, cognito_stub):
        with pytest.raises(RegistrationError) as exc:
            register_client(
                {"redirect_uris": [123]},
                user_pool_id=_USER_POOL,
                cognito_client=cognito_stub,
            )
        assert exc.value.code == "invalid_redirect_uri"
