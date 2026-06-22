"""FastMCP server exposing oto-cli connectors as MCP tools.

Transport : `streamable_http` uniquement (défaut) — gated par JWT Logto
(RFC 9728 : la metadata de ressource protégée annonce l'auth server au client).
Le transport `stdio` (local, sans auth) a été **retiré** le 2026-06-13 : oto-mcp
est toujours authentifié → la couche capacité (ADR 0009) suppose un sub résolu.
L'usage local passe par la CLI `oto`.

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

from . import api_routes, db
from .config import require_env
from .tools import register_all

logger = logging.getLogger("oto_mcp")


class _IatGatedVerifier(JWTVerifier):
    """JWTVerifier qui rejette en plus tout token avec `iat < MIN_TOKEN_IAT`.

    Permet une déco globale "soft" : bumper l'env `MIN_TOKEN_IAT` à `now()`
    invalide tous les tokens en cours sans toucher à Logto. Les clients
    reçoivent un 401 + WWW-Authenticate et re-lancent l'OAuth dance.
    """

    def __init__(self, *args, min_iat: int = 0, fallback: "JWTVerifier | None" = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._min_iat = min_iat
        # Fenêtre de bascule tenant (A2/B1) : accepte aussi les tokens d'un 2e
        # issuer (auth.oto.ninja) le temps que tout le monde migre. Non posé =
        # mono-issuer, comportement inchangé.
        self._fallback = fallback

    async def verify_token(self, token):
        result = await super().verify_token(token)
        if not result and self._fallback is not None:
            try:
                result = await self._fallback.verify_token(token)
            except Exception:
                result = None
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
    # Bascule tenant (A2) : `LOGTO_ENDPOINT_ALT` posé → 2e issuer accepté pendant la
    # fenêtre de migration (même audience/indicator sur les 2 tenants). Drain puis on
    # retire l'env. Logto self-hosted signe en ES384 (vérifié sur /oidc/jwks).
    fallback = None
    alt = os.environ.get("LOGTO_ENDPOINT_ALT", "").strip().rstrip("/")
    if alt:
        alt_issuer = f"{alt}/oidc"
        fallback = JWTVerifier(
            jwks_uri=f"{alt_issuer}/jwks", issuer=alt_issuer,
            audience=audience, algorithm="ES384",
        )
    return _IatGatedVerifier(
        jwks_uri=f"{issuer}/jwks",
        issuer=issuer,
        audience=audience,
        algorithm="ES384",
        min_iat=min_iat,
        fallback=fallback,
    )


def _build_auth(verifier: JWTVerifier) -> RemoteAuthProvider:
    """Valider les JWTs Logto + se présenter en **façade AS** (cf. oauth_facade) :
    le PRM pointe les clients vers NOUS (pas Logto direct), pour qu'ils lisent
    notre métadonnée AS augmentée d'un `registration_endpoint` (DCR façade). Sans
    ça, claude.ai ne peut pas s'auto-enregistrer (Logto n'a pas de DCR) et l'user
    doit coller le client_id à la main."""
    public_base = require_env("OTO_MCP_PUBLIC_URL").rstrip("/")
    # Façade DCR active ⟺ OTO_MCP_CLAUDE_APP_ID posé : alors le PRM pointe vers
    # NOUS (on sert l'AS metadata + /oauth/register, cf. oauth_facade + branche
    # HTTP). Sinon, fallback au comportement historique (PRM → Logto direct,
    # client_id collé à la main). Les deux DOIVENT rester couplés sur ce flag,
    # sinon le PRM pointerait vers un AS metadata inexistant → discovery cassée.
    if os.environ.get("OTO_MCP_CLAUDE_APP_ID"):
        authz = public_base
    else:
        authz = f"{require_env('LOGTO_ENDPOINT').rstrip('/')}/oidc"
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[AnyHttpUrl(authz)],
        base_url=public_base,
        resource_name="oto MCP",
    )


# Plus de bootstrap SOPS→platform_keys au boot (oto-mcp#12) : la DB (coffre
# `platform_keys`) est la SEULE source des clés plateforme. Poser/roter une clé =
# surface admin (REST `/api/admin/platform-keys` ou meta-tool MCP), jamais SOPS.
# L'unit pose `OTO_CONFIG_DISABLE_SOPS=1` : tout `require_secret` résiduel côté
# serveur échoue fort au lieu de lire le filesystem.

_SERVER_INSTRUCTIONS = """\
Oto — TA boîte à outils d'automatisation (prospection B2B, données entreprise FR, CRM, email, messagerie, base de connaissance…). Ces outils `*_` AGISSENT sur les comptes et données de l'utilisateur : utilise-les DIRECTEMENT pour répondre. **Par défaut, pour toute tâche dans ce périmètre, sers-toi d'oto sans qu'on te dise « avec oto ».** Oto n'est PAS un sujet à rechercher sur le web — ne lance pas de recherche web pour « découvrir Oto » ; tout ce qu'il faut est ici et dans les outils.

Pour un compte récent ou peu configuré, commence par `oto_onboarding()` — il explique Oto, fait l'état de la configuration du compte (org active, base de connaissance, clés de connecteurs, doctrine) et donne les prochaines étapes de paramétrage à proposer à l'utilisateur.

En début de session, appelle `oto_get_doctrine()` — il renvoie la doctrine de ton organisation (workflows validés, règles métier, vocabulaire) ET l'index de ses doctrines nommées (skills), à charger à la demande via `oto_get_doctrine(slug)` (ou cherche avec `oto_list_doctrines`). Vide si ton org n'en a pas : continue normalement avec ces instructions.

**Encadre et remonte.** Quand tu exécutes une procédure — un workflow doctriné OU un déroulé one-shot qui mérite d'être tracé — ouvre-la par `run_start(label, doctrine?)` (passe `doctrine`=slug pour une doctrine nommée, omets-le pour un run ad-hoc) et ferme-la par `run_finish(run_id, outcome)` (done|abandoned|failed|blocked). **Remonte tout signal d'usage** via `feedback(signal, kind, target, text?)` : `signal='gap'` quand oto ne couvre PAS ce dont tu as besoin (outil, doctrine ou donnée manquants — `target`=ce que tu voulais faire) plutôt que d'abandonner en silence ; `signal='tool_feedback'` quand un outil se comporte mal ou excellemment (`target`=le nom de l'outil). C'est ainsi que la plateforme apprend.

Namespaces :
• fr_* — données entreprise France (open data + INSEE). fr_get = fiche complète agrégée (identité + bilan INPI + événements BODACC). fr_search = recherche multicritère.
• unipile_* — LinkedIn hébergé (Unipile) : profil, entreprise, recherche, messagerie. Requiert un compte LinkedIn connecté par l'user (dashboard.oto.ninja).
• attio_* — CRM Attio complet (companies, people, deals, notes, tasks, lists, entries, threads). Masqué par défaut (préférer le MCP Attio officiel) — réactivable via oto_enable_tool.
• serper_* — recherche web (Serper API) : web, news, scrape.
• hunter_* — emails : domain search, finder, vérification.
• kaspr_* — enrichissement contacts depuis profil LinkedIn.
• fullenrich_* — enrichissement contacts waterfall multi-provider (phones ~70% hit).
• lemlist_* — campagnes cold outreach.
• crunchbase_* — données startups, levées de fonds.
• reddit_* — recherche et posts Reddit.
• slack_* — messagerie Slack.
• whatsapp_* / telegram_* / instagram_* — messagerie hébergée (Unipile) par canal. Chacun requiert que l'user connecte le compte du canal (dashboard.oto.ninja, option messagerie).
• data_* — datastore tabulaire per-user (PG natif, schéma libre ; data_write/data_rows/data_share).
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
    # Lier l'instance pour que les handlers de capacité (doctrine) résolvent le
    # manifeste « referenced_tools » sans se faire passer l'instance.
    from . import tool_registry
    tool_registry.bind(instance)
    register_all(instance)

    # Couche capacité (ADR 0009) : monte un tool par capacité déclarée
    # (no-op tant que le registre est vide — canari). Après register_all pour
    # que le test d'unicité voie les deux mondes (legacy + capacités).
    from .capabilities import _mcp_adapter
    from .capabilities import registry as _cap_registry
    _mcp_adapter.register(instance, _cap_registry.CAPABILITIES)

    # Capture des exceptions de tools vers Sentry (vrai traceback ; no-op si
    # OTO_SENTRY_DSN absent). Les erreurs de tool sont des erreurs JSON-RPC en
    # HTTP 200 → invisibles à l'intégration Starlette ; ce middleware les voit.
    from .sentry_setup import SentryToolErrorMiddleware
    instance.add_middleware(SentryToolErrorMiddleware())

    # Filtrage per-user des tools (toggle individuel sur /account).
    from .middleware import UserDisabledToolsMiddleware
    instance.add_middleware(UserDisabledToolsMiddleware())
    # Journalisation des appels MCP (lib commune otomata-calllog, table tool_calls,
    # lue par /api/admin/monitoring/*). Identité via auth_hooks (auth Logto custom —
    # le get_access_token fastmcp par défaut ne la voit pas).
    import asyncio

    from otomata_calllog import ToolCallLogger

    from .auth_hooks import current_user_sub_from_token

    from . import credits_store

    async def _calllog_sink(row: dict) -> None:
        # Corrélation (ADR 0017) : stampe session_id (session mcp) + run_id (déroulé
        # de doctrine actif) AVANT l'insert. Best-effort — un contexte absent
        # (ex. pas de session) ne casse jamais la journalisation.
        try:
            from fastmcp.server.dependencies import get_context

            from . import doctrine_run

            ctx = get_context()
            row["session_id"] = ctx.session_id
            row["run_id"] = await doctrine_run.active_run_id(ctx)
        except Exception:
            pass
        # to_thread : l'INSERT PG (pool psycopg sync) ne doit pas bloquer
        # l'event loop sur le chemin chaud de chaque tool call.
        await asyncio.to_thread(db.insert_tool_call, row)
        # Débit best-effort du wallet de l'org active du caller (billing par appel).
        # Soft enforcement : ce hook tourne APRÈS l'exécution du tool → ne peut jamais
        # bloquer un appel ; debit_for_call avale toute erreur. No-op sans sub/org active.
        await asyncio.to_thread(credits_store.debit_for_call, row.get("sub"))

    def _calllog_identity() -> dict:
        return {"sub": current_user_sub_from_token()}

    instance.add_middleware(
        ToolCallLogger(_calllog_sink, server="oto", identity=_calllog_identity)
    )

    return instance


# Instance module-level (sans auth) pour les imports de test + l'enregistrement
# des tools. Le serveur réel reconstruit avec le verifier dans main() (http).
mcp = _build_mcp("noauth")


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Error tracking Sentry — AVANT tout build d'app : l'intégration Starlette
    # patche au moment de l'init. No-op si OTO_SENTRY_DSN absent.
    from .sentry_setup import init_sentry
    init_sentry()

    # stdio retiré (2026-06-13) : oto-mcp ne se sert plus qu'en streamable_http
    # (toujours authentifié Logto). Conséquence voulue — plus de chemin local
    # sans login → la couche capacité (ADR 0009) peut supposer un sub résolu,
    # le cas-limite sub=None disparaît. L'usage local passe par la CLI `oto`.
    transport = os.environ.get("MCP_TRANSPORT", "streamable_http")

    global mcp
    if transport in ("http", "streamable_http"):
        host = os.environ.get("HOST", "127.0.0.1")
        port = int(os.environ.get("PORT", "9103"))

        verifier = _build_verifier()
        mcp = _build_mcp(transport, verifier)

        db.init_db()
        app = mcp.http_app()
        # API REST consommée par oto.ninja (page de gestion de compte).
        # Insérée avant les routes FastMCP pour qu'elles matchent /api/* en priorité.
        for route in reversed(api_routes.make_routes(verifier, mcp_instance=mcp)):
            app.router.routes.insert(0, route)

        # Façade DCR (oauth_facade) : sert /.well-known/oauth-authorization-server
        # + /oauth/register pour que claude.ai s'auto-enregistre sans coller le
        # client_id. No-op si OTO_MCP_CLAUDE_APP_ID absent (paste manuel conservé).
        claude_app_id = os.environ.get("OTO_MCP_CLAUDE_APP_ID")
        if claude_app_id:
            from . import oauth_facade
            for route in reversed(oauth_facade.make_routes(
                    require_env("OTO_MCP_PUBLIC_URL"), claude_app_id)):
                app.router.routes.insert(0, route)
            logger.info("DCR facade active (claude app %s)", claude_app_id)

        import uvicorn
        logger.info("HTTP MCP server on %s:%d", host, port)
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        )
        return

    raise ValueError(
        f"MCP_TRANSPORT={transport!r} non supporté. Le transport stdio a été "
        f"retiré (2026-06-13) — seul streamable_http est servi."
    )


if __name__ == "__main__":
    main()
