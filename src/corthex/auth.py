"""Authentication primitives for the private Corthex HTTP endpoint."""

from __future__ import annotations

import hmac

from mcp.server.auth.provider import AccessToken


class StaticTokenVerifier:
    """Verify one deployment token without storing a derivative in responses."""

    def __init__(self, token: str, *, scopes: list[str] | None = None) -> None:
        if not token:
            raise ValueError("A non-empty token is required")
        self._token = token
        self._scopes = list(scopes or ["corthex:memory"])

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or not hmac.compare_digest(token, self._token):
            return None
        return AccessToken(
            token=token,
            client_id="corthex-static-client",
            scopes=self._scopes,
        )
