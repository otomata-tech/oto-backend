"""Primitives partagées par TOUS les modules de routes REST (`api_routes*.py`).

Ce module ne déclare **aucune** route : il porte ce que les handlers de tous les
domaines appellent — l'authentification (`_authenticate`), les en-têtes CORS, les
deux fabriques de réponse JSON, `_file` (toute réponse qui n'est PAS du JSON — elle
passe par là pour ne pas sortir sans CORS, cf. son docstring), le préflight
`OPTIONS`, et `bind` (le passeur de dépendances explicites).

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

import functools
from typing import Awaitable, Callable

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from .. import config, db
from ..auth import platform_worker, service_identity, token_scopes
from .. import verrou_org
from ..tenant_migration import alias_drain_armed
from .. import account_suspension

# Signature de `_authenticate`, telle que la consomment les modules de routes.
AuthFn = Callable[..., Awaitable["tuple[str | None, JSONResponse | None]"]]


def en_thread(handler):
    """Route dont le corps parle à la base : il tourne dans le threadpool, jamais sur la boucle.

    Le serveur est mono-loop (`docs/event-loop-perf.md`) : un `async def` qui appelle du SQL
    synchrone gèle TOUT le processus le temps de la requête — et une route publique, sans
    jeton, est celle qu'un tiers peut marteler. On écrit donc le corps en `def` synchrone
    (rien à awaiter dedans), et ce décorateur le sert comme une coroutine : l'objet reste
    un `async def` pour Starlette, pour `route.endpoint is …` et pour qui l'attend
    (`asyncio.run(route(req))`), et son nom, sa doc et sa signature sont conservés.

    Une route qui doit AUSSI awaiter (authentification, flux) n'entre pas ici : elle
    garde son `async def` et confie ses lectures à `run_in_threadpool` elle-même.
    """
    @functools.wraps(handler)
    async def route(*args, **kwargs):
        return await run_in_threadpool(handler, *args, **kwargs)
    return route


def _allowed_origins() -> list[str]:
    """Les origines que CETTE instance déclare (`config.cors_origins`, #968) — aucune
    liste en dur : lue à chaque requête, donc un changement d'environnement prend effet
    au redémarrage sans redéploiement."""
    return config.cors_origins()


def _cors_headers(origin: str | None) -> dict[str, str]:
    if origin and origin in _allowed_origins():
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Oto-Org, X-Oto-Group, X-Oto-View-As, X-Oto-View-As-Write, X-Oto-Run",
            # Sans cette ligne, `X-Oto-Version` (oto#33) part sur le fil mais reste
            # ILLISIBLE au dashboard : un navigateur ne donne à `fetch` que les
            # en-têtes de réponse explicitement exposés. Un en-tête qu'aucun de nos
            # consommateurs ne peut lire ne date rien.
            #
            # `Content-Disposition` pour la même raison, mesurée le 2026-09-09 : les
            # réponses fichier (PDF de facture, export ZIP) portent leur nom de
            # fichier LÀ et nulle part ailleurs. Non exposé, le front télécharge un
            # document qu'il ne peut pas nommer.
            "Access-Control-Expose-Headers": "X-Oto-Version, Content-Disposition",
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
    """Applique le « voir en tant que » (axe user, REST) : si un sub de consultation
    est posé pour la requête (par ViewAsMiddleware, qui a DÉJÀ validé opérateur +
    cible, et soit une lecture, soit une écriture ACCEPTÉE par un super_admin),
    renvoie ce sub cible ; sinon le sub réel. `apply_view_as`
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


def _id_du_tableau(sub: str, adresse: str):
    """L'identifiant du tableau que `adresse` (un nom) désigne pour `sub` — `None` s'il
    n'en désigne aucun, ou plusieurs : un nom qui ne résout pas n'ouvre rien."""
    from ..datastore.core import DatastoreAmbigu, DatastoreNotFound, make_store
    try:
        return make_store(sub)._resolve(adresse)
    except (DatastoreNotFound, DatastoreAmbigu):
        return None


async def _authenticate(
    request: Request,
    verifier: JWTVerifier,
    *,
    allow_query_token: bool = False,
    apply_view_as: bool = True,
    allow_api_token: bool = True,
    allow_service: bool = False,
) -> tuple[str | None, JSONResponse | None]:
    """Résout l'appelant (JWT Logto **ou** jeton API `oto_`) et **garde la portée**.

    `allow_api_token=False` = route réservée à une **session interactive** : un
    porteur de jeton y est refusé. Réservé à la gestion des jetons eux-mêmes — un
    jeton qui peut en créer d'autres rend sa fuite auto-entretenue (révoquer le
    jeton fuité ne suffit plus, l'attaquant s'en est fait un second, non-expirant).

    `allow_service=True` = la route accepte une identité de SERVICE
    (`auth.service_identity`). Faux par défaut : seule une capacité dont la règle
    lit ce principal l'ouvre (`_rest_adapter`).
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

    # Secret de MACHINE d'un worker de plateforme (`otow_`) : pas un compte.
    # Aucune ligne `users`, aucune org, aucune pause à vérifier, aucun view-as —
    # rien de ce qui s'applique à une identité. Le principal publié est son
    # `worker_sub`, et la variable de contexte dit à la règle d'autorisation
    # que CETTE requête est celle d'un worker. Posée à None d'abord : elle ne
    # survit jamais d'une requête à l'autre par oubli.
    platform_worker.set_current(None)
    service_identity.set_current(None)
    # Le verrou d'org d'un jeton de délégation (`verrou_org.py`) : posé à None
    # d'abord, comme les deux variables ci-dessus — il ne survit à aucune requête.
    verrou_org.poser(None)
    if token.startswith(db.WORKER_SECRET_PREFIX):
        token_scopes.set_current(None)
        if not allow_api_token:
            return None, _json_error(
                request, 403, "api_token_forbidden",
                "La gestion des jetons demande une session interactive (JWT).")
        row = await run_in_threadpool(db.verify_worker_secret, token)
        if not row:
            return None, _json_error(request, 401, "invalid_worker_secret")
        platform_worker.set_current(row)
        _publier_principal(request, row["worker_sub"], token_kind="worker")
        return row["worker_sub"], None

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
        # Une portée émise avant qu'elle ne nomme les tableaux par identifiant n'ouvre
        # plus rien, et le DIT : la juger sur des noms rouvrirait ce qu'un renommage
        # déplace (oto#158). Elle se migre, elle ne se devine pas.
        if (anciens := token_scopes.noms_de_tableau(scopes)):
            return None, _json_error(
                request, 403, "token_scope_by_name",
                f"La portée de ce jeton nomme des tableaux par leur NOM "
                f"({', '.join(anciens)}) : une portée nomme un tableau par son "
                "IDENTIFIANT, et celle-ci n'ouvre donc plus ces tableaux. Le jeton doit "
                "être réémis avec les identifiants (`data_list_datastores`).")
        # Un NOM de tableau dans le chemin est jugé sur l'identifiant qu'il résout,
        # pour le porteur — le temps du préavis (`deprecations.RETRAIT_NOM_DE_TABLEAU`).
        adresse = token_scopes.tableau_du_chemin(request.method, request.url.path)
        id_du_tableau = (await run_in_threadpool(_id_du_tableau, row["sub"], adresse)
                         if scopes and adresse and not adresse.isdigit() else None)
        if not token_scopes.authorize(scopes, request.method, request.url.path,
                                      id_du_tableau):
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
            elif cause == "ecriture":
                detail = (
                    "Ouvrir ou clore un run demande un jeton qui ÉCRIT au moins un "
                    f"tableau : un run ne sert qu'à réserver puis écrire des lignes. {ouvre}")
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
        verrou_org.poser(verrou := verrou_org.depuis_ligne(row))
        servi = _maybe_view_as(row["sub"], apply_view_as)
        # Le verrou ne vise que le porteur : « voir en tant que » un autre compte
        # l'éteindrait. Sous un jeton verrouillé, la consultation est refusée.
        if verrou is not None and servi != row["sub"] and verrou_org.courant():
            return None, _json_error(
                request, 403, verrou_org.CODE,
                "A hosted agent's token cannot view as another account.")
        _publier_principal(request, row["sub"],
                           token_id=row.get("token_id"),
                           token_kind=row.get("token_kind"))
        return servi, None

    # Sinon, JWT Logto — jamais de portée de jeton.
    token_scopes.set_current(None)

    # Client MACHINE d'un service (oto-backend#1068) : reconnu à l'audience qu'il
    # revendique, puis vérifié pour de vrai. Comme le worker : aucune ligne `users`,
    # aucune pause, aucun view-as — un identifiant vérifié, pas un compte.
    if service_identity.adresse_aux_services(token):
        if not allow_service:
            return None, _json_error(
                request, 403, "service_forbidden",
                "Cette route n'est pas ouverte à une identité de service.")
        try:
            principal = await service_identity.verifier_jeton(token)
        except service_identity.ServiceRefuse as refus:
            return None, _json_error(request, refus.status, refus.code, refus.detail)
        service_identity.set_current(principal)
        _publier_principal(request, principal["sub"], token_kind="service")
        return principal["sub"], None

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


def _json(request: Request, payload: dict, status: int = 200,
          extra_headers: dict[str, str] | None = None) -> JSONResponse:
    headers = _cors_headers(request.headers.get("origin"))
    if extra_headers:
        headers.update(extra_headers)
    return JSONResponse(payload, status_code=status, headers=headers)


def _file(request: Request, content, *, media_type: str,
          filename: str | None = None,
          headers: dict[str, str] | None = None,
          status_code: int = 200) -> Response:
    """La réponse qui n'est PAS du JSON — un PDF, un ZIP, une icône, du markdown.

    **Le seul chemin.** Une `Response` construite à la main sort sans CORS, et le
    navigateur la refuse. Mesuré le 2026-09-09 :
    `GET /api/me/billing/invoices/{id}/pdf` répondait 200 en production et le
    dashboard n'en voyait rien — « No Access-Control-Allow-Origin ». Le CORS de ce
    serveur se pose réponse par réponse (aucun `CORSMiddleware`, cf. `_cors_headers`)
    et seuls `_json`, `_json_error` et `options_handler` le posaient : le préflight
    passait, les erreurs JSON passaient, **le 200 qui porte le fichier sortait nu**.
    Trois routes avaient fait le même oubli, chacune de son côté — le défaut était
    dans la FAÇON de poser le CORS, pas dans les trois appels.
    `tests/api/test_reponses_binaires_cors.py` refuse le quatrième.

    `filename` compose le `Content-Disposition` ICI parce que ce nom vient d'une
    donnée d'amont (un numéro de facture, un nom de projet) et atterrit dans un
    en-tête : le filtre CR/LF s'applique une fois, pour tous — même réflexe que
    `email._no_crlf`. Un nom entièrement vidé par le filtre retombe sur `fichier` :
    une pièce jointe garde un nom, elle ne perd pas son en-tête en silence.
    """
    entetes = dict(headers or {})
    entetes.update(_cors_headers(request.headers.get("origin")))
    if filename is not None:
        nom = "".join(c for c in filename if c not in '"\r\n\x00') or "fichier"
        entetes["Content-Disposition"] = f'attachment; filename="{nom}"'
    return Response(content, media_type=media_type, status_code=status_code,
                    headers=entetes)


def _file_stream(request: Request, content, *, media_type: str,
                 filename: str | None = None,
                 headers: dict[str, str] | None = None,
                 status_code: int = 200) -> StreamingResponse:
    """`_file`'s streaming twin — même CORS, même filtre de nom de fichier ; seule
    différence : `content` est un itérable ASYNC de bytes plutôt qu'un corps déjà
    en mémoire, pour une réponse trop grosse pour tenir d'un bloc (l'export CSV
    d'un tableau).

    Une fonction à part plutôt qu'un paramètre optionnel sur `_file` : les deux
    prennent une forme de corps différente, et confondre les deux signatures à
    l'appel (un générateur passé où `_file` attend des bytes, ou l'inverse)
    échouerait tard et loin de sa cause. `test_aucune_reponse_fichier_ne_se_
    construit_hors_de_base_file` (`tests/api/test_reponses_binaires_cors.py`)
    exige que toute construction de réponse fichier passe par CE module — cette
    fonction est ce que `datastore_export.py` appelle pour ne pas y échapper."""
    entetes = dict(headers or {})
    entetes.update(_cors_headers(request.headers.get("origin")))
    if filename is not None:
        nom = "".join(c for c in filename if c not in '"\r\n\x00') or "fichier"
        entetes["Content-Disposition"] = f'attachment; filename="{nom}"'
    return StreamingResponse(content, media_type=media_type, status_code=status_code,
                             headers=entetes)


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

    # Nom, qualname, doc, module — et les ATTRIBUTS du handler (`__dict__`), dont le
    # `contrat` d'une route de nature (`ContratDeRoute`, oto#106) : sans eux, le
    # document publiait une route liée par `bind` en souche « écrite à la main »
    # (#655). `__wrapped__` mène au vrai corps (garde des refus atteignables).
    return functools.update_wrapper(endpoint, handler)
