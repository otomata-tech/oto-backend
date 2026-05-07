"""FastMCP server exposing oto-cli connectors as MCP tools.

Transports:
- `stdio` (default): for local Claude Desktop / Claude Code, no auth needed.
- `streamable_http`: remote transport for Claude.ai Integrations and other
  HTTP-based clients, gated by Logto-issued JWT bearer tokens (RFC 9728
  protected resource metadata advertises the auth server back to the client).

Wrappers autour des clients oto-cli. État par utilisateur stocké dans la
SQLite locale (cf. `db.py`) — aujourd'hui le cookie LinkedIn. Nouveaux
connecteurs : ajouter un module dans `oto_mcp/tools/` puis l'enregistrer
dans `tools/__init__.py`.
"""
from __future__ import annotations

import logging
import os

from fastmcp import FastMCP
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import AnyHttpUrl
from starlette.routing import Route

from . import db
from .config import require_env
from .settings_routes import (
    settings_get,
    settings_linkedin_clear_post,
    settings_linkedin_post,
)
from .tools import register_all

logger = logging.getLogger("oto_mcp")


def _build_auth() -> RemoteAuthProvider:
    """Validate JWTs issued by Logto, advertise it as the auth server.

    Audience = the MCP resource indicator (RFC 8707). Must match the
    `indicator` of the Logto API resource Claude requests when getting a
    token. Issuer = `<endpoint>/oidc` per Logto's OIDC discovery.
    """
    public_base = require_env("OTO_MCP_PUBLIC_URL").rstrip("/")
    logto_endpoint = require_env("LOGTO_ENDPOINT").rstrip("/")
    audience = require_env("MCP_AUDIENCE")

    issuer = f"{logto_endpoint}/oidc"
    verifier = JWTVerifier(
        jwks_uri=f"{issuer}/jwks",
        issuer=issuer,
        audience=audience,
        algorithm="RS256",
    )
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[AnyHttpUrl(issuer)],
        base_url=public_base,
        resource_name="oto MCP",
    )


def _build_mcp(transport: str) -> FastMCP:
    kwargs: dict = {}
    if transport in ("http", "streamable_http"):
        kwargs["auth"] = _build_auth()
    instance = FastMCP("oto", **kwargs)
    register_all(instance)
    return instance


# Always-available module-level instance for stdio transport + testing imports.
mcp = _build_mcp("stdio")


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    global mcp
    if transport != "stdio":
        mcp = _build_mcp(transport)

    if transport == "stdio":
        mcp.run(transport="stdio")
        return

    if transport in ("http", "streamable_http"):
        host = os.environ.get("HOST", "127.0.0.1")
        port = int(os.environ.get("PORT", "9103"))
        db.init_db()
        app = mcp.http_app()
        # User-facing settings UI (LinkedIn cookie). Auth interne
        # `current_user_sub_from_request` — sera câblée à Logto.
        app.router.routes.insert(0, Route("/settings", settings_get, methods=["GET"]))
        app.router.routes.insert(1, Route("/settings/linkedin", settings_linkedin_post, methods=["POST"]))
        app.router.routes.insert(2, Route("/settings/linkedin/clear", settings_linkedin_clear_post, methods=["POST"]))

        import uvicorn
        logger.info("HTTP MCP server on %s:%d", host, port)
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        )
        return

    raise ValueError(f"Unknown MCP_TRANSPORT={transport!r}")


if __name__ == "__main__":
    main()
