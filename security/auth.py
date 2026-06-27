"""Shared MCP authentication for mcp-tools.

Every public-facing tool server reuses :func:`build_oauth_provider` so that
Claude (desktop / web / mobile) gets a spec-compliant OAuth 2.1 + PKCE flow with
the correct discovery metadata and ``WWW-Authenticate`` header. This is the piece
that lets a self-hosted server work with the claude.ai web/mobile custom
connector (which a bare Cloudflare Access portal does not -- see docs/SETUP.md).

Identity is Google, gated by a verified-email allowlist (fail closed). FastMCP's
``GoogleProvider`` on its own authenticates *any* Google account, which would
expose this server's upstream credentials to the entire internet.
:class:`GoogleAllowlistProvider` wraps the provider's configured token verifier
and rejects any login whose verified email is not in ``MCP_ALLOWED_GOOGLE_EMAILS``.

Note Google also gives a *second*, native gate: while the Google Cloud OAuth
consent screen is in "Testing" status, only emails added as test users can
complete the upstream login at all -- before this allowlist even runs.
"""

from __future__ import annotations

import logging
import os

from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.google import GoogleProvider

LOGGER = logging.getLogger("mcp_tools.auth")

# Must match the Google OAuth client's "Authorized redirect URI" exactly:
#   <MCP_PUBLIC_URL>/auth/callback
REDIRECT_PATH = "/auth/callback"

# openid + email so the verifier always receives a verified email to gate on.
REQUIRED_SCOPES = ["openid", "email"]


def _is_truthy(value: str | None) -> bool:
    return bool(value) and value.strip().lower() in {"1", "true", "yes", "on"}


def _csv_set(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def _email_is_verified(claim: object) -> bool:
    # Google returns this as bool True or the string "true" depending on endpoint.
    if claim is None:
        return True  # absence => don't second-guess a successfully verified token
    return str(claim).strip().lower() in {"true", "1"}


class GoogleAllowlistProvider(GoogleProvider):
    """GoogleProvider that only accepts an explicit set of verified emails."""

    def __init__(self, *, allowed_emails: set[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._allowed_emails = {e.lower() for e in allowed_emails}

        # The provider's token verifier (configured by OAuthProxy after init) lives
        # at `_token_validator`. Wrap its verify_token so the allowlist runs after a
        # successful Google verification, preserving all of its other config.
        base_verify = self._token_validator.verify_token
        allowed = self._allowed_emails

        async def verify_with_allowlist(token: str) -> AccessToken | None:
            result = await base_verify(token)
            if result is None:
                return None
            claims = result.claims or {}
            email = (claims.get("email") or "").lower()
            if not email or email not in allowed:
                LOGGER.warning("Rejected Google login %r: not in allowlist", email or None)
                return None
            if not _email_is_verified(claims.get("email_verified")):
                LOGGER.warning("Rejected Google login %r: email not verified", email)
                return None
            return result

        self._token_validator.verify_token = verify_with_allowlist  # type: ignore[method-assign]


def build_oauth_provider() -> GoogleProvider | None:
    """Return a configured provider for public serving, or ``None`` when disabled.

    ``MCP_AUTH_ENABLED`` off -> loopback / trusted-consumer mode (e.g. a local
    agent on 127.0.0.1); the server is a plain unauthenticated MCP endpoint. On ->
    require Google OAuth. Every credential needed for public serving is mandatory;
    any missing piece raises so we never start a public server wide open.
    """
    if not _is_truthy(os.getenv("MCP_AUTH_ENABLED")):
        LOGGER.info("MCP_AUTH_ENABLED off -- serving without auth (loopback mode).")
        return None

    try:
        client_id = os.environ["GOOGLE_CLIENT_ID"]
        client_secret = os.environ["GOOGLE_CLIENT_SECRET"]
        base_url = os.environ["MCP_PUBLIC_URL"]
    except KeyError as missing:
        raise RuntimeError(
            f"MCP_AUTH_ENABLED is on but {missing} is not set. Refusing to start a "
            "public server without OAuth configured."
        ) from None

    allowed = _csv_set(os.getenv("MCP_ALLOWED_GOOGLE_EMAILS"))
    if not allowed:
        raise RuntimeError(
            "MCP_AUTH_ENABLED is on but MCP_ALLOWED_GOOGLE_EMAILS is empty. Refusing "
            "to start: any Google user could otherwise use this server."
        )

    LOGGER.info(
        "OAuth enabled (Google) at %s; allowed emails: %s",
        base_url,
        ", ".join(sorted(allowed)),
    )
    return GoogleAllowlistProvider(
        allowed_emails=allowed,
        client_id=client_id,
        client_secret=client_secret,
        base_url=base_url,
        issuer_url=base_url,
        redirect_path=REDIRECT_PATH,
        required_scopes=REQUIRED_SCOPES,
    )
