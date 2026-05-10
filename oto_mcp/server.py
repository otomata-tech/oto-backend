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

L'API REST `/api/*` (consommée par le frontend oto.ninja pour la page de
gestion de compte) partage le même JWTVerifier que `/mcp`.
"""
from __future__ import annotations

import logging
import os

from fastmcp import FastMCP
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import AnyHttpUrl

from . import access, api_routes, db
from .config import require_env
from .tools import register_all

logger = logging.getLogger("oto_mcp")


def _build_verifier() -> JWTVerifier:
    """JWT verifier partagé entre l'auth MCP et l'API REST."""
    logto_endpoint = require_env("LOGTO_ENDPOINT").rstrip("/")
    audience = require_env("MCP_AUDIENCE")
    issuer = f"{logto_endpoint}/oidc"
    return JWTVerifier(
        jwks_uri=f"{issuer}/jwks",
        issuer=issuer,
        audience=audience,
        # Logto self-hosted signs avec ES384 par défaut (vérifié sur /oidc/jwks).
        algorithm="ES384",
    )


def _build_auth(verifier: JWTVerifier) -> RemoteAuthProvider:
    """Advertise Logto comme AS, valider les JWTs avec le verifier partagé."""
    public_base = require_env("OTO_MCP_PUBLIC_URL").rstrip("/")
    logto_endpoint = require_env("LOGTO_ENDPOINT").rstrip("/")
    issuer = f"{logto_endpoint}/oidc"
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[AnyHttpUrl(issuer)],
        base_url=public_base,
        resource_name="oto MCP",
    )


def _bootstrap_env_keys() -> None:
    """Importe les `<PROVIDER>_API_KEY` env en `platform_keys` (label "env")
    et grant à `OTO_MCP_ADMIN_SUB`. Idempotent — ne casse rien si une clé
    manque (on log et on passe). Permet à la prod existante de continuer à
    marcher sans intervention manuelle après la migration des grants.
    """
    try:
        from oto.config import get_secret
    except Exception as e:
        logger.warning("oto.config indisponible — bootstrap env keys skippé: %s", e)
        return
    env_keys: dict[str, str] = {}
    for provider in db.KEY_PROVIDERS:
        try:
            v = get_secret(f"{provider.upper()}_API_KEY")
        except Exception:
            v = None
        if v:
            env_keys[provider] = v
    if env_keys:
        access.bootstrap_env_keys(env_keys)
        logger.info("Bootstrap env keys importées : %s", sorted(env_keys.keys()))


def _build_mcp(transport: str, verifier: JWTVerifier | None = None) -> FastMCP:
    # init_db idempotent — utile pour que les tables existent avant que
    # le middleware (per-user disabled_tools) ne les interroge.
    try:
        db.init_db()
    except Exception as e:
        logger.warning("init_db at _build_mcp failed: %s", e)

    kwargs: dict = {}
    if transport in ("http", "streamable_http") and verifier is not None:
        kwargs["auth"] = _build_auth(verifier)
    instance = FastMCP("oto", **kwargs)
    register_all(instance)

    # Filtrage per-user des tools (toggle individuel sur /account).
    from .middleware import UserDisabledToolsMiddleware
    instance.add_middleware(UserDisabledToolsMiddleware())

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
    if transport == "stdio":
        mcp.run(transport="stdio")
        return

    if transport in ("http", "streamable_http"):
        host = os.environ.get("HOST", "127.0.0.1")
        port = int(os.environ.get("PORT", "9103"))

        verifier = _build_verifier()
        mcp = _build_mcp(transport, verifier)

        db.init_db()
        _bootstrap_env_keys()
        app = mcp.http_app()
        # API REST consommée par oto.ninja (page de gestion de compte).
        # Insérée avant les routes FastMCP pour qu'elles matchent /api/* en priorité.
        for route in reversed(api_routes.make_routes(verifier, mcp_instance=mcp)):
            app.router.routes.insert(0, route)

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
