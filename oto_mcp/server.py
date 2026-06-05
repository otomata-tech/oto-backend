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

from . import access, api_routes, connectors, db
from .config import require_env
from .tools import register_all

logger = logging.getLogger("oto_mcp")


class _IatGatedVerifier(JWTVerifier):
    """JWTVerifier qui rejette en plus tout token avec `iat < MIN_TOKEN_IAT`.

    Permet une déco globale "soft" : bumper l'env `MIN_TOKEN_IAT` à `now()`
    invalide tous les tokens en cours sans toucher à Logto. Les clients
    reçoivent un 401 + WWW-Authenticate et re-lancent l'OAuth dance.
    """

    def __init__(self, *args, min_iat: int = 0, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._min_iat = min_iat

    async def verify_token(self, token):
        result = await super().verify_token(token)
        if result and getattr(result, "claims", None) and self._min_iat > 0:
            iat = result.claims.get("iat") or 0
            if iat < self._min_iat:
                logger.info(f"iat-gate reject sub={result.claims.get('sub')} iat={iat} < min_iat={self._min_iat}")
                return None
        return result


def _build_verifier() -> JWTVerifier:
    """JWT verifier partagé entre l'auth MCP et l'API REST."""
    logto_endpoint = require_env("LOGTO_ENDPOINT").rstrip("/")
    audience = require_env("MCP_AUDIENCE")
    issuer = f"{logto_endpoint}/oidc"
    min_iat = int(os.environ.get("MIN_TOKEN_IAT", "0") or "0")
    return _IatGatedVerifier(
        jwks_uri=f"{issuer}/jwks",
        issuer=issuer,
        audience=audience,
        # Logto self-hosted signs avec ES384 par défaut (vérifié sur /oidc/jwks).
        algorithm="ES384",
        min_iat=min_iat,
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
    # Toutes les clés env sont importées en `platform_keys` (label "env") pour
    # que l'admin PUISSE les grant au cas par cas (modèle serper : user key OU
    # platform key + quota). Importer ≠ partager : une clé plateforme n'est
    # accessible qu'avec un grant explicite (cf. access.resolve_api_key).
    # La plupart des providers exposent `<PROVIDER>_API_KEY` ; slack n'a pas de
    # clé unique mais un user token (`SLACK_USER_TOKEN`, xoxp) — c'est lui qu'on
    # importe comme clé plateforme (mode `as_user` côté tool).
    env_keys: dict[str, str] = {}
    for provider in db.KEY_PROVIDERS:
        secret_name = connectors.ENV_SECRET_NAMES.get(provider, f"{provider.upper()}_API_KEY")
        try:
            v = get_secret(secret_name)
        except Exception:
            v = None
        if v:
            env_keys[provider] = v
    if env_keys:
        access.bootstrap_env_keys(env_keys)
        logger.info("Bootstrap env keys importées : %s", sorted(env_keys.keys()))


_SERVER_INSTRUCTIONS = """\
Oto — toolkit d'automatisation pour la prospection B2B et l'intelligence commerciale.

Namespaces :
• fr_* — données entreprise France (open data + INSEE). fr_get = fiche complète agrégée (identité + bilan INPI + événements BODACC). fr_search = recherche multicritère.
• linkedin_* — scraping LinkedIn via browser persistant. Cookie requis (oto.ninja/account).
• attio_* — CRM Attio complet (companies, people, deals, notes, tasks, lists, entries, threads).
• serper_* — recherche web (Serper API) : web, news, scrape.
• hunter_* — emails : domain search, finder, vérification.
• kaspr_* — enrichissement contacts depuis profil LinkedIn.
• fullenrich_* — enrichissement contacts waterfall multi-provider (phones ~70% hit).
• lemlist_* — campagnes cold outreach.
• crunchbase_* — données startups, levées de fonds.
• reddit_* — recherche et posts Reddit.
• slack_* — messagerie Slack.
• whatsapp_* — messagerie WhatsApp (pairing QR requis).
• data_* — datastore tabulaire per-user (backend Google Sheets).
• gmail_* — Gmail per-user, multi-compte (search, get, send, reply, draft, archive, trash). OAuth Google requis (app.oto.ninja). gmail_list_accounts liste les comptes ; param `account` (email) pour cibler un compte précis.
• culture_spectacle_* — entreprises du spectacle vivant.
• oto_* — méta-tools : list/enable/disable tools, presets nommés.

Configuration compte : https://oto.ninja/account (cookie LinkedIn, clés API, presets de toolset).\
"""


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
    instance = FastMCP("oto", instructions=_SERVER_INSTRUCTIONS, **kwargs)
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
