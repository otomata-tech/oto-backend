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
import time

from fastmcp import FastMCP
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import AnyHttpUrl
from starlette.concurrency import run_in_threadpool

from . import db, instructions, tenancy
from .api import routes as api_routes
from .config import require_env, mcp_audience_alts
from .tools import register_all

logger = logging.getLogger("oto_mcp")


class _IatGatedVerifier(JWTVerifier):
    """JWTVerifier qui rejette en plus tout token avec `iat < MIN_TOKEN_IAT`.

    Permet une déco globale "soft" : bumper l'env `MIN_TOKEN_IAT` à `now()`
    invalide tous les tokens en cours sans toucher à Logto. Les clients
    reçoivent un 401 + WWW-Authenticate et re-lancent l'OAuth dance.
    """

    # Registre d'émetteurs (ADR 0052 §3) : `iss → (tenant, verifier)`, le verifier
    # étant `None` pour le primaire — c'est nous. Défaut de CLASSE : une instance
    # construite hors `_build_verifier` (tests du chemin jeton d'API) route sur le
    # primaire, comme avant le registre. Jamais muté.
    _by_issuer: dict = {}

    def __init__(self, *args, min_iat: int = 0,
                 by_issuer: "dict | None" = None,
                 expected_audience: "str | None" = None,
                 alt_audiences: "frozenset[str]" = frozenset(), **kwargs) -> None:
        # Le parent est construit SANS check d'audience (`audience=None`) : on valide
        # l'audience nous-mêmes (canonique OU sous-domaine org publié, ADR 0032 §barreau 4).
        super().__init__(*args, **kwargs)
        self._min_iat = min_iat
        # Le drain `LOGTO_ENDPOINT_ALT` n'est plus un mécanisme à part : il est une
        # ENTRÉE de ce registre, sur le tenant `oto` (deux émetteurs, un tenant).
        self._by_issuer = dict(by_issuer or {})
        self._expected_audience = expected_audience
        # Audiences canoniques SECONDAIRES (coexistence multi-domaine, ex. mcp.oto.cx).
        # Vide = no-op → mcp.oto.ninja inchangé.
        self._alt_audiences = alt_audiences

    def _audience_ok(self, claims, slug: str = "") -> bool:
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
        # Audience d'un host déclaré par le tenant DE CE JETON (binding strict).
        # ⚠️ `slug` est un PARAMÈTRE, pas un attribut d'instance : ce verifier est
        # unique et partagé par toutes les requêtes. Le porter sur `self` marcherait
        # aujourd'hui (rien n'attend entre la pose et la lecture) et casserait le jour
        # où ce chemin gagne un `await` — deux appels concurrents s'entrelaceraient et
        # une audience serait validée contre le tenant du VOISIN. Un tel bug
        # n'apparaît que sous charge et se lit comme un incident d'authentification.
        if any(_audience_of_declared_tenant(a, slug) for a in auds):
            return True
        from . import subdomain_project
        return any(subdomain_project.valid_org_audience(a) for a in auds)

    async def _verify_api_token(self, token):
        """Jeton d'API oto (`oto_…`, opaque) — la face MCP l'accepte comme la face REST.

        Un runtime NON INTERACTIF (Claude Tag dans Slack, une CI, un script) ne peut
        pas faire l'OAuth dance : sans ce chemin, il lui faut une app Logto dédiée,
        donc un compte machine orphelin, sans email ni dashboard — invisible et
        inconfigurable par son propriétaire. Ce jeton, lui, EST un vrai compte : créé
        et révoqué depuis le dashboard, il porte le `sub` de qui l'a émis.

        Un jeton PORTÉ (`scopes`) est refusé ici : son gate (`token_scopes.authorize`)
        raisonne sur méthode+chemin HTTP, notions absentes d'un appel MCP. L'accepter
        reviendrait à ignorer sa portée en silence — donc à l'élargir. Fail-closed.
        """
        if not token or not token.startswith("oto_"):
            return None
        # DB hors de la loop : un blip DB ne doit jamais geler le serveur mono-loop
        # (même raison que sur la face REST, vécu 2026-07-02).
        row = await run_in_threadpool(db.verify_api_token, token)
        if not row or row.get("scopes") is not None:
            return None
        sub = row["sub"]
        return AccessToken(token=token, client_id="oto_api_token", scopes=[],
                           subject=sub, claims={"sub": sub})

    def _route(self, token) -> tuple:
        """`(slug du tenant, verifier)` pour ce jeton — sélection par le claim `iss`,
        **non vérifié** (ADR 0052 §3).

        Le claim ne sert qu'à CHOISIR : le verifier retenu revalide ensuite l'émetteur
        pour de vrai (signature contre SON JWKS + `iss` byte-à-byte). Un jeton forgé
        qui revendique l'émetteur d'un tenant tiers est donc rejeté par le verifier de
        ce tenant, pas accepté par le nôtre. `iss` absent, malformé ou inconnu retombe
        sur le primaire — qui le rejettera aussi.
        """
        entry = self._by_issuer.get(tenancy.unverified_issuer(token) or "")
        return entry if entry is not None else (tenancy.PRIMARY_SLUG, None)

    def _qualified(self, result, slug: str):
        """Le point où un jeton devient un sub — l'UNIQUE endroit de la chaîne de
        vérification (les faces MCP et REST partagent cette instance).

        Tenant `oto` : le résultat ressort **tel quel**, byte pour byte. Ce n'est pas
        une optimisation — l'AAD du coffre dérive du sub, donc qualifier le sub du
        tenant `oto` rendrait tous les credentials existants indéchiffrables.
        """
        if slug == tenancy.PRIMARY_SLUG:
            return result
        claims = dict(getattr(result, "claims", None) or {})
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            return result       # pas de sub ⟹ pas d'identité, donc rien à qualifier
        claims["sub"] = tenancy.qualify(slug, sub)
        return result.model_copy(update={"claims": claims, "subject": claims["sub"]})

    async def verify_token(self, token):
        # Jeton d'API `oto_` : son sub sort de la DB, où il a été écrit DÉJÀ qualifié
        # (c'est un vrai compte). Le re-qualifier le préfixerait deux fois.
        api = await self._verify_api_token(token)
        if api is not None:
            return api
        slug, verifier = self._route(token)
        if verifier is None:
            result = await super().verify_token(token)
        else:
            try:
                result = await verifier.verify_token(token)
            # noqa: SILENT — dette déclarée : panne JWKS de tenant ⇒ vague de 401 sans une ligne (#424, verdict C)
            except Exception:
                result = None
        if not result:
            return None
        claims = getattr(result, "claims", None) or {}
        # Le tenant retenu voyage jusqu'au check d'audience — il n'est PAS relu du
        # jeton (ce serait déclaratif) : c'est celui dont le verifier vient de valider
        # la signature. Porté par l'appel, jamais par l'instance, qui est partagée.
        if not self._audience_ok(claims, slug):
            logger.info("audience reject aud=%r", claims.get("aud"))
            return None
        if self._min_iat > 0:
            iat = claims.get("iat") or 0
            if iat < self._min_iat:
                logger.info(f"iat-gate reject sub={claims.get('sub')} iat={iat} < min_iat={self._min_iat}")
                return None
        # Qualification EN DERNIER : un jeton recalé par l'audience ou l'iat ne doit
        # jamais avoir produit de sub.
        return self._qualified(result, slug)


def _audience_of_declared_tenant(aud, slug: str = "") -> bool:
    """L'audience canonique d'un host déclaré par le tenant **DU JETON**.

    Le binding complet est `host → tenant → (AS, audience)`, et il est **STRICT** :
    l'audience d'un partenaire n'est acceptée que si le jeton relève de CE partenaire.
    Un jeton du tenant primaire portant l'audience d'un partenaire est refusé — sinon
    « servi sur son domaine » ne voudrait rien dire de plus que « servi ».

    Trois conditions, toutes nécessaires : le host est réclamé par une ligne `tenants`
    (écrite à la main, jamais devinée) ; le chemin est exactement `/mcp` — déclarer un
    domaine ne consent qu'à son endpoint, pas au domaine entier ; et le tenant de ce
    host est celui du jeton.

    ⚠️ **Ce cran s'active tout seul, sans interrupteur — et c'est voulu.** Il ne peut
    rien accepter tant qu'aucune ligne ne porte de host (l'état d'aujourd'hui), donc il
    est inerte. Et tant que `MCP_AUDIENCE_ALT` porte l'audience d'un partenaire, elle
    est servie à tout le monde **avant** d'arriver ici (`_audience_ok` teste les alts
    en premier) : la transition reste douce. **Le retrait de cette variable EST
    l'activation du cran strict** — pas un drapeau à basculer, pas un second
    déploiement, juste une ligne d'environnement qui disparaît. Un drapeau se serait
    ajouté à la liste des choses à ne pas oublier ; celui-ci s'oublie tout seul.
    """
    if not isinstance(aud, str) or not aud:
        return False
    from urllib.parse import urlparse
    from . import tenancy
    parsed = urlparse(aud.rstrip("/"))
    if parsed.scheme != "https" or parsed.path != "/mcp":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    entry = tenancy.current().for_host(host)
    # Le tenant du host DOIT être celui du jeton. `slug` vient de la sélection par
    # `iss` (déjà revalidée par le verifier retenu), donc il n'est pas déclaratif.
    return entry is not None and entry.slug == slug


def _registry_and_issuers() -> "tuple[tenancy.IssuerRegistry, dict]":
    """`(registre, by_issuer)` depuis l'env + la base — la construction PARTAGÉE
    entre le boot (`_build_verifier`) et le rechargement à chaud
    (`reload_tenant_registry`). Une seule écriture de cette logique : un reload qui
    la recopierait divergerait au premier tenant ajouté au boot seulement.
    """
    logto_endpoint = require_env("LOGTO_ENDPOINT").rstrip("/")
    issuer = f"{logto_endpoint}/oidc"
    alt = os.environ.get("LOGTO_ENDPOINT_ALT", "").strip().rstrip("/")
    drains = [f"{alt}/oidc"] if alt else []
    registry = tenancy.IssuerRegistry(
        tenancy.build(issuer, drain_issuers=drains, tenants=tenancy.load_tenants()))
    by_issuer: dict = {}
    for entry in registry.entries():
        if entry.issuer == issuer:
            by_issuer[entry.issuer] = (entry.slug, None)   # le primaire, c'est nous
            continue
        # audience=None : la validation d'audience reste faite par `_audience_ok`,
        # uniformément sur tous les tenants (le cran par tenant est le lot L3).
        # Logto self-hosted signe en ES384 (vérifié sur /oidc/jwks).
        by_issuer[entry.issuer] = (entry.slug, JWTVerifier(
            jwks_uri=entry.jwks_uri, issuer=entry.issuer,
            audience=None, algorithm="ES384"))
    return registry, by_issuer


# Le verifier VIVANT du process — posé par `_build_verifier`, lu par le reload à
# chaud. None tant que le serveur n'est pas construit (tests, scripts hors serveur).
_VERIFIER: "_IatGatedVerifier | None" = None


def reload_tenant_registry() -> dict:
    """Recharge le registre d'émetteurs depuis la base, SANS redémarrer (ADR 0052 B4,
    la moitié « prise d'effet » du provisionnement — déclarer reste un runbook).

    Deux écritures, dans cet ordre, toutes deux des swaps de référence (atomiques
    sous CPython — aucun lecteur ne voit un état intermédiaire) :

    1. `tenancy.install(registre_neuf)` — classification des subs, hosts, préfixes
       d'outils, façade OAuth : tout ce qui lit `tenancy.current()` par requête.
    2. `verifier._by_issuer = dict_neuf` — les émetteurs ACCEPTÉS : c'est le swap
       qui éteint le verdict `pending_restart` (les jetons du tenant passent).

    Échec de lecture (base, env) ⟹ l'exception REMONTE et rien n'est écrit : le
    process garde le registre d'avant, entier — jamais un registre à moitié posé.
    """
    registry, by_issuer = _registry_and_issuers()
    tenancy.install(registry)
    verifier_updated = False
    if _VERIFIER is not None:
        _VERIFIER._by_issuer = dict(by_issuer)
        verifier_updated = True
    logger.info("registre d'émetteurs RECHARGÉ : %s (verifier %s)",
                {iss: slug for iss, (slug, _) in by_issuer.items()},
                "mis à jour" if verifier_updated else "absent — process sans serveur")
    return {"tenants": sorted({e.slug for e in registry.entries()}),
            "issuers": len(by_issuer), "verifier_updated": verifier_updated}


def _build_verifier() -> JWTVerifier:
    """JWT verifier partagé entre l'auth MCP et l'API REST — **registre d'émetteurs**
    (ADR 0052 §3).

    L'émetteur primaire vient de l'env (`LOGTO_ENDPOINT`) et porte le tenant `oto` :
    ce chemin est DB-indépendant, l'authentification canonique ne casse jamais sur un
    hoquet de base. `LOGTO_ENDPOINT_ALT` — la fenêtre de drain d'une bascule
    d'instance Logto — n'est plus un mécanisme à côté : c'est une **entrée du
    registre**, sur le même tenant `oto` (deux émetteurs, un tenant, sub nu des deux
    côtés). Les tenants TIERS viennent de la base, avec leur slug et leur JWKS.
    """
    audience = require_env("MCP_AUDIENCE")
    logto_endpoint = require_env("LOGTO_ENDPOINT").rstrip("/")
    issuer = f"{logto_endpoint}/oidc"
    min_iat = int(os.environ.get("MIN_TOKEN_IAT", "0") or "0")
    registry, by_issuer = _registry_and_issuers()
    # Posé pour les consommateurs HORS chemin de vérification : l'attribution du
    # journal REST (`_claimed_sub`, qui décode sans vérifier) et la garde d'alias
    # (`db.users.migrate_sub`). Jamais pour décider d'une autorisation.
    tenancy.install(registry)
    logger.info("registre d'émetteurs : %s",
                {iss: slug for iss, (slug, _) in by_issuer.items()})
    global _VERIFIER
    _VERIFIER = _IatGatedVerifier(
        jwks_uri=f"{issuer}/jwks",
        issuer=issuer,
        audience=None,
        algorithm="ES384",
        min_iat=min_iat,
        by_issuer=by_issuer,
        expected_audience=audience,
        alt_audiences=mcp_audience_alts(),
    )
    return _VERIFIER


class TenantChallengeMiddleware:
    """Le `WWW-Authenticate` d'un 401 pointe le PRM à consulter. Sur un host servi
    POUR un autre tenant, il doit pointer le PRM de CE host (ADR 0052 L3).

    Pourquoi un middleware ASGI et pas un paramètre : le pointeur est figé à la
    construction de `RequireAuthMiddleware` (`resource_metadata_url`), qui ne voit
    pas la requête. Réécrire l'en-tête au vol est le seul endroit où le host est
    connu — et ça garde intact le reste de la chaîne d'auth.

    Deux gardes qui font que ce lot atterrit **inerte** :

    - on ne touche QUE les réponses **401** portant un `resource_metadata=` ;
    - et seulement si le host est **réclamé par un tenant**. Aucune ligne ne porte
      de host aujourd'hui, donc le registre rend `None` et rien n'est réécrit :
      l'octet servi est celui d'avant.

    Ne réécrit jamais l'émetteur annoncé dans le challenge : c'est le PRM pointé qui
    le porte, et lui suit déjà le host (`oauth_facade`). Une seule source.
    """

    _MARK = "resource_metadata="

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        host = ""
        for k, v in scope.get("headers") or ():
            if k == b"host":
                host = v.decode("latin-1").split(":")[0].strip().lower()
                break
        from . import tenancy
        entry = tenancy.current().for_host(host) if host else None
        if entry is None:                      # cas nominal — rien à faire
            await self.app(scope, receive, send)
            return

        async def _send(message):
            if message.get("type") == "http.response.start" and \
                    int(message.get("status") or 0) == 401:
                headers, changed = [], False
                for k, v in message.get("headers") or ():
                    if k.lower() == b"www-authenticate":
                        txt = v.decode("latin-1")
                        if self._MARK in txt:
                            txt = _point_challenge_at_host(txt, host)
                            v = txt.encode("latin-1")
                            changed = True
                    headers.append((k, v))
                if changed:
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, _send)


def _point_challenge_at_host(challenge: str, host: str) -> str:
    """Réécrit l'URL de `resource_metadata=` du challenge sur `host`, en gardant le
    chemin annoncé (il porte le suffixe de la ressource, ex. `.../mcp`) — c'est le
    HOST qui est faux, jamais le chemin."""
    import re as _re

    def _swap(m):
        url = m.group(1)
        path = url.split("://", 1)[-1]
        path = path[path.find("/"):] if "/" in path else ""
        return f'resource_metadata="https://{host}{path}"'

    return _re.sub(r'resource_metadata="([^"]+)"', _swap, challenge)


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

# Clés du relevé d'appel (`session_org.note_call_trace`) versées dans les args du
# journal. Liste FERMÉE : le relevé est un seam de service interne, il ne doit pas
# devenir une porte par laquelle n'importe quel état de résolution finit journalisé.
#
# Les deux clés ajoutées le 12/08 sont l'EMPREINTE d'un run (chantier du run, lot J2 ;
# ADR 0055-D10 / 0058-D1 : « le gel des versions EST l'empreinte du run ») — ce qui
# manquait pour qu'un déroulé soit reproductible :
#   • `doctrine_version` — la VERSION de la procédure exécutée, résolue par `run_start`
#     (`runs.doctrine` ne porte qu'un slug, or les procédures sont versionnées) ;
#   • `instance` — le ref de l'instance de connecteur RÉSOLUE pour l'appel
#     (`instance_refs`), c'est-à-dire quel credential a réellement servi. Le journal
#     enregistre l'appel, pas ce qu'il a résolu : `_instance=`/`_account=` sont retirés
#     des args par `CallContextMiddleware` (le plus externe) AVANT que `ToolCallLogger`
#     ne journalise, et une résolution sans jeton ne laisse aucune trace du tout.
#     ⚠️ **Aucun seam ne l'alimente encore** : le point d'écriture est la sortie de
#     `access.resolve_credential` (le résolveur unique, ADR 0024) — une ligne
#     `session_org.note_call_trace(instance=<ref de la ligne gagnante>)`. Tant qu'elle
#     n'y est pas, cette clé n'apparaît dans AUCUNE ligne de journal : ne rien bâtir
#     dessus sans avoir vérifié qu'elle se remplit.
#
# `readonly_forced` (#658, 02/09/2026) — les colonnes VERROUILLÉES qu'un appel a
# remplacées de force, avec la ligne et la valeur d'avant. C'est la SEULE trace du
# geste, tranchée comme telle : pas de colonne de plus sur la ligne. ⚠️ Le journal ne
# remonte qu'à ~35 jours, donc la trace disparaît alors que la valeur forcée reste —
# la question a été posée et fermée en connaissance de cause. Le « qui » n'est pas
# répété ici : le sink stampe déjà `sub` et `org_id`.
_TRACED_ARGS = ("ns_id", "doctrine_version", "instance", "readonly_forced")


_PREPARED = False
"""Vrai dès que `_prepare_database` a tourné DANS CE PROCESS. Garde par PROCESS et
non par base : sur la base partagée prod/preprod, « déjà fait » n'est pas une
propriété de la base (une autre instance a pu la migrer entre-temps, et le boot
suivant doit quand même pouvoir la réparer) — c'est une propriété de CE démarrage."""


def _timed(label: str):
    """Chronomètre une étape du démarrage et la journalise. La première chose que
    l'ADR 0065 demande d'instrumenter : « on décide sur mesure, pas sur intuition »."""
    import contextlib

    @contextlib.contextmanager
    def _cm():
        debut = time.monotonic()
        try:
            yield
        finally:
            logger.info("boot: %s %.0f ms", label, (time.monotonic() - debut) * 1000)
    return _cm()


def _prepare_database() -> None:
    """Le schéma et les backfills one-shot — **une seule fois par process**.

    Avant l'ADR 0065 lot 0, ce bloc vivait en tête de `_build_mcp`, appelé DEUX fois
    (l'instance anonyme au niveau module, puis l'authentifiée dans `main`), plus un
    `db.init_db()` nu dans `main` : **3 `init_db` et 2 tours de backfills par boot**.
    Mesuré le 2026-08-28 sur la base servie (RTT 3,14 ms/ordre) : 2,8 s pour les trois
    `init_db`, 1,2 s pour les deux `backfill_personal_orgs` (82 users × 2 allers-retours
    chacun) — ~4,5 s rendus à la fenêtre du healthcheck, qui est finie (120 s).

    La garde est ICI et pas dans `db.init_db` à dessein : `init_db()` doit rester
    rejouable, c'est ainsi que les tests prouvent son idempotence (`init_db(); init_db()`
    = no-op). Ce qui ne doit pas se rejouer, c'est le CHEMIN DU BOOT — et il est ici.

    Fail-open, étape par étape, comme avant : un backfill qui casse ne doit pas
    empêcher le serveur de répondre.
    """
    global _PREPARED
    if _PREPARED:
        return
    _PREPARED = True
    debut = time.monotonic()
    # init_db idempotent — utile pour que les tables existent avant que
    # le middleware (per-user disabled_tools) ne les interroge.
    try:
        db.init_db()
    except Exception as e:
        logger.warning("init_db at boot failed: %s", e)
    # Suppression du perso : tout user existant sans org reçoit son espace maison
    # (one-shot idempotent, no-op aux boots suivants).
    try:
        from . import org_store
        with _timed("backfill_personal_orgs"):
            org_store.backfill_personal_orgs()
    except Exception as e:
        logger.warning("backfill_personal_orgs at boot failed: %s", e)
    # Orgs nées à NULL avant que `create_org` ne dérive le front lui-même (console
    # admin, orgs perso) → front de leur tenant. One-shot idempotent.
    try:
        with _timed("backfill_org_front"):
            org_store.backfill_org_front()
    except Exception as e:
        logger.warning("backfill_org_front at boot failed: %s", e)
    # ADR 0033 : credentials per-user (hors oauth) → scope membre (sub, org maison).
    # Re-chiffrement (l'AAD change) — APRÈS backfill_personal_orgs (org maison garantie).
    # One-shot idempotent, no-op aux boots suivants.
    try:
        from . import credentials_store
        with _timed("backfill_member_scope"):
            credentials_store.backfill_member_scope()
    except Exception as e:
        logger.warning("backfill_member_scope at boot failed: %s", e)
    # (Le backfill ADR 0044 §F R2 — clés plateforme legacy → instances scope PLATFORM —
    # a été RETIRÉ le 2026-07-28 : la fenêtre R2→R4 est close, `platform_keys` est droppée
    # par les migrations (`db/_init.py`) et les lectures passent par le coffre unifié. Il
    # ne migrait donc plus rien : il levait « relation platform_keys does not exist » à
    # CHAQUE boot, allongeant d'autant la fenêtre pendant laquelle le service ne répond pas.)
    # ADR 0033 B4 : unipile_accounts au grain (sub, org, provider) — org de contexte
    # NOT NULL + platform_seat + PK composite. Même fenêtre (org maison garantie).
    try:
        with _timed("backfill_unipile_member_scope"):
            db.backfill_unipile_member_scope()
    except Exception as e:
        logger.warning("backfill_unipile_member_scope at boot failed: %s", e)
    # Seed des blocs plateforme A/B (#50) s'ils n'existent pas (idempotent).
    try:
        instructions.seed_platform_blocks()
    except Exception as e:
        logger.warning("seed_platform_blocks at boot failed: %s", e)
    # Seed des guides plateforme on-demand depuis les fichiers `guides/*.md`
    # (idempotent — la DB est la source de vérité éditable, ADR 0042 tout-DB).
    try:
        from . import guide_store
        with _timed("seed_platform_guides"):
            guide_store.seed_platform_guides()
    except Exception as e:
        logger.warning("seed_platform_guides at boot failed: %s", e)
    # Surcharges de propriétés de connecteur (L6 pièce 2 c2) : LECTURE, pas travail —
    # une poignée de lignes, chargées en mémoire pour que le chemin chaud n'ait jamais
    # à les demander (la cardinalité est consultée jusqu'à 4× par appel d'outil, sur un
    # serveur mono-loop). Ici plutôt qu'à la première résolution : au boot l'échec est
    # journalisé et le process retombe sur les défauts du registre, c'est-à-dire le
    # comportement d'avant le lot. ⚠️ PAR PROCESS — cf. `reload` de la capacité admin.
    try:
        from .connectors import cardinality
        with _timed("load_connector_settings"):
            cardinality.reload()
    except Exception as e:
        logger.warning("load_connector_settings at boot failed: %s", e)
    logger.info("boot: préparation de la base %.0f ms (une seule fois par process)",
                (time.monotonic() - debut) * 1000)


def _build_mcp(transport: str, verifier: JWTVerifier | None = None, *,
               include_mounts: bool = False) -> FastMCP:
    """Construit le catalogue. Ne prépare PAS la base, ne va PAS chercher les
    catalogues distants sauf demande explicite.

    Construire n'est pas démarrer. `main()` prépare la base puis demande les
    catalogues distants ; un test, un script ou un outil qui veut seulement le
    catalogue local n'a plus à payer — ni à subir — une entrée-sortie pour
    l'obtenir. La fédération est derrière `include_mounts` parce qu'elle attend
    un tiers, et qu'une attente n'a rien à faire dans un import (oto-backend#892).
    """
    kwargs: dict = {}
    if transport in ("http", "streamable_http") and verifier is not None:
        kwargs["auth"] = _build_auth(verifier)
    instance = FastMCP("oto", instructions=_SERVER_INSTRUCTIONS, **kwargs)
    # Lier l'instance pour que les handlers de capacité (guide) résolvent le
    # manifeste « referenced_tools » sans se faire passer l'instance.
    from . import tool_registry
    tool_registry.bind(instance)
    register_all(instance, include_mounts=include_mounts)

    # Couche capacité (ADR 0009) : monte un tool par capacité déclarée
    # (no-op tant que le registre est vide — canari). Après register_all pour
    # que le test d'unicité voie les deux mondes (legacy + capacités).
    from .capabilities import _mcp_adapter
    from .capabilities import registry as _cap_registry
    _mcp_adapter.register(instance, _cap_registry.CAPABILITIES)

    # ⚠️ ORDRE : fastmcp exécute les middlewares dans l'ordre d'ajout — le PREMIER
    # ajouté est le plus EXTERNE (vérifié empiriquement, `_run_middleware` wrap en
    # reversed()). Les commentaires historiques « ajouté en dernier = outermost »
    # étaient FAUX : rédaction et contexte d'appel tournaient au plus INTERNE, donc
    # `_CALL_ORG` était reset AVANT que la rédaction et le calllog (plus externes) ne
    # relisent `current_org` → un appel épinglé `_org=` était rédigé/audité sous l'org
    # MAISON. Corrigé 2026-08-02 : l'ordre d'ajout ci-dessous EST l'ordre extern→interne.

    # 0. Nom des outils au nom du PRODUIT du tenant (ADR 0052) — OUTERMOST absolu :
    # il rétablit le nom CANONIQUE avant que le reste de la chaîne ne le lise (gates
    # `_org=`, rédaction par namespace, visibilité, journal), et renomme la liste
    # servie en dernier, après le filtrage de visibilité. Inerte pour le tenant `oto`
    # et pour tout tenant qui ne déclare pas de préfixe (cf. `tool_alias`).
    from .middleware.alias import ToolAliasMiddleware
    instance.add_middleware(ToolAliasMiddleware())

    # 0 bis. Compte en PAUSE : refus de TOUTE requête, avant tout le reste. Sous
    # `ToolAlias` seulement parce que celui-ci se déclare outermost absolu et
    # rétablit un nom en entrée — nom que ce refus ne lit pas, donc l'ordre entre
    # eux est sans conséquence. Il est en revanche AU-DESSUS du contexte d'appel,
    # de la rédaction, de la visibilité et du journal : rien de tout cela n'a de
    # raison de tourner pour une requête qu'on refuse, et un compte neutralisé ne
    # doit pas non plus RECEVOIR les instructions d'org du handshake.
    from .middleware.account_suspended import AccountSuspendedMiddleware
    instance.add_middleware(AccountSuspendedMiddleware())

    # 1. Rendu du VIDE en PHRASE (otomata-tech/oto#32) : un résultat sans aucun
    # résultat ne part JAMAIS en structure nue dans le canal texte, qui fait dégénérer
    # le décodage du modèle. Plus EXTERNE que la rédaction et que l'écho de compte,
    # qui réémettent le payload en JSON — tourner avant eux rétablirait la structure
    # qu'on vient d'en retirer ; sous `ToolAlias`, dont il lui faut le nom canonique
    # pour trouver le gabarit de l'outil.
    from .middleware.empty_result import EmptyResultMiddleware
    instance.add_middleware(EmptyResultMiddleware())

    # ⚠️ Il y a EU ici un « filet de contexte » (v1.155.0, quelques heures le
    # 2026-08-28) : le bloc de l'org livré dans la première réponse d'outil d'une
    # session qui ne l'avait pas chargé, avec un registre mémoire « déjà servi » à
    # fenêtre de 30 min. RETIRÉ le jour même : ce registre est un état de session
    # côté serveur, interdit par ADR 0038 — et la conception d'origine (projet oto
    # 180, page 481 §11.3) l'excluait déjà : « l'assemblage doit tenir dans une
    # requête ordinaire, SANS ÉTAT CONSERVÉ ENTRE APPELS ». Le symptôme mesuré en
    # prod (livraison dépendante de qui a appelé en dernier, sous un jeton partagé)
    # était la conséquence directe de cet état. Un successeur devra être sans état ;
    # le besoin, lui, reste ouvert : oto-backend#478.

    # 2. Contexte d'appel (`_org=`, modèle sans état de session, #108/#112) : pose la
    # ContextVar `_CALL_ORG` AVANT le reste de la chaîne et la reset APRÈS, pour que
    # le handler ET les hooks post-tool (rédaction, calllog) lisent la MÊME org que
    # l'appel. Garde d'appartenance au point d'entrée.
    from .middleware.call_context import CallContextMiddleware
    instance.add_middleware(
        CallContextMiddleware(_mcp_adapter.reserved_org_tool_names(_cap_registry.CAPABILITIES))
    )

    # 3. Rédaction des champs sensibles du RÉSULTAT des tools (ADR 0009/0015) selon la
    # politique de l'org active — sous le contexte d'appel (lit la bonne org), au-dessus
    # de tout le reste (retouche le résultat final en sortie).
    from .middleware.field_redaction import FieldRedactionMiddleware
    instance.add_middleware(FieldRedactionMiddleware())

    # 4. Contrat d'erreur uniforme rendu à l'agent (D2, #124) : réécrit toute exception
    # de tool en McpError scrubbée + data {code, retryable, hint}. Plus externe que
    # Sentry et le calllog → eux voient l'erreur BRUTE, l'enveloppe normalise en dernier.
    from .middleware.error_envelope import ErrorEnvelopeMiddleware
    instance.add_middleware(ErrorEnvelopeMiddleware())

    # 5. Filtrage per-user des tools (toggle individuel sur /account) — au-dessus du
    # calllog : un refus de gate (tool_not_mounted) n'est pas journalisé (inchangé).
    from .middleware.disabled_tools import UserDisabledToolsMiddleware
    from .middleware.dynamic_instructions import DynamicInstructionsMiddleware
    instance.add_middleware(UserDisabledToolsMiddleware())
    # Injection du guide de base de l'org dans les instructions du `initialize`
    # (par-(sub,org) — otomata-private#49, amende ADR 0014 ; ⚠️ livraison NON garantie
    # côté client, #478 : le bloc A est un socle-résumé qui pointe le guide `notice`).
    instance.add_middleware(DynamicInstructionsMiddleware())
    # Journalisation des appels MCP (middleware inliné `calllog.py`, table tool_calls,
    # lue par /api/admin/monitoring/*). Identité via auth_hooks (auth Logto custom —
    # le get_access_token fastmcp par défaut ne la voit pas).
    import asyncio

    from .calllog import ToolCallLogger

    from .auth.hooks import current_user_sub_from_token

    async def _calllog_sink(row: dict) -> None:
        # Corrélation (ADR 0017) : stampe session_id (session mcp) + run_id (déroulé
        # de guide actif) AVANT l'insert. Best-effort — un contexte absent
        # (ex. pas de session) ne casse jamais la journalisation.
        try:
            from fastmcp.server.dependencies import get_context

            from . import guide_run

            ctx = get_context()
            row["session_id"] = ctx.session_id
            # #117 — l'identifiant de la requête entrante. `session_id` désigne une
            # conversation entière : il ne distingue pas deux appels concurrents de la
            # même session, qui est précisément le cas suspecté.
            row["request_id"] = str(ctx.request_id) if ctx.request_id is not None else None
            # run_id de l'appel : axe explicite `_run_id=` EN PRIORITÉ (modèle sans état
            # de session, #108 — la pile session-scopée ne survit pas au renouvellement
            # du Mcp-Session-Id), repli sur la pile session de `guide_run`.
            from . import session_org
            row["run_id"] = session_org.current_call_run() or await guide_run.active_run_id(ctx)
            # Org de l'appel (#67) : seam current_org du caller dans CE contexte de
            # session → scope exact du journal d'audit org. NULL hors org.
            from . import access
            row["org_id"] = access.current_org(row.get("sub"))
            # Application cliente porteuse du grant (`azp` — claude.ai, Claude Code,
            # ChatGPT…) : axe télémétrie par surface. Contexte copié par create_task
            # → le token est lisible ici comme dans le handler.
            from .auth.hooks import current_client_id_from_token
            row["client_id"] = current_client_id_from_token()
            # #117 — le compte relu APRÈS exécution du handler, là où `row["sub"]` est
            # celui capturé à l'ENTRÉE (middleware, avant `call_next`). Les deux DOIVENT
            # être égaux : une divergence signifie que l'appel a été servi sous une autre
            # identité que celle qui l'a demandé. C'est le seul champ qui puisse trahir
            # une réponse partie à la mauvaise requête — et la seule façon de trancher
            # une hypothèse ouverte depuis le 06/07 autrement qu'en la supposant.
            # Écrit TOUJOURS (pas seulement en cas d'écart) : une colonne qui ne se
            # remplit qu'en cas de bug ne se distingue pas d'une colonne qui ne marche
            # pas — et on ne saurait pas lire son silence.
            effective = current_user_sub_from_token()
            row["effective_sub"] = effective
            if effective != row.get("sub"):
                # Cri immédiat : la ligne de journal suffit à l'analyse a posteriori,
                # pas à réagir. Rare par construction ⟹ aucun risque de bruit.
                logger.error(
                    "IDENTITÉ DIVERGENTE sur %s — demandée=%r effective=%r "
                    "(call_uid=%s request_id=%s session=%s)",
                    row.get("tool"), row.get("sub"), effective,
                    row.get("call_uid"), row.get("request_id"), row.get("session_id"))
            # Lien journal → traceback : l'event Sentry capturé pour CET appel par
            # `SentryToolErrorMiddleware` (innermost → il a déjà tourné quand on écrit).
            # None sur le chemin nominal, sur une erreur GÉRÉE (4xx amont) et sans DSN.
            from .sentry_setup import current_tool_event_id
            row["sentry_event_id"] = current_tool_event_id()
            # Entités résolues pendant l'appel (relevé rempli par les seams, ex.
            # `DatastorePg._resolve` → ns_id). Versées DANS les args, à côté de ce que
            # l'agent a tapé : `namespace` reste l'intention, `ns_id` devient la clé de
            # corrélation exacte des lectures du journal. Le contexte est copié par
            # create_task → le holder est le même objet que celui muté par le handler.
            trace = session_org.current_call_trace()
            if trace:
                row["args"] = {**(row.get("args") or {}),
                               **{k: v for k, v in trace.items() if k in _TRACED_ARGS}}
        # noqa: SILENT — dette déclarée : tout l'enrichissement du journal tombe d'un bloc (#424, verdict C)
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

    # 8. Capture des exceptions de tools vers Sentry (no-op si OTO_SENTRY_DSN absent) —
    # INNERMOST : au plus près du handler, capture le vrai traceback AVANT le calllog
    # (qui pourra stamper l'event_id) et l'enveloppe (qui scrubbe). Les erreurs de tool
    # sont des erreurs JSON-RPC en HTTP 200 → invisibles à l'intégration Starlette.
    from .sentry_setup import SentryToolErrorMiddleware
    instance.add_middleware(SentryToolErrorMiddleware())

    return instance


# ⚠️ PAS d'instance construite ici. Une construction au niveau module s'exécute à
# l'IMPORT — donc à la collecte de tout test qui touche ce module — et elle
# entraînait avec elle la préparation de la base et le chargement des catalogues
# distants, c'est-à-dire deux attentes sans délai maximal (oto-backend#892).
# Un import doit définir, pas travailler. `main()` construit ; les tests qui ont
# besoin du catalogue local le demandent par `tests/_mcp_app.static_mcp()`.
mcp: FastMCP | None = None


def build_root_app(app, anon_app):
    """L'app ASGI servie par uvicorn, de l'intérieur vers l'extérieur.

    1. **dispatch par Host** (ADR 0032) : `<slug>.mcp.oto.cx` publié anonyme → instance
       anonyme ; publié `org` → authentifiée + org épinglée ; sinon (host canonique ou
       slug inconnu) → authentifiée. Compose les lifespans des deux instances FastMCP.
    2. **étiquetage du charset** (#472) : `text/event-stream` et `application/json`
       partent complétés d'un `charset=utf-8`. Posée SOUS la garde mais AU-DESSUS du
       dispatch, donc elle couvre les deux instances (canonique et anonyme) et toute
       la face REST d'un seul geste. Cf. `response_charset` pour le pourquoi.
    3. **étiquette de version** (oto#33) : chaque réponse part avec `X-Oto-Version`,
       de sorte qu'un journal d'appels DÉJÀ écrit permette de dater rétrospectivement
       un changement de comportement. Posée au-dessus du charset et du dispatch pour
       la même raison que lui — elle couvre les deux instances FastMCP et toute la
       face REST d'un seul geste. Cf. `version_header`.
    4. **garde de déconnexion client** (#352), la couche la plus EXTERNE — entre uvicorn
       et tout le reste. Un POST `/mcp` dont le client est parti en cours de route
       laissait une réponse ASGI incomplète ; uvicorn fermait alors le transport, et
       Caddy rendait des 502 sur cette connexion **et sur les requêtes voisines** qui
       héritaient de la connexion empoisonnée dans son pool keep-alive. La garde
       complète la réponse à la place du client parti, et n'attrape QUE cette classe
       (toute autre exception traverse). Elle ne touche NI la chaîne de middlewares
       FastMCP (leur ordre est un contrat, cf. `tests/middleware/test_middleware_order.py`) NI la
       lib `mcp`.

    Fonction séparée de `main` pour que l'assemblage soit VÉRIFIABLE : `main` démarre
    uvicorn, donc une garde posée là ne serait couverte par aucun test — et une garde
    qu'on peut retirer sans faire rougir la suite n'est pas une garde
    (`tests/test_client_disconnect_guard.py`)."""
    from . import subdomain_project
    from .client_disconnect_guard import ClientDisconnectGuard
    from .response_charset import ResponseCharset
    from .version_header import VersionHeader
    return ClientDisconnectGuard(
        VersionHeader(
            ResponseCharset(subdomain_project.HostDispatch(app, anon_app))))


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

    # Le schéma et les backfills, UNE fois par process. Cet appel vivait dans
    # `_build_mcp`, donc à l'import du module ; il est ici parce que préparer une
    # base est un geste de DÉMARRAGE, et que `main` est le seul endroit qui
    # démarre. Fail-open étape par étape, inchangé : un backfill qui casse ne
    # doit pas empêcher le serveur de répondre — mais il le dit dans le journal.
    _prepare_database()

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
        # Les catalogues distants sont demandés ICI, une fois la base prête —
        # plus à l'import. `include_mounts=True` est le seul endroit du code qui
        # accepte d'attendre un tiers, et c'est un démarrage, pas une collecte.
        anon_mcp = _build_mcp("noauth", include_mounts=True)
        anon_mcp.add_middleware(AnonymousVisibilityMiddleware())
        anon_app = anon_mcp.http_app()
        # Shim OAuth ANONYME (ADR 0032) : claude.ai/Mistral exigent un flux OAuth pour un
        # connecteur custom, même sans auth → sans ces routes, DCR 404 = « impossible de
        # s'inscrire ». Le shim auto-approuve (zéro login) et délivre un token sans privilège
        # (l'app anonyme /mcp ne le vérifie pas). Inséré avant le catch /mcp de FastMCP.
        from .auth import anon as anon_oauth
        for route in reversed(anon_oauth.make_routes()):
            anon_app.router.routes.insert(0, route)

        verifier = _build_verifier()
        mcp = _build_mcp(transport, verifier, include_mounts=True)

        # (Le `db.init_db()` qui était ici a été RETIRÉ — ADR 0065 lot 0. C'était le
        # TROISIÈME du boot : `_build_mcp` l'appelait déjà, deux fois, et il rejouait
        # 297 ordres SQL sur une base managée pour un résultat déjà obtenu. La
        # préparation de la base est désormais gardée par process, cf.
        # `_prepare_database`.)
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
            from .auth import facade as oauth_facade
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

        # Découverte sensible au host (ADR 0052 L3) : sur un host réclamé par un
        # tenant tiers, le 401 doit pointer SON PRM, pas le nôtre. Pass-through total
        # tant qu'aucun tenant ne déclare de host — l'état d'aujourd'hui.
        app.add_middleware(TenantChallengeMiddleware)

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
        # Indexation sémantique (lot 3) : draine l'outbox embed_dirty hors event loop.
        # No-op sans MISTRAL_API_KEY (la recherche reste lexicale).
        if os.environ.get("OTO_EMBED_WORKER_ENABLED", "1") != "0":
            from . import embed_worker
            _bg_loops.append(embed_worker.run_embed_loop)
        # Extraction du texte des fichiers déposés (#298) : les rend cherchables par
        # ce qu'ils CONTIENNENT. Boucle SÉPARÉE de l'indexation sémantique, et pas par
        # goût du découpage — `run_embed_loop` sort d'emblée sans MISTRAL_API_KEY, or
        # l'extraction ne dépend d'aucun service tiers. L'y greffer la rendrait muette
        # sur un déploiement sans clé, sous le symptôme « la recherche de fichiers ne
        # trouve rien », sans erreur nulle part. Les domaines de panne restent donc
        # disjoints : un échec Mistral n'arrête pas l'extraction, ni l'inverse.
        if os.environ.get("OTO_FILE_EXTRACT_WORKER_ENABLED", "1") != "0":
            from . import file_extract_worker
            _bg_loops.append(file_extract_worker.run_extract_loop)
        # Vecteurs de classement (#318) : remplit par tranches, puis RÉCONCILIE (elle
        # ne s'arrête pas). Hors du boot par nécessité — la variante auto-remplissante
        # tenait `datastore_rows` 7,55 s sous verrou exclusif, en pleine production.
        if os.environ.get("OTO_RANK_BACKFILL_ENABLED", "1") != "0":
            from . import rank_backfill_worker
            _bg_loops.append(rank_backfill_worker.run_rank_backfill_loop)
        # Le tick des déclencheurs du runner (chantier R3) : une horloge qui ENFILE
        # des jobs à l'échéance — jamais d'exécution (le worker externe claime).
        # Concurrent-sûr par CAS sur next_due : prod et preprod partagent la base,
        # deux ticks tournent, un seul gagne chaque échéance.
        from . import runner_tick
        if runner_tick.enabled():
            _bg_loops.append(runner_tick.run_runner_tick_loop)
        from . import billing as _billing
        if _billing.is_enabled() and os.environ.get("OTO_BILLING_RUNNER_ENABLED", "1") != "0":
            # échéances d'abonnement + réconciliation (ADR 0043) — gaté sur le
            # feature flag billing (dormant en prod) + no-op sans MOLLIE_API_KEY.
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

        root_app = build_root_app(app, anon_app)

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
