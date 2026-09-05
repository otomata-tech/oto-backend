"""Primitives partagées par TOUS les modules de routes REST (`api_routes*.py`).

Ce module ne déclare **aucune** route : il porte ce que les handlers de tous les
domaines appellent — l'authentification (`_authenticate`), les en-têtes CORS, les
deux fabriques de réponse JSON, le préflight `OPTIONS`, et `bind` (le passeur de
dépendances explicites).

**Pourquoi un module à part plutôt que `api/routes.py`.** Depuis la découpe du
2026-08-27, les handlers vivent dans des `api_routes_<domaine>.py` que
`api/routes.py` importe pour assembler la table. S'ils allaient rechercher
`_authenticate` dans `api.routes`, l'import serait circulaire ; la base est donc
sous eux, jamais au-dessus. `api.routes` **ré-exporte** ces noms — `api.routes._authenticate`
et `api.routes._cors_headers` restent valides pour les appelants (et les tests)
d'avant la découpe.

Les dix modules de routes ANTÉRIEURS à la découpe (`api/datastore.py`,
`api/sirene.py`, …) reçoivent encore ces mêmes fonctions en PARAMÈTRES de
leur `make_routes` — c'est leur patron historique, né du même besoin d'éviter le
cycle. Il n'a pas été touché : les convertir serait un second lot, sans effet sur
ce qui est servi.
"""
from __future__ import annotations

import os
from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .. import db
from ..auth import token_scopes
from ..tenant_migration import alias_drain_armed
from .. import account_suspension

# Signature de `_authenticate`, telle que la consomment les modules de routes.
AuthFn = Callable[..., Awaitable["tuple[str | None, JSONResponse | None]"]]


def _allowed_origins() -> list[str]:
    raw = os.environ.get("OTO_MCP_CORS_ORIGINS")
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return [
        "https://oto.cx",                   # domaine marketing canonique (cutover ADR 0040)
        "https://www.oto.cx",
        "https://manage.oto.cx",            # oto-dashboard PROD (cutover ADR 0040)
        "https://oto.ninja",                # preprod/canari + redirections
        "https://www.oto.ninja",
        "https://app.oto.ninja",
        # noqa: CLIENT — origines FONCTIONNELLES d'un front tiers (repli seulement :
        # les deux box posent OTO_MCP_CORS_ORIGINS, cf. CLAUDE.md). Les retirer casse
        # le CORS d'un dev sans env. Relocalisation = 2e volet de oto-private#85.
        "https://app.tulina.ai",            # noqa: CLIENT — front tiers PROD
        "https://tulina.oto.zone",          # noqa: CLIENT — front tiers PREPROD
        "http://localhost:5173",
        "http://localhost:4173",
        "http://localhost:5182",
        "http://localhost:5184",
        "http://localhost:5192",            # oto-dashboard dev (ADR 0007)
        "http://localhost:5193",            # front tiers en dev, ports alternatifs
        "http://localhost:5194",
        "http://localhost:5195",
        "http://localhost:5196",
        "https://dashboard.otoninja.dev",   # oto-dashboard via Caddy local
        "https://dashboard.oto.ninja",      # oto-dashboard prod
    ]



def _cors_headers(origin: str | None) -> dict[str, str]:
    if origin and origin in _allowed_origins():
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Oto-Org, X-Oto-Group, X-Oto-View-As",
            # Sans cette ligne, `X-Oto-Version` (oto#33) part sur le fil mais reste
            # ILLISIBLE au dashboard : un navigateur ne donne à `fetch` que les
            # en-têtes de réponse explicitement exposés. Un en-tête qu'aucun de nos
            # consommateurs ne peut lire ne date rien.
            "Access-Control-Expose-Headers": "X-Oto-Version",
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
        }
    return {}


def _locale_from_accept_language(header: str | None) -> str | None:
    """Déduit `en`/`fr` du 1er tag de langue de l'en-tête `Accept-Language`.

    Même repli que le dashboard (`i18n.ts:detectBrowserLocale`, `navigator.language`) :
    `fr` si la langue commence par `fr`, sinon `en`. `None` si l'en-tête est absent ou
    vide — l'appelant ne pose alors rien (oto-backend#701 : c'est le seul signal
    disponible côté REST interactif, jamais une déduction depuis le domaine email ou
    une autre heuristique)."""
    if not header:
        return None
    primary = header.split(",", 1)[0].split(";", 1)[0].strip().lower()
    if not primary:
        return None
    return "fr" if primary.startswith("fr") else "en"


def _maybe_view_as(real_sub: str, apply_view_as: bool) -> str:
    """Applique le « voir en tant que » (axe user, REST lecture seule) : si un sub
    de consultation est posé pour la requête (par ViewAsMiddleware, qui a DÉJÀ validé
    opérateur + cible + GET), renvoie ce sub cible ; sinon le sub réel. `apply_view_as`
    False = chemin du middleware lui-même (qui doit voir le sub RÉEL pour gater)."""
    if not apply_view_as:
        return real_sub
    from .. import session_org
    target = session_org.current_view_user()
    return target if (target and target != real_sub) else real_sub


# Clé du principal résolu, déposée dans le `scope` ASGI — le MÊME dict que celui
# du middleware de journal, qui le relit dans son `finally`.
#
# ⚠️ Pourquoi le publier au lieu de le redéduire : le middleware ne voit que
# l'en-tête, et il n'en tire un compte QUE si le bearer est un JWT
# (`_claimed_sub` : trois parts). Tout appel par jeton API ou par jeton de
# délégation s'écrivait donc SANS compte — anonyme dans le seul journal où l'on
# va chercher qui a fait quoi. L'authentification, elle, résout le porteur pour
# de vrai ; il suffisait de ne pas jeter ce qu'elle avait déjà en main.
CLE_PRINCIPAL = "oto_principal"


def _publier_principal(request: Request, sub: str, *,
                       token_id: int | None = None,
                       token_kind: str | None = None) -> None:
    """Dépose dans le scope QUI a été authentifié, pour le journal.

    `sub` = le porteur RÉEL du bearer — celui qui s'est authentifié, jamais la
    cible d'un « en tant que ».

    ⚠️ Le view-as N'EST PAS journalisé, et surtout pas dans `effective_sub` : le
    schéma en fait le compte relu APRÈS le handler, dont toute divergence d'avec
    `sub` EST un défaut (elle trahirait une réponse servie sous une autre
    identité). Y écrire une consultation rendrait normale la divergence que cette
    colonne existe pour dénoncer. Le journal dit donc QUI A PRÉSENTÉ le bearer —
    c'est ce qu'on cherche quand on demande « qui a fait ça ».

    ⚠️ Le jeton lui-même n'entre JAMAIS ici : on nomme son identifiant, pas sa
    valeur.
    """
    request.scope[CLE_PRINCIPAL] = {
        "sub": sub, "token_id": token_id, "token_kind": token_kind,
    }


async def _authenticate(
    request: Request,
    verifier: JWTVerifier,
    *,
    allow_query_token: bool = False,
    apply_view_as: bool = True,
    allow_api_token: bool = True,
) -> tuple[str | None, JSONResponse | None]:
    """Résout l'appelant (JWT Logto **ou** jeton API `oto_`) et **garde la portée**.

    `allow_api_token=False` = route réservée à une **session interactive** : un
    porteur de jeton y est refusé. Réservé à la gestion des jetons eux-mêmes — un
    jeton qui peut en créer d'autres rend sa fuite auto-entretenue (révoquer le
    jeton fuité ne suffit plus, l'attaquant s'en est fait un second, non-expirant).
    """
    auth = request.headers.get("authorization", "")
    token: str | None = None
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    elif allow_query_token:
        # Fallback pour SSE via EventSource (qui n'autorise pas les headers).
        token = request.query_params.get("token")
    if not token:
        return None, _json_error(request, 401, "missing_bearer")

    # API token long-lived (CLI) : préfixe `oto_` → lookup hash en DB.
    # Pas de upsert_user ici : la FK CASCADE garantit que si la row user a
    # été supprimée, le token a été supprimé avec.
    if token.startswith("oto_"):
        if not allow_api_token:
            token_scopes.set_current(None)
            return None, _json_error(
                request, 403, "api_token_forbidden",
                "La gestion des jetons demande une session interactive (JWT) — "
                "un jeton API ne peut ni lister, ni créer, ni révoquer de jeton.")
        # DB HORS de la loop (threadpool) : un blip DB ne doit jamais geler le
        # serveur mono-loop entier (vécu 2026-07-02, py-spy : getconn wait ici).
        row = await run_in_threadpool(db.verify_api_token, token)
        if not row:
            token_scopes.set_current(None)
            return None, _json_error(request, 401, "invalid_api_token")
        # Portée du jeton (`token_scopes`) : posée à CHAQUE requête (None comprise),
        # puis gate deny-by-default. Un jeton non porté (`scopes` NULL) est inchangé.
        scopes = row.get("scopes")
        token_scopes.set_current(scopes)
        if not token_scopes.authorize(scopes, request.method, request.url.path):
            granted = []
            if token_scopes.namespaces(scopes):
                granted.append(f"les tableaux {sorted(token_scopes.namespaces(scopes))}")
            if token_scopes.projects(scopes):
                granted.append(f"les projets {sorted(token_scopes.projects(scopes))}")
            # ⚠️ Le refus DIT LEQUEL des deux cas, sinon il se contredit : il
            # listait les tableaux ouverts même quand le tableau demandé en
            # faisait partie — et le lecteur en concluait que son jeton était
            # cassé. Un refus qui nomme comme autorisé ce qu'il refuse est pire
            # que pas de détail du tout.
            cause, quoi = token_scopes.motif_du_refus(
                scopes, request.method, request.url.path)
            ouvre = f"Ce jeton ouvre {' et '.join(granted) or 'rien'}."
            if cause == "geste":
                # La liste RESTE — elle coûte une session de debug à l'intégrateur
                # quand elle manque — mais elle vient APRÈS la cause, et la cause
                # dit que le tableau n'y est pour rien.
                detail = (
                    "Ce geste n'est ouvert à AUCUN jeton porté, quelle que soit sa "
                    "portée : gouvernance d'un tableau (créer, supprimer, renommer, "
                    "partager) et tout ce qui sort du datastore. Il demande une "
                    f"session interactive du propriétaire. {ouvre}")
            else:
                detail = (
                    f"« {quoi} » n'est pas dans la portée de ce jeton, ou pas avec "
                    f"le droit qu'exige ce geste. {ouvre}")
            return None, _json_error(request, 403, "token_scope_forbidden", detail)
        # Compte en PAUSE : un jeton `oto_` ne porte aucune expiration obligatoire,
        # donc c'est ici que la pause serait la plus facilement contournée si on ne
        # la vérifiait qu'au login — il n'y a pas de login sur ce chemin. La garde
        # porte sur le PORTEUR du jeton, avant `_maybe_view_as` : un opérateur qui
        # consulte « en tant que » un compte en pause doit pouvoir le faire, c'est
        # même le premier geste de diagnostic après une mise en pause.
        if (pause := await run_in_threadpool(account_suspension.refus, row["sub"])):
            return None, _json_error(request, 403, account_suspension.CODE, pause[0])
        servi = _maybe_view_as(row["sub"], apply_view_as)
        _publier_principal(request, row["sub"],
                           token_id=row.get("token_id"),
                           token_kind=row.get("token_kind"))
        return servi, None

    # Sinon, JWT Logto (session interactive) — jamais de portée de jeton.
    token_scopes.set_current(None)
    access_token = await verifier.verify_token(token)
    if not access_token or not getattr(access_token, "claims", None):
        return None, _json_error(request, 401, "invalid_token")
    sub = access_token.claims.get("sub")
    if not sub:
        return None, _json_error(request, 401, "missing_sub")
    # Drain d'alias (B1) : canonicaliser le sub AVANT l'upsert. ⚠️ L'`upsert_user` qui
    # suit n'est PAS sous commande : un vieux sub non redirigé n'échoue pas ici, il
    # RECRÉE le compte supprimé par la fusion. Le drain est donc porteur — il ne
    # s'arrête pas parce que le rapprochement, lui, a cessé de servir.
    if alias_drain_armed():
        sub = await run_in_threadpool(db.resolve_sub, sub)
    # Compte en PAUSE : le refus tombe ici, AVANT l'upsert — un compte neutralisé
    # n'écrit plus rien, pas même le rafraîchissement de son adresse. Et il tombe à
    # CHAQUE requête, pas au login : le jeton qu'il porte a été émis avant la pause
    # et reste signé jusqu'à son expiration ; une pause vérifiée à la connexion
    # laisserait une heure de sursis à ce qu'elle est censée arrêter.
    if (pause := await run_in_threadpool(account_suspension.refus, sub)):
        return None, _json_error(request, 403, account_suspension.CODE, pause[0])
    # upsert_user = DB à CHAQUE requête REST → threadpool (jamais dans la loop).
    # locale (#701) : signal déduit de l'en-tête, jamais un choix — `upsert_user`
    # ne le pose que si la ligne n'en porte encore aucun (COALESCE côté SQL).
    try:
        await run_in_threadpool(
            lambda: db.upsert_user(
                sub, email=access_token.claims.get("email"),
                name=access_token.claims.get("name"),
                locale=_locale_from_accept_language(request.headers.get("accept-language"))))
    except db.CompteEnPause as refus:
        # L'ANCIEN identifiant d'un compte mis en pause. Il n'a pas de ligne à lui
        # (la fusion l'a supprimée), donc la garde ci-dessus ne l'a pas vu : c'est
        # `upsert_user` qui reconnaît, au moment de le RECRÉER, que son alias mène à
        # un compte neutralisé. Sans ce refus, le porteur repartirait avec un compte
        # neuf et un espace personnel neuf — la résurrection déjà vécue.
        return None, _json_error(request, 403, db.CompteEnPause.code, str(refus))
    servi = _maybe_view_as(sub, apply_view_as)
    # Session interactive : pas de jeton nommé, le porteur suffit. Publié quand
    # même — sinon le journal continuerait de le RE-DÉDUIRE de l'en-tête, et une
    # seule des deux formes de bearer serait attribuée.
    _publier_principal(request, sub)
    return servi, None


def _json_error(request: Request, status: int, code: str,
                detail: str | None = None,
                details: dict | None = None) -> JSONResponse:
    """L'enveloppe d'erreur REST : `error` (jeton machine), `detail` (la phrase),
    et `details` — la forme STRUCTURÉE du refus quand il y en a une (ADR 0009 :
    `AuthzDenied.details`). Additive : une erreur qui n'en pose pas rend exactement
    le corps d'avant, et aucun client n'a à connaître la clé pour lire les autres."""
    payload = {"error": code}
    if detail:
        payload["detail"] = detail
    if details:
        payload["details"] = details
    return JSONResponse(
        payload,
        status_code=status,
        headers=_cors_headers(request.headers.get("origin")),
    )


def _json(request: Request, payload: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(
        payload, status_code=status, headers=_cors_headers(request.headers.get("origin"))
    )


async def options_handler(request: Request) -> Response:
    return Response(status_code=204, headers=_cors_headers(request.headers.get("origin")))


def bind(handler: Callable[..., Awaitable[Response]], **deps):
    """Fige les dépendances explicites d'un handler de module en un endpoint
    Starlette `(request) -> Response`.

    Les handlers étaient des CLOSURES de `make_routes` : ils lisaient `verifier` et
    `mcp_instance` dans la portée englobante. Devenus fonctions de module, ils les
    reçoivent en paramètres nommés — et `bind` est le seul endroit où ces paramètres
    sont fournis, à l'assemblage. Rien n'est posé en global : deux appels de
    `make_routes` avec deux verifiers différents restent indépendants.

    ⚠️ **Pas `functools.partial`** : Starlette teste `inspect.isfunction(endpoint)`
    pour choisir entre « handler de requête » et « app ASGI brute ». Un `partial`
    tombe du mauvais côté et la route cesse de répondre. La fonction interne reprend
    le `__name__` du handler pour que `route.name` (donc `url_for`) reste identique
    à celui d'avant la découpe.
    """
    async def endpoint(request: Request):
        return await handler(request, **deps)

    endpoint.__name__ = handler.__name__
    endpoint.__qualname__ = handler.__qualname__
    endpoint.__doc__ = handler.__doc__
    endpoint.__module__ = handler.__module__
    return endpoint
