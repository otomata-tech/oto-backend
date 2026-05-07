"""OAuth 2.1 provider with a single shared password gate.

Flow (Claude.ai Integrations-compatible):
1. Claude.ai calls POST /register (Dynamic Client Registration, RFC 7591).
   The InMemoryOAuthProvider auto-accepts and returns client_id/client_secret.
2. Claude.ai opens a browser on GET /authorize?... (standard OAuth endpoint).
3. Our overridden `authorize()` does NOT immediately issue a code. Instead it
   stores the original params under a random nonce and redirects to /login.
4. User sees a password form at GET /login?nonce=…
5. POST /login with correct password → we complete the original authorization,
   issuing a code and redirecting to Claude.ai's redirect_uri.
6. Claude.ai exchanges the code at POST /token for an access token (+ refresh).

Single-user: one shared password, set via env OTO_MCP_OAUTH_PASSWORD.
The access token issued is a random opaque string managed by the in-memory
provider; it has no oto secrets inside.
"""
from __future__ import annotations

import hmac
import logging
import secrets
import time
from typing import Optional

from fastmcp.server.auth.providers.in_memory import (
    AuthorizationCode,
    AuthorizationParams,
    InMemoryOAuthProvider,
    OAuthClientInformationFull,
    construct_redirect_uri,
)
from pydantic import AnyHttpUrl

logger = logging.getLogger("oto_mcp.oauth")


# Nonces expire quickly — they only cover the time between /authorize and /login.
NONCE_TTL_SECONDS = 600


class PasswordOAuthProvider(InMemoryOAuthProvider):
    """InMemory OAuth provider gated by a single shared password."""

    def __init__(self, password: str, **kwargs):
        super().__init__(**kwargs)
        if not password:
            raise ValueError("PasswordOAuthProvider requires a non-empty password.")
        self._password = password
        # nonce -> (client, params, expires_at)
        self._pending: dict[
            str, tuple[OAuthClientInformationFull, AuthorizationParams, float]
        ] = {}

    def check_password(self, submitted: str) -> bool:
        # constant-time compare
        return hmac.compare_digest(
            self._password.encode(), (submitted or "").encode()
        )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Do not auto-issue the code: bounce the user to /login."""
        nonce = secrets.token_urlsafe(32)
        self._pending[nonce] = (client, params, time.time() + NONCE_TTL_SECONDS)
        # Clean up old nonces opportunistically
        now = time.time()
        self._pending = {
            k: v for k, v in self._pending.items() if v[2] > now
        }
        # Relative redirect — the MCP handler will resolve against base_url.
        return f"{str(self.base_url).rstrip('/')}/login?nonce={nonce}"

    # Called by our /login POST handler after the password check passes.
    async def complete_login(self, nonce: str) -> Optional[str]:
        """Consume a nonce, issue an auth code, return the final redirect URL.

        Returns None if the nonce is invalid/expired.
        """
        entry = self._pending.pop(nonce, None)
        if not entry:
            return None
        client, params, expires_at = entry
        if expires_at < time.time():
            return None

        # Mirror InMemoryOAuthProvider.authorize()'s code-issuance logic.
        code_str = secrets.token_urlsafe(32)
        auth_code = AuthorizationCode(
            code=code_str,
            scopes=params.scopes or [],
            expires_at=time.time() + 300,  # 5 min
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        self.auth_codes[code_str] = auth_code
        logger.info("OAuth login OK — issued code to client=%s", client.client_id)
        return construct_redirect_uri(
            str(params.redirect_uri), code=code_str, state=params.state
        )

    def get_pending(self, nonce: str) -> Optional[tuple]:
        entry = self._pending.get(nonce)
        if not entry:
            return None
        if entry[2] < time.time():
            self._pending.pop(nonce, None)
            return None
        return entry
