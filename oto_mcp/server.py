"""FastMCP server exposing oto-cli connectors as MCP tools.

Transports:
- `stdio` (default): for local Claude Desktop / Claude Code, no auth needed.
- `streamable_http`: remote transport for Claude.ai Integrations and other
  HTTP-based clients, gated by OAuth 2.1 with a shared password (see oauth.py
  + login_route.py).

Stateless wrappers around oto-cli Python clients — no per-user state. New
connectors are added by extending `oto_mcp/tools.py`.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastmcp import FastMCP
from mcp.server.auth.settings import ClientRegistrationOptions
from starlette.routing import Route

from .config import require_env
from .login_route import login_get, login_post
from .oauth import PasswordOAuthProvider
from .tools import register_tools

logger = logging.getLogger("oto_mcp")

_pending_provider: dict = {}  # passes the provider instance to Starlette state


def _build_mcp(transport: str) -> FastMCP:
    kwargs: dict = {}
    if transport in ("http", "streamable_http"):
        public_base = require_env("OTO_MCP_PUBLIC_URL").rstrip("/")
        password = require_env("OTO_MCP_OAUTH_PASSWORD")
        provider = PasswordOAuthProvider(
            password=password,
            base_url=public_base,
            resource_base_url=public_base,
            client_registration_options=ClientRegistrationOptions(enabled=True),
        )
        kwargs["auth"] = provider
        _pending_provider["instance"] = provider
    instance = FastMCP("oto", **kwargs)
    register_tools(instance)
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
        app = mcp.http_app()
        app.state.oauth_provider = _pending_provider["instance"]
        app.router.routes.insert(0, Route("/login", login_get, methods=["GET"]))
        app.router.routes.insert(1, Route("/login", login_post, methods=["POST"]))

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
