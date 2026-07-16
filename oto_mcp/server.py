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

import asyncio
import logging
import os

from fastmcp import FastMCP
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import AnyHttpUrl

from . import api_routes, db, instructions
from .config import require_env, mcp_audience_alts
from .tools import register_all

logger = logging.getLogger("oto_mcp")


class _IatGatedVerifier(JWTVerifier):
    """JWTVerifier qui rejette en plus tout token avec `iat < MIN_TOKEN_IAT`.

    Permet une déco globale "soft" : bumper l'env `MIN_TOKEN_IAT` à `now()`
    invalide tous les tokens en cours sans toucher à Logto. Les clients
    reçoivent un 401 + WWW-Authenticate et re-lancent l'OAuth dance.
    """

    def __init__(self, *args, min_iat: int = 0, fallback: "JWTVerifier | None" = None,
                 expected_audience: "str | None" = None,
                 alt_audiences: "frozenset[str]" = frozenset(), **kwargs) -> None:
        # Le parent est construit SANS check d'audience (`audience=None`) : on valide
        # l'audience nous-mêmes (canonique OU sous-domaine org publié, ADR 0032 §barreau 4).
        super().__init__(*args, **kwargs)
        self._min_iat = min_iat
        # Fenêtre de bascule tenant (A2/B1) : accepte aussi les tokens d'un 2e
        # issuer (auth.oto.ninja) le temps que tout le monde migre. Non posé =
        # mono-issuer, comportement inchangé.
        self._fallback = fallback
        self._expected_audience = expected_audience
        # Audiences canoniques SECONDAIRES (coexistence multi-domaine, ex. mcp.oto.cx).
        # Vide = no-op → mcp.oto.ninja inchangé.
        self._alt_audiences = alt_audiences

    def _audience_ok(self, claims) -> bool:
        """Canonique (`MCP_AUDIENCE`, DB-INDÉPENDANT → l'auth canonique ne casse jamais)
        OU resource indicator d'un endpoint org publié (`<slug>.mcp.oto.cx/mcp`, motif +
        existence en DB, fail-closed). Pas d'audience attendue configurée → pas de check."""
        if not self._expected_audience:
            return True
        aud = (claims or {}).get("aud")
        auds = aud if isinstance(aud, list) else [aud]
        if self._expected_audience in auds:
            return True
        alt = getattr(self, "_alt_audiences", frozenset())
        if alt and any(a in alt for a in auds):
            return True
        from . import subdomain_project
        return any(subdomain_project.valid_org_audience(a) for a in auds)

    async def verify_token(self, token):
        result = await super().verify_token(token)
        if not result and self._fallback is not None:
            try:
                result = await self._fallback.verify_token(token)
            except Exception:
                result = None
        if not result:
            return None
        claims = getattr(result, "claims", None) or {}
        if not self._audience_ok(claims):
            logger.info("audience reject aud=%r", claims.get("aud"))
            return None
        if self._min_iat > 0:
            iat = claims.get("iat") or 0
            if iat < self._min_iat:
                logger.info(f"iat-gate reject sub={claims.get('sub')} iat={iat} < min_iat={self._min_iat}")
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
        # audience=None : la validation d'audience est faite par _IatGatedVerifier
        # (canonique OU sous-domaine org), unifiée sur le résultat primaire/fallback.
        fallback = JWTVerifier(
            jwks_uri=f"{alt_issuer}/jwks", issuer=alt_issuer,
            audience=None, algorithm="ES384",
        )
    return _IatGatedVerifier(
        jwks_uri=f"{issuer}/jwks",
        issuer=issuer,
        audience=None,
        algorithm="ES384",
        min_iat=min_iat,
        fallback=fallback,
        expected_audience=audience,
        alt_audiences=mcp_audience_alts(),
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
        # Scopes annoncés dans le PRM (RFC 9728). Sans ça le PRM expose
        # `scopes_supported: []` → un client conforme (Mistral) qui DÉRIVE le
        # `scope` de sa requête authorize du PRM n'envoie AUCUN scope → l'authorize
        # authentifié échoue côté Logto et renvoie une erreur au callback (erreur de
        # connexion immédiate, sans navigateur). claude.ai contourne en envoyant
        # `offline_access` en dur, d'où « marche dans Claude, pas dans Mistral ».
        # Mêmes scopes que l'AS metadata (oauth_facade.as_metadata). Vécu 2026-06-25.
        scopes_supported=["openid", "profile", "email", "offline_access"],
        resource_name="oto MCP",
    )


# Plus de bootstrap SOPS→platform_keys au boot (oto-mcp#12) : la DB (coffre
# `platform_keys`) est la SEULE source des clés plateforme. Poser/roter une clé =
# surface admin (REST `/api/admin/platform-keys` ou meta-tool MCP), jamais SOPS.
# L'unit pose `OTO_CONFIG_DISABLE_SOPS=1` : tout `require_secret` résiduel côté
# serveur échoue fort au lieu de lire le filesystem.

_SERVER_INSTRUCTIONS = instructions.render()


def _build_mcp(transport: str, verifier: JWTVerifier | None = None) -> FastMCP:
    # init_db idempotent — utile pour que les tables existent avant que
    # le middleware (per-user disabled_tools) ne les interroge.
    try:
        db.init_db()
    except Exception as e:
        logger.warning("init_db at _build_mcp failed: %s", e)
    # Suppression du perso : tout user existant sans org reçoit son espace maison
    # (one-shot idempotent, no-op aux boots suivants).
    try:
        from . import org_store
        org_store.backfill_personal_orgs()
    except Exception as e:
        logger.warning("backfill_personal_orgs at _build_mcp failed: %s", e)
    # ADR 0033 : credentials per-user (hors oauth) → scope membre (sub, org maison).
    # Re-chiffrement (l'AAD change) — APRÈS backfill_personal_orgs (org maison garantie).
    # One-shot idempotent, no-op aux boots suivants.
    try:
        from . import credentials_store
        credentials_store.backfill_member_scope()
    except Exception as e:
        logger.warning("backfill_member_scope at _build_mcp failed: %s", e)
    # ADR 0044 §F R2 : clés plateforme legacy + grants → instances scope PLATFORM
    # (credential re-chiffré + accès share_mode/share_down + quota meta.rate_limit*).
    # Idempotent, re-dérivé chaque boot tant que legacy = vérité (fenêtre R2→R4).
    try:
        credentials_store.backfill_platform_scope()
    except Exception as e:
        logger.warning("backfill_platform_scope at _build_mcp failed: %s", e)
    # ADR 0033 B4 : unipile_accounts au grain (sub, org, provider) — org de contexte
    # NOT NULL + platform_seat + PK composite. Même fenêtre (org maison garantie).
    try:
        db.backfill_unipile_member_scope()
    except Exception as e:
        logger.warning("backfill_unipile_member_scope at _build_mcp failed: %s", e)
    # Seed des blocs plateforme A/B (#50) s'ils n'existent pas (idempotent).
    try:
        instructions.seed_platform_blocks()
    except Exception as e:
        logger.warning("seed_platform_blocks at _build_mcp failed: %s", e)
    # Seed des guides plateforme on-demand depuis les fichiers `guides/*.md`
    # (idempotent — la DB est la source de vérité éditable, ADR 0042 tout-DB).
    try:
        from . import guide_store
        guide_store.seed_platform_guides()
    except Exception as e:
        logger.warning("seed_platform_guides at _build_mcp failed: %s", e)

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

    # Contrat d'erreur uniforme rendu à l'agent (D2, #124) : réécrit toute exception
    # de tool en McpError scrubbée + data {code, retryable, hint}. Ajouté AVANT Sentry
    # → OUTERMOST : Sentry (plus interne) capture le vrai traceback en premier, cette
    # enveloppe normalise en dernier (cf. ErrorEnvelopeMiddleware).
    from .middleware import ErrorEnvelopeMiddleware
    instance.add_middleware(ErrorEnvelopeMiddleware())

    # Capture des exceptions de tools vers Sentry (vrai traceback ; no-op si
    # OTO_SENTRY_DSN absent). Les erreurs de tool sont des erreurs JSON-RPC en
    # HTTP 200 → invisibles à l'intégration Starlette ; ce middleware les voit.
    from .sentry_setup import SentryToolErrorMiddleware
    instance.add_middleware(SentryToolErrorMiddleware())

    # Filtrage per-user des tools (toggle individuel sur /account).
    from .middleware import DynamicInstructionsMiddleware, UserDisabledToolsMiddleware
    instance.add_middleware(UserDisabledToolsMiddleware())
    # Injection de la doctrine de base de l'org dans les instructions du `initialize`
    # (canal fiable, par-(sub,org) — otomata-private#49, amende ADR 0014).
    instance.add_middleware(DynamicInstructionsMiddleware())
    # Journalisation des appels MCP (lib commune otomata-calllog, table tool_calls,
    # lue par /api/admin/monitoring/*). Identité via auth_hooks (auth Logto custom —
    # le get_access_token fastmcp par défaut ne la voit pas).
    import asyncio

    from otomata_calllog import ToolCallLogger

    from .auth_hooks import current_user_sub_from_token

    async def _calllog_sink(row: dict) -> None:
        # Corrélation (ADR 0017) : stampe session_id (session mcp) + run_id (déroulé
        # de doctrine actif) AVANT l'insert. Best-effort — un contexte absent
        # (ex. pas de session) ne casse jamais la journalisation.
        try:
            from fastmcp.server.dependencies import get_context

            from . import doctrine_run

            ctx = get_context()
            row["session_id"] = ctx.session_id
            # run_id de l'appel : axe explicite `run_id=` EN PRIORITÉ (modèle sans état
            # de session, #108 — la pile session-scopée ne survit pas au renouvellement
            # du Mcp-Session-Id), repli sur la pile session de `doctrine_run`.
            from . import session_org
            row["run_id"] = session_org.current_call_run() or await doctrine_run.active_run_id(ctx)
            # Org de l'appel (#67) : seam current_org du caller dans CE contexte de
            # session → scope exact du journal d'audit org. NULL hors org.
            from . import access
            row["org_id"] = access.current_org(row.get("sub"))
        except Exception:
            pass
        # to_thread : l'INSERT PG (pool psycopg sync) ne doit pas bloquer
        # l'event loop sur le chemin chaud de chaque tool call.
        await asyncio.to_thread(db.insert_tool_call, row)

    def _calllog_identity() -> dict:
        return {"sub": current_user_sub_from_token()}

    instance.add_middleware(
        ToolCallLogger(_calllog_sink, server="oto", identity=_calllog_identity)
    )

    # Rédaction des champs sensibles du RÉSULTAT des tools (ADR 0009/0015) selon la
    # politique de l'org active. EN DERNIER : l'exécution est en ordre inverse, donc
    # ce middleware enveloppe les autres et retouche le résultat final en sortie.
    from .middleware import FieldRedactionMiddleware
    instance.add_middleware(FieldRedactionMiddleware())

    # Contexte d'appel (`org=`, modèle sans état de session, #108/#112). ENCORE APRÈS
    # la rédaction → outermost : pose la ContextVar `_CALL_ORG` AVANT toute la chaîne et
    # la reset APRÈS, pour que le handler ET les hooks post-tool (rédaction, calllog) lisent
    # la MÊME org que l'appel. Garde d'appartenance au point d'entrée. Ensemble des tools
    # à `org=` réservé dérivé du registre de capacités (inerte pour les autres).
    from .middleware import CallContextMiddleware
    instance.add_middleware(
        CallContextMiddleware(_mcp_adapter.reserved_org_tool_names(_cap_registry.CAPABILITIES))
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

    # Surveillance des gels d'event loop (serveur mono-loop) : chaque callback
    # bloquant ≥1s est attribué dans le journal, ≥10s → event Sentry. AVANT le
    # démarrage de la loop (patch global de Handle._run). Cf. loop_watch.
    from . import loop_watch
    loop_watch.enable()

    # stdio retiré (2026-06-13) : oto-mcp ne se sert plus qu'en streamable_http
    # (toujours authentifié Logto). Conséquence voulue — plus de chemin local
    # sans login → la couche capacité (ADR 0009) peut supposer un sub résolu,
    # le cas-limite sub=None disparaît. L'usage local passe par la CLI `oto`.
    transport = os.environ.get("MCP_TRANSPORT", "streamable_http")

    global mcp
    if transport in ("http", "streamable_http"):
        host = os.environ.get("HOST", "127.0.0.1")
        port = int(os.environ.get("PORT", "9103"))

        # Instance MCP ANONYME (ADR 0032, `<slug>.mcp.oto.cx`) : sans auth, visibilité =
        # allowlist figée du preset de projet. On RÉUTILISE l'instance no-auth DÉJÀ
        # construite au niveau module (`mcp = _build_mcp("noauth")` en haut) au lieu d'en
        # construire une 3ᵉ : un _build_mcp de plus (register_all + mounts + init_db/
        # backfill/seed) DOUBLAIT le temps de boot (~53 s) et dépassait la fenêtre du
        # healthcheck du deploy → KO + rollback avant que uvicorn ne bind (vécu 2026-07-01).
        # `mcp` pointe encore ici sur l'instance no-auth ; on la capture AVANT de le
        # réassigner à l'authentifiée (tool_registry.bind finit donc lié à l'authentifiée).
        from .anon_visibility import AnonymousVisibilityMiddleware
        anon_mcp = mcp
        anon_mcp.add_middleware(AnonymousVisibilityMiddleware())
        anon_app = anon_mcp.http_app()
        # Shim OAuth ANONYME (ADR 0032) : claude.ai/Mistral exigent un flux OAuth pour un
        # connecteur custom, même sans auth → sans ces routes, DCR 404 = « impossible de
        # s'inscrire ». Le shim auto-approuve (zéro login) et délivre un token sans privilège
        # (l'app anonyme /mcp ne le vérifie pas). Inséré avant le catch /mcp de FastMCP.
        from . import anon_oauth
        for route in reversed(anon_oauth.make_routes()):
            anon_app.router.routes.insert(0, route)

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

        # Garde-fou on-demand TLS (ADR 0032) : Caddy appelle `/api/mcp/tls-check?domain=`
        # avant d'émettre un cert `<slug>.mcp.oto.cx` → 200 seulement pour un projet publié.
        # NON authentifié (appel localhost Caddy), inséré avant le gate JWT.
        from . import subdomain_project as _subproj
        for route in reversed(_subproj.make_routes()):
            app.router.routes.insert(0, route)

        # View-as (ADR 0023) : middleware ASGI brut, n'intervient que sur /api/* avec
        # le header X-Oto-Org (pass-through total sinon → n'altère pas le streaming /mcp).
        app.add_middleware(api_routes.ViewAsMiddleware, verifier=verifier)

        # Endpoint scopé par org (« 1 oto par org ») : épingle l'org depuis le Host
        # `<slug>--mcp.oto.ninja` (pass-through sur le host canonique). Ajouté APRÈS
        # → outermost, pose le candidat avant le reste de la chaîne.
        from . import subdomain_org
        app.add_middleware(subdomain_org.SubdomainOrgMiddleware)

        # Monitoring REST (ADR 0017, kind='rest') : journalise chaque /api/* dans le
        # flux unifié. Ajouté EN DERNIER → outermost : chronomètre toute la requête
        # (y compris ViewAs/Subdomain). Pass-through total hors /api/* (n'altère pas /mcp).
        app.add_middleware(api_routes.RestCallLogger)

        # Boucles de fond démarrées au boot en composant le lifespan FastMCP existant
        # (mono-process → une boucle par tâche). Chacune isolée en thread (ne bloque
        # pas l'event loop). Opt-out par env : OTO_SCHEDULER_ENABLED (email différé).
        # (L'index BOAMP/ACCO est passé au service FOD — ADR 0028 B2b — qui porte
        # désormais l'ingest ; plus de refresh in-process backend.)
        _bg_loops = []
        if os.environ.get("OTO_SCHEDULER_ENABLED", "1") != "0":
            from . import scheduler
            _bg_loops.append(scheduler.run_scheduler_loop)
        from . import billing as _billing
        if _billing.is_enabled() and os.environ.get("OTO_BILLING_RUNNER_ENABLED", "1") != "0":
            # échéances d'abonnement + réconciliation (ADR 0043 B3) — gaté sur le
            # feature flag billing (dormant en prod) + no-op sans STANCER_API_KEY.
            from . import billing_runner
            _bg_loops.append(billing_runner.run_billing_loop)
        import contextlib
        _prev_lifespan = app.router.lifespan_context

        @contextlib.asynccontextmanager
        async def _lifespan(app_):
            # Réchauffe le registre d'outils HORS de tout contexte de session
            # (visibilité non filtrée) → le manifeste `referenced_tools` reflète le
            # registre boot, pas la visibilité de la session courante (#75).
            from . import tool_registry
            try:
                await tool_registry.warm_registry(mcp)
            except Exception as e:
                logger.warning("warm_registry at boot failed: %s", e)
            tasks = [asyncio.create_task(f()) for f in _bg_loops]
            try:
                async with _prev_lifespan(app_):
                    yield
            finally:
                for t in tasks:
                    t.cancel()
                for t in tasks:
                    with contextlib.suppress(asyncio.CancelledError):
                        await t

        app.router.lifespan_context = _lifespan

        # App racine : dispatch par Host (ADR 0032). `<slug>.mcp.oto.cx` publié anonyme →
        # instance anonyme ; publié `org` → authentifiée + org épinglée ; sinon (canonique /
        # slug inconnu) → authentifiée. Compose les lifespans des deux instances FastMCP.
        from . import subdomain_project
        root_app = subdomain_project.HostDispatch(app, anon_app)

        import uvicorn
        logger.info("HTTP MCP server on %s:%d (+ anonymous <slug>.mcp.oto.cx)", host, port)
        uvicorn.run(
            root_app,
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
