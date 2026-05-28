"""Dynamic Client Registration (RFC 7591) proxy for Cognito.

Magic Monitor's HTTPS MCP transport speaks OAuth 2.1 for auth. The MCP
Authorization spec requires the server to support Dynamic Client
Registration so each Claude client (desktop, mobile, third-party) can
self-register without manual user-pool-client setup per client.

Cognito doesn't expose a DCR endpoint natively — but it does expose
CreateUserPoolClient via the API, which is the same operation under
the hood. This module is the translation layer: validate the RFC 7591
payload, call CreateUserPoolClient with the right knobs (public
client, PKCE, callback URL allowlist), and reshape the response back
into the RFC 7591 client information response shape.

**1:1 mapping, no mapping table.** Each `/register` call creates one
new UserPoolClient → one Cognito-assigned `client_id`. That `client_id`
IS the DCR `client_id` we return. No external DB needed. If the same
Claude client re-registers it gets a new `client_id`; the abandoned
ones persist in the pool until cleanup (deferred — Cognito's
1000-client limit isn't a real risk at 3 users and occasional
reinstalls).

**Allowlist enforcement is at JWT-verify time, not here.** Anyone can
hit `/register` and get a Cognito client_id. The hard cap on who can
actually use the MCP tools is the sub-allowlist in `jwt_verifier.py`
— a stranger registering a client gets a client_id but cannot
authenticate as one of the allowed family subs.

**No /authorize, /token proxying.** The OAuth metadata document points
clients directly at Cognito's hosted UI for authorize/token. DCR is
the only RFC 7591/8414 endpoint we proxy; the rest is Cognito-native.

**Unwired in session 2A.** This module exists but nothing in
`mcp/server_http.py` imports it yet. The `/register` route lands in
session 2B alongside the metadata endpoints and the middleware swap.
"""

from typing import Any


_REQUIRED_FIELDS = ("redirect_uris",)

# Cognito's ClientName max length per the AWS API docs.
_COGNITO_CLIENT_NAME_MAX = 128


class RegistrationError(Exception):
    """Maps to RFC 7591 §3.2.2 client registration error responses.

    The `code` field is one of the spec-defined error codes
    (`invalid_client_metadata`, `invalid_redirect_uri`, etc.); the
    `description` is the human-readable detail that gets echoed in
    the `error_description` field of the 400 response.
    """

    def __init__(self, code: str, description: str):
        self.code = code
        self.description = description
        super().__init__(f"{code}: {description}")


def register_client(
    payload: Any,
    *,
    user_pool_id: str,
    cognito_client: Any = None,
    supported_idp_names: tuple[str, ...] = ("Google",),
    allowed_oauth_scopes: tuple[str, ...] = ("openid", "email", "profile"),
) -> dict[str, Any]:
    """Validate a DCR payload, create a Cognito UserPoolClient, return RFC 7591 response.

    Args:
        payload: Parsed JSON body from the `/register` request.
        user_pool_id: Cognito user pool ID (e.g. `us-east-2_ORhu761AY`).
        cognito_client: Optional injected boto3 client (for tests).
            Production callers pass None and we build the default.
        supported_idp_names: Which federated IdPs in the pool to bind
            to the new app client. Default `("Google",)` matches the
            pool's current federation setup.
        allowed_oauth_scopes: OAuth scopes the new client may request.

    Raises:
        RegistrationError: Invalid payload — caller maps to HTTP 400
            with the RFC 7591 error shape.

    Returns:
        Dict in RFC 7591 §3.2.1 client information response format.
    """
    _validate(payload)

    client_name = (payload.get("client_name") or "mcp-dcr-client").strip()
    if not client_name:
        client_name = "mcp-dcr-client"
    redirect_uris = payload["redirect_uris"]

    import boto3
    cognito = cognito_client or boto3.client("cognito-idp")
    # GenerateSecret=False = Cognito's equivalent of
    # `token_endpoint_auth_method=none` (RFC 7591 PKCE public client).
    resp = cognito.create_user_pool_client(
        UserPoolId=user_pool_id,
        ClientName=client_name[:_COGNITO_CLIENT_NAME_MAX],
        GenerateSecret=False,
        AllowedOAuthFlows=["code"],
        AllowedOAuthFlowsUserPoolClient=True,
        AllowedOAuthScopes=list(allowed_oauth_scopes),
        CallbackURLs=list(redirect_uris),
        SupportedIdentityProviders=list(supported_idp_names),
        ExplicitAuthFlows=["ALLOW_REFRESH_TOKEN_AUTH"],
    )

    cog_client = resp["UserPoolClient"]
    issued_at = 0
    creation = cog_client.get("CreationDate")
    if creation is not None:
        issued_at = int(creation.timestamp())

    return {
        "client_id": cog_client["ClientId"],
        "client_id_issued_at": issued_at,
        "client_name": cog_client.get("ClientName", client_name),
        "redirect_uris": cog_client.get("CallbackURLs", list(redirect_uris)),
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": " ".join(allowed_oauth_scopes),
    }


def _validate(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise RegistrationError(
            "invalid_client_metadata",
            "request body must be a JSON object",
        )
    for field in _REQUIRED_FIELDS:
        if field not in payload:
            raise RegistrationError(
                "invalid_client_metadata",
                f"missing required field: {field}",
            )
    redirect_uris = payload["redirect_uris"]
    if not isinstance(redirect_uris, list) or not redirect_uris:
        raise RegistrationError(
            "invalid_redirect_uri",
            "redirect_uris must be a non-empty list",
        )
    for uri in redirect_uris:
        if not _is_acceptable_redirect_uri(uri):
            raise RegistrationError(
                "invalid_redirect_uri",
                f"redirect_uri must be https or localhost http: {uri!r}",
            )


def _is_acceptable_redirect_uri(uri: Any) -> bool:
    """Accept https://* and http://localhost / http://127.0.0.1 only.

    Localhost HTTP is allowed because desktop OAuth clients commonly
    use the loopback redirect pattern (RFC 8252 §7.3). Every other
    http:// scheme is rejected — there's no legitimate reason a remote
    client would callback over cleartext.
    """
    if not isinstance(uri, str):
        return False
    if uri.startswith("https://"):
        return True
    if uri.startswith("http://localhost") or uri.startswith("http://127.0.0.1"):
        return True
    return False
