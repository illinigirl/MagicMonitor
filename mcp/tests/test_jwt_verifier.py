"""Tests for mcp/jwt_verifier.py.

Generates a real RSA keypair in-process so the verify path runs
end-to-end (parse → JWKS lookup → signature verify → claim checks)
without any HTTP fetch. The keypair lives only in the test process;
nothing is written to disk.
"""

import json
import time

import jwt
import jwt.algorithms
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import jwt_verifier
from jwt_verifier import VerifierConfig, VerifyError, verify_token


_ISSUER = "https://cognito-idp.us-east-2.amazonaws.com/us-east-2_TESTPOOL"
_ALLOWED_SUB = "11111111-1111-1111-1111-111111111111"
_KID = "test-kid"


@pytest.fixture
def keypair():
    """Fresh RSA keypair per test — cheap enough not to share across tests."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


@pytest.fixture
def jwk_set(keypair):
    """JWKS containing the test public key."""
    _, public = keypair
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public))
    jwk["kid"] = _KID
    jwk["alg"] = "RS256"
    jwk["use"] = "sig"
    return {"keys": [jwk]}


@pytest.fixture
def jwks_loader(jwk_set):
    def _loader(url):
        return jwk_set
    return _loader


@pytest.fixture
def config():
    return VerifierConfig(
        issuer=_ISSUER,
        allowed_subs=frozenset({_ALLOWED_SUB}),
        jwks_url=f"{_ISSUER}/.well-known/jwks.json",
    )


def _make_token(
    keypair,
    *,
    sub=_ALLOWED_SUB,
    token_use="access",
    iss=_ISSUER,
    exp_offset=300,
    kid=_KID,
    include_kid=True,
):
    private, _ = keypair
    now = int(time.time())
    claims = {
        "sub": sub,
        "iss": iss,
        "token_use": token_use,
        "iat": now,
        "exp": now + exp_offset,
        "client_id": "test-client",
    }
    headers = {"kid": kid} if include_kid else {}
    return jwt.encode(claims, private, algorithm="RS256", headers=headers)


@pytest.fixture(autouse=True)
def clear_jwks_cache():
    """Module-level JWKS cache leaks between tests if not cleared."""
    jwt_verifier._jwks_cache.clear()
    yield
    jwt_verifier._jwks_cache.clear()


class TestHappyPath:
    def test_valid_token_returns_claims(self, keypair, config, jwks_loader):
        token = _make_token(keypair)
        claims = verify_token(token, config=config, jwks_loader=jwks_loader)
        assert claims["sub"] == _ALLOWED_SUB
        assert claims["token_use"] == "access"
        assert claims["iss"] == _ISSUER

    def test_cache_hit_skips_second_loader_call(self, keypair, config, jwk_set):
        calls = {"n": 0}

        def loader(url):
            calls["n"] += 1
            return jwk_set

        token1 = _make_token(keypair)
        token2 = _make_token(keypair)
        # Prime the cache by calling the real default loader path. We
        # do that by stuffing the cache manually — the loader fixture
        # never goes through _default_jwks_loader.
        jwt_verifier._jwks_cache[config.jwks_url] = jwk_set
        verify_token(token1, config=config, jwks_loader=loader)
        verify_token(token2, config=config, jwks_loader=loader)
        # Two verifies; loader still called because we inject it
        # directly (the cache only short-circuits _default_jwks_loader).
        # This test documents the contract: injected loaders are always
        # called; the module cache is only consulted by the default.
        assert calls["n"] == 2


class TestSignatureFailures:
    def test_wrong_signing_key_rejected(self, config, jwks_loader):
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = int(time.time())
        bad_token = jwt.encode(
            {
                "sub": _ALLOWED_SUB,
                "iss": _ISSUER,
                "token_use": "access",
                "iat": now,
                "exp": now + 300,
            },
            other_key,
            algorithm="RS256",
            headers={"kid": _KID},
        )
        with pytest.raises(VerifyError):
            verify_token(bad_token, config=config, jwks_loader=jwks_loader)

    def test_unknown_kid_rejected(self, keypair, config, jwks_loader):
        token = _make_token(keypair, kid="unknown-kid")
        with pytest.raises(VerifyError, match="no JWK matching kid"):
            verify_token(token, config=config, jwks_loader=jwks_loader)


class TestClaimFailures:
    def test_expired_token_rejected(self, keypair, config, jwks_loader):
        token = _make_token(keypair, exp_offset=-10)
        with pytest.raises(VerifyError, match="expired"):
            verify_token(token, config=config, jwks_loader=jwks_loader)

    def test_wrong_issuer_rejected(self, keypair, config, jwks_loader):
        token = _make_token(keypair, iss="https://evil.example.com")
        with pytest.raises(VerifyError, match="issuer"):
            verify_token(token, config=config, jwks_loader=jwks_loader)

    def test_id_token_rejected(self, keypair, config, jwks_loader):
        """Only access tokens authorize MCP calls — id tokens carry the
        wrong audience semantics and must be rejected even when
        signature + sub are valid."""
        token = _make_token(keypair, token_use="id")
        with pytest.raises(VerifyError, match="token_use"):
            verify_token(token, config=config, jwks_loader=jwks_loader)


class TestAllowlist:
    def test_sub_not_in_allowlist_rejected(self, keypair, config, jwks_loader):
        token = _make_token(keypair, sub="22222222-2222-2222-2222-222222222222")
        with pytest.raises(VerifyError, match="not in allowlist"):
            verify_token(token, config=config, jwks_loader=jwks_loader)

    def test_empty_allowlist_rejects_everyone(self, keypair, jwks_loader):
        """Empty allowlist means deny — never accidentally serve every
        Cognito user just because the env var didn't get set."""
        cfg = VerifierConfig(
            issuer=_ISSUER,
            allowed_subs=frozenset(),
            jwks_url=f"{_ISSUER}/.well-known/jwks.json",
        )
        token = _make_token(keypair)
        with pytest.raises(VerifyError, match="not in allowlist"):
            verify_token(token, config=cfg, jwks_loader=jwks_loader)


class TestMalformedInput:
    def test_garbage_token_rejected(self, config, jwks_loader):
        with pytest.raises(VerifyError):
            verify_token("not-a-jwt", config=config, jwks_loader=jwks_loader)

    def test_missing_kid_header_rejected(self, keypair, config, jwks_loader):
        token = _make_token(keypair, include_kid=False)
        with pytest.raises(VerifyError, match="kid"):
            verify_token(token, config=config, jwks_loader=jwks_loader)


class TestJWKSRotation:
    def test_kid_miss_triggers_one_refresh(self, keypair, config):
        """First loader call returns a JWKS missing our kid; second
        call (post-eviction) returns the right one. Verifier should
        recover, not fail."""
        _, public = keypair
        right_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public))
        right_jwk["kid"] = _KID
        right_jwk["alg"] = "RS256"

        stale = {
            "keys": [{"kid": "stale-kid", "kty": "RSA", "n": "x", "e": "AQAB",
                      "alg": "RS256"}]
        }
        fresh = {"keys": [right_jwk]}

        calls = {"n": 0}

        def loader(url):
            calls["n"] += 1
            return stale if calls["n"] == 1 else fresh

        token = _make_token(keypair)
        claims = verify_token(token, config=config, jwks_loader=loader)
        assert claims["sub"] == _ALLOWED_SUB
        assert calls["n"] == 2  # cold load + refresh on kid miss

    def test_kid_miss_then_still_missing_fails(self, keypair, config):
        """If both loader calls return the wrong JWKS, give up — no
        infinite-loop refreshes hammering Cognito."""
        stale = {
            "keys": [{"kid": "stale-kid", "kty": "RSA", "n": "x", "e": "AQAB",
                      "alg": "RS256"}]
        }
        calls = {"n": 0}

        def loader(url):
            calls["n"] += 1
            return stale

        token = _make_token(keypair)
        with pytest.raises(VerifyError, match="no JWK matching kid"):
            verify_token(token, config=config, jwks_loader=loader)
        assert calls["n"] == 2
