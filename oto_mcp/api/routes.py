"""ASSEMBLAGE de l'API REST `/api/*` — la table de routes, et rien d'autre.

Depuis la découpe du 2026-08-27, ce fichier ne contient plus de handler : il
**monte**. `make_routes` fait trois choses, dans cet ordre — appeler les
`make_routes` des modules de routes historiques, monter la couche capacité
(ADR 0009, deux faces générées d'un descripteur unique), et rendre la table
ordonnée des chemins écrits à la main. L'ORDRE de cette table est un contrat
(Starlette prend le PREMIER match : `…/tools/registry` doit précéder
`…/tools/{name}`), donc elle se lit d'un seul endroit.

Les handlers vivent par DOMAINE, chacun une fonction de module appelable seule :

| module                     | domaine                                              |
| -------------------------- | ---------------------------------------------------- |
| `api/base.py`       | primitives partagées (auth, CORS, JSON, OPTIONS, `bind`) |
| `api/public.py`     | surfaces sans auth : favicon, catalogues, bibliothèques, invitations, docs partagés |
| `api/media.py`      | avatar user, logo d'org (multipart)                   |
| `api/projects.py`   | fichiers bruts d'un projet, export ZIP                |
| `api/uploads.py`    | réception d'un upload signé (`/api/upload/{token}`)   |

Les modules ANTÉRIEURS à la découpe gardent leur forme : datastore, sirene,
accords, zoho, salesforce, billing — ils exposent un
`make_routes(...)` qui reçoit les primitives en paramètres. (`api/connectors.py` a
disparu le 2026-08-29 avec sa dernière route, le webhook de liaison messagerie :
dormant depuis la v2 du fournisseur, #581 ; `api/atlassian.py` et `api/folk.py` le
2026-09-09 avec la fédération MCP, ADR 0069.)

Ce fichier garde aussi les deux MIDDLEWARES ASGI de la face REST, dont l'ordre de
pose (dans `server.py`) est un contrat dont dépendent des colonnes de monitoring :
`ViewAsMiddleware` (org/équipe/user de consultation, ADR 0023) et `RestCallLogger`
(une ligne `tool_calls(kind='rest')` par requête, ADR 0017).

Le reste du palier ORG (`/api/me/orgs`, `/api/orgs/*`, `/api/admin/orgs/*`) est
100 % en capacités depuis la migration qui a supprimé `api_routes_orgs.py` — ce
docstring y renvoyait encore le 2026-08-27, vers un fichier qui n'existe plus.

Auth : Bearer JWT Logto **ou** jeton API long-lived (préfixe `oto_`), vérifié par
`api_routes_base._authenticate` (ré-exporté ici). CORS : les origines
que l'instance déclare (`OTO_MCP_CORS_ORIGINS`), `_allowed_origins`.
"""
from __future__ import annotations

from typing import Iterable

import asyncio
import base64
import json
import logging
import re
import time

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.concurrency import run_in_threadpool

from .. import db, journal_secrets, tenancy
from . import (accords as api_routes_accords,
               billing as api_routes_billing,
               datastore as api_routes_datastore,
               hooks as api_routes_hooks,
               instagram_meta as api_routes_instagram_meta,
               receveurs as api_routes_receveurs,
               salesforce as api_routes_salesforce,
               sirene as api_routes_sirene,
               zoho as api_routes_zoho)
from ..capabilities import _rest_adapter as _cap_rest_adapter
from ..capabilities import registry as _cap_registry
from ..capabilities._authz import CODE_HORS_VUE
# Primitives partagées (auth, CORS, réponses JSON, préflight, `bind`) : elles ont
# quitté ce fichier pour `base.py` le 2026-08-27, sous les modules de
# domaine qui les appellent (sinon l'import serait circulaire). RÉ-EXPORTÉES ici :
# `api.routes._authenticate` / `_cors_headers` / `_json` … restent valides.
from . import base as api_base
from .base import (  # noqa: F401 — ré-export de compatibilité
    AuthFn, _allowed_origins, _authenticate, _cors_headers, _json, _json_error,
    _maybe_view_as, bind, options_handler)
# Handlers par DOMAINE (découpe du 2026-08-27) : chaque module porte des fonctions
# de module, testables seules ; la table de routes ci-dessous reste ici.
from . import alias_routes, public
from . import datastore_export
from . import media
from . import projects
from . import transcription as api_transcription
from . import uploads

logger = logging.getLogger(__name__)




# ── View-as (ADR 0023) : consultation d'une org dans le dashboard ───────────
def _parse_view_org(request: Request) -> int | None:
    """Org de consultation (header `X-Oto-Org`). None = pas de header ; 0 = perso ;
    >0 = id d'org. Header mal formé → None (repli maison, jamais d'erreur dure)."""
    raw = request.headers.get("x-oto-org")
    if raw is None:
        return None
    v = raw.strip().lower()
    if v in ("", "0", "perso", "personal"):
        return 0
    try:
        n = int(v)
        return n if n > 0 else 0
    except ValueError:
        return None


def _parse_view_group(request: Request) -> int | None:
    """Équipe de consultation (header `X-Oto-Group`). None = pas de header / niveau
    org ; >0 = id de groupe. Pas de sentinelle perso (l'absence = niveau org)."""
    raw = request.headers.get("x-oto-group")
    if raw is None:
        return None
    try:
        n = int(raw.strip())
        return n if n > 0 else None
    except ValueError:
        return None


def _parse_view_user(request: Request) -> str | None:
    """User de consultation (« voir en tant que », header `X-Oto-View-As` = sub cible).
    None = pas de header. Validé (opérateur + cible existe + GET) dans le middleware."""
    raw = request.headers.get("x-oto-view-as")
    if raw is None:
        return None
    return raw.strip() or None


def _parse_view_write(request: Request) -> bool:
    """Geste d'ACCEPTATION de l'écriture en « voir en tant que » (header
    `X-Oto-View-As-Write: 1`). Sans lui, une consultation reste en lecture seule.
    Validé (super_admin + cible appliquée) dans le middleware, jamais cru seul."""
    return (request.headers.get("x-oto-view-as-write") or "").strip() == "1"


# Ops de LECTURE des endpoints op-aware (POST `{op:…}`). Le dashboard LIT en POST
# (`{op:'list'}`, `{op:'get'}`, …) — une garde par méthode HTTP bloquerait donc les
# lectures. En consultation LECTURE SEULE (view-as user / inspection org opérateur),
# seules ces ops passent sur une requête non-GET ; toute autre op — ou un POST/PUT/
# DELETE sans op (= action/upload) — est une écriture, rejetée. Deny-by-default.
# ⚠️ La liste est par NOM d'op, commune à toutes les routes : chaque op de chaque
# capacité op-aware est classée lecture OU écriture dans
# `tests/test_readonly_op_guard.py`, qui rougit sur une op neuve non classée, sur un
# nom qui serait lecture ici et écriture là, et sur une entrée que plus rien ne sert
# (oto#221 : `fleets op=state`, une lecture, était refusée en consultation).
_READ_OPS = frozenset({
    "list", "get", "search", "revisions", "inventory", "list_templates", "preview",
    "state", "activity", "runs", "lint", "handoff", "backlinks", "shared_with_me",
    "deliveries", "versions", "read", "audience", "journal", "optouts",
})


async def _peek_op(receive):
    """Bufferise le corps de la requête, en extrait le champ `op` (JSON), et rend un
    `receive` qui REJOUE le corps intact au handler aval. Les routes `/api/*` sont de
    petites requêtes JSON → bufferiser est sûr (`/mcp` streaming est exclu en amont).
    Retourne `(op | None, receive_rejoué)`."""
    messages: list = []
    while True:
        msg = await receive()
        messages.append(msg)
        if msg.get("type") != "http.request" or not msg.get("more_body", False):
            break
    body = b"".join(m.get("body", b"") for m in messages if m.get("type") == "http.request")
    op = None
    if body:
        try:
            data = json.loads(body)
            op = data.get("op") if isinstance(data, dict) else None
        # noqa: SILENT — corps non-JSON légitime sur /mcp : la sonde d'op n'a rien à dire
        except Exception:
            op = None
    i = 0

    async def replay():
        nonlocal i
        if i < len(messages):
            m = messages[i]
            i += 1
            return m
        return {"type": "http.request", "body": b"", "more_body": False}

    return op, replay


# ── Vue BORNÉE d'un org_admin (oto#270) ─────────────────────────────────────
# Un org_admin « voit en tant que » un membre de SON org O, en lecture seule, pour
# vérifier ce que ce membre voit (onboarding). Même middleware, même garde de lecture
# seule, même journal que la vue d'opérateur ; en plus, la vue est BORNÉE à O :
# - O est obligatoire (`X-Oto-Org`, ou l'org de `X-Oto-Group`), l'appelant en est
#   admin RÉEL, la cible membre RÉEL, et n'est pas opérateur plateforme (ses droits
#   déborderaient toute org) ;
# - aucune écriture, jamais : l'écriture acceptée reste au super_admin ;
# - seules les LECTURES listées ci-dessous passent — liste fermée, refus nommé
#   (`view_as_hors_org`) pour tout le reste, jamais servi « au cas où » ;
# - `/api/orgs/{id}` et `/api/groups/{id}` sont épinglés sur O ;
# - dans la requête, `session_org.current_view_as_bound_org()` vaut O : le seam
#   `ownership` et l'adaptateur des capacités s'y bornent (cf. `docs/org-context.md`).
#
# Chaque entrée est une lecture VÉRIFIÉE bornée à O. Les absences sont voulues :
# jetons API, grants de comptes connecteurs, tableaux partagés « avec moi »,
# abonnements de modèles, instances de connecteurs (elles portent les clés membre des
# AUTRES orgs), facturation, légal, bibliothèques, gouvernance de ressources, alias
# `/api/datastore/namespaces/*`, admin — toutes « compte entier » ou hors du sujet.
_LECTURES_VUE_BORNEE: dict[tuple[str, str], frozenset | None] = {
    # Surfaces sans donnée de compte.
    ("GET", "/api/version"): None,
    ("GET", "/api/mcp/catalog"): None,
    ("GET", "/api/connectors"): None,
    ("GET", "/api/openapi.json"): None,
    ("GET", "/api/billing/plans"): None,
    # Le compte, vu dans O.
    ("GET", "/api/me"): None,
    ("GET", "/api/me/orgs"): None,
    ("GET", "/api/me/profile"): None,
    ("GET", "/api/me/agent-context"): None,
    ("GET", "/api/me/agent-toolbox"): None,
    ("GET", "/api/me/calls"): None,
    ("GET", "/api/me/activity-summary"): None,
    ("GET", "/api/me/recent-changes"): None,
    ("GET", "/api/me/search"): None,
    ("GET", "/api/me/shell"): None,
    # Guides, readmes et procédures (perso compris : ils suivent la personne).
    ("GET", "/api/me/guides"): None,
    ("GET", "/api/me/guides/{scope}/{slug}"): None,
    ("GET", "/api/me/guides/{guide_id}"): None,
    ("GET", "/api/me/instructions"): None,
    ("GET", "/api/me/instructions/{slug}"): None,
    ("GET", "/api/me/instructions/{slug}/versions"): None,
    ("GET", "/api/me/instructions/{slug}/usage"): None,
    # Le lien web d'une procédure et ses lecteurs (`partages_procedure`) : lectures de
    # O ; la garde org_admin de la capacité s'applique à la cible, comme ailleurs.
    ("GET", "/api/me/instructions/{slug}/share"): None,
    ("GET", "/api/me/instructions/{slug}/share/readers"): None,
    # Boîte à outils effective dans O (statuts, jamais un secret).
    ("GET", "/api/me/tools"): None,
    ("GET", "/api/me/tools/registry"): None,
    ("GET", "/api/me/tools/{name}/detail"): None,
    ("GET", "/api/me/connectors"): None,
    ("GET", "/api/me/connectors/{name}/oauth-status"): None,
    ("GET", "/api/me/connectors/{provider}/effect"): None,
    ("GET", "/api/settings/api-keys/{provider}"): None,
    # Projets, pages, nœuds.
    ("POST", "/api/me/projects"): frozenset({
        "list", "list_templates", "get", "inventory", "activity", "runs", "lint",
        "handoff"}),
    ("GET", "/api/me/projects/{project_id}"): None,
    ("GET", "/api/me/projects/{project_id}/files"): None,
    ("GET", "/api/me/projects/{id}/export"): None,
    ("POST", "/api/me/docs"): frozenset({
        "get", "list", "revisions", "backlinks", "search", "shared_with_me"}),
    ("GET", "/api/me/nodes/{node_id}"): None,
    ("GET", "/api/me/nodes/{node_id}/rows"): None,
    ("POST", "/api/me/functions"): frozenset({"list", "get", "versions"}),
    ("POST", "/api/me/kb"): frozenset({"get"}),
    # Runner et fils de runs de O.
    ("POST", "/api/me/runner/fleets"): frozenset({"list", "get", "state"}),
    ("POST", "/api/me/runner/jobs"): frozenset({"list", "get"}),
    ("POST", "/api/me/runner/triggers"): frozenset({"list", "get", "deliveries"}),
    ("POST", "/api/me/runs/thread"): frozenset({"read"}),
    # Tableaux.
    ("GET", "/api/datastores"): None,
    ("GET", "/api/datastores/{datastore}"): None,
    ("GET", "/api/datastores/{datastore}/rows"): None,
    ("GET", "/api/datastores/{datastore}/rows/export.csv"): None,
    ("GET", "/api/datastores/{datastore}/rows/{row_id}"): None,
    ("GET", "/api/datastores/{datastore}/rows/{row_id}/history"): None,
    ("GET", "/api/datastores/{datastore}/rows/{row_id}/activity"): None,
    ("GET", "/api/datastores/{datastore}/schema"): None,
    ("GET", "/api/datastores/{datastore}/activity"): None,
    ("GET", "/api/datastores/{datastore}/aggregate"): None,
    ("GET", "/api/datastores/{datastore}/queue"): None,
    ("GET", "/api/datastores/{datastore}/share"): None,
    ("GET", "/api/datastores/{datastore}/url"): None,
}
# Toute lecture de `/api/orgs/{id}/…` et `/api/groups/{id}/…` passe, ÉPINGLÉE sur O.
_ORG_DANS_LE_CHEMIN = re.compile(r"^/api/orgs/([^/]+)(?:/|$)")
_EQUIPE_DANS_LE_CHEMIN = re.compile(r"^/api/groups/([^/]+)(?:/|$)")


def _gabarit(chemin: str) -> re.Pattern:
    return re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", chemin) + "$")


_LECTURES_VUE_BORNEE_RE = [(verbe, _gabarit(chemin), ops)
                           for (verbe, chemin), ops in _LECTURES_VUE_BORNEE.items()]


def _juger_vue_bornee(sub: str, cible: str, view_org: int | None,
                      view_group: int | None, candidat_sous_domaine: int | None,
                      porte_un_run: bool) -> tuple[int | None, tuple | None]:
    """La vue « en tant que » d'un NON-opérateur : `(org O, None)` si elle est ouverte,
    `(None, None)` si elle est sans effet (cible = soi), `(None, (statut, code,
    détail))` si elle est refusée. Toutes les gardes, en une fois, hors de la boucle."""
    from .. import access, group_store, org_store, roles
    org = view_org if view_org else None
    if view_group:
        g = group_store.get_group(view_group)
        if g is None:
            return None, (403, "forbidden", None)
        if org is not None and int(g["org_id"]) != org:
            return None, (403, CODE_HORS_VUE, "L'équipe n'est pas dans l'org consultée.")
        org = int(g["org_id"])
    if org is None:
        return None, (400, "view_as_org_required",
                      "Un org_admin voit « en tant que » dans SON org : `X-Oto-Org` "
                      "(ou `X-Oto-Group`) est requis.")
    if org_store.get_org_role(org, sub) != roles.ORG_ADMIN:
        return None, (403, "forbidden", None)
    if cible == sub:
        return None, None
    if org_store.get_org_role(org, cible) is None:
        return None, (403, CODE_HORS_VUE, "La cible n'est pas membre de cette org.")
    if access.is_platform_operator(cible):
        return None, (403, CODE_HORS_VUE,
                      "La cible est opérateur plateforme : ses droits débordent toute "
                      "org, sa vue ne se borne pas.")
    if view_group and not roles.can_read_group(cible, view_group):
        return None, (403, CODE_HORS_VUE, "La cible ne lit pas cette équipe.")
    if candidat_sous_domaine is not None and candidat_sous_domaine != org:
        return None, (403, CODE_HORS_VUE, "Le sous-domaine désigne une autre org.")
    if porte_un_run:
        return None, (403, CODE_HORS_VUE,
                      "`X-Oto-Run` place la requête dans l'org du run : pas en vue bornée.")
    return org, None


def _lecture_vue_bornee(methode: str, chemin: str, op: str | None, org: int) -> str | None:
    """None si la lecture est ouverte en vue bornée à `org` ; sinon le motif du refus."""
    from .. import group_store
    if methode == "GET":
        m = _ORG_DANS_LE_CHEMIN.match(chemin)
        if m:
            return None if m.group(1) == str(org) else "une autre org"
        m = _EQUIPE_DANS_LE_CHEMIN.match(chemin)
        if m:
            g = group_store.get_group(int(m.group(1))) if m.group(1).isdigit() else None
            return None if g is not None and int(g["org_id"]) == org else "une autre équipe"
    for verbe, gabarit, ops in _LECTURES_VUE_BORNEE_RE:
        if verbe == methode and gabarit.match(chemin) and (ops is None or op in ops):
            return None
    return "cette route"


def refused_prefixes_vue_bornee() -> list[str]:
    """Préfixes REST refusés (`403 view_as_hors_org`) en vue bornée d'un org_admin —
    servis par `GET /api/me` (`view_as_refused_prefixes`, oto#270 suite), pour que le
    dashboard masque une section sans tenir sa propre copie de `_LECTURES_VUE_BORNEE`.

    DÉRIVÉ, jamais recopié : rejoue `_lecture_vue_bornee` (la fonction que le
    middleware applique réellement) sur les gabarits GET du registre de capacités
    (`registry.CAPABILITIES`, qui couvre l'essentiel de la surface REST authentifiée —
    ~200 des ~230 routes servies, `tests/api/test_vue_bornee_middleware.py
    ::test_la_liste_ne_nomme_que_des_routes_servies`). Un changement de la liste
    blanche, ou une capacité REST neuve, change ce que cette fonction rend sans qu'un
    appelant n'ait à y penser.

    `/api/orgs/{id}/…` et `/api/groups/{id}/…` sont ÉPINGLÉS sur O par le middleware
    (jamais refusés en bloc, cf. `_lecture_vue_bornee`) : exclus d'office, comme lui.

    ⚠️ **Portée : les lectures `GET`.** Une lecture POST « op-aware » (`{"op":
    "list"}`…) hors de `_LECTURES_VUE_BORNEE` est elle aussi refusée par le
    middleware, mais n'entre pas dans cette liste — ce sont 7 gabarits sur ~200,
    l'exception documentée (`docs/org-context.md`) plutôt que la règle ; un front qui
    en ajoute une la couvre au cas par cas, pas par préfixe générique.

    Le PRÉFIXE rendu est le chemin tronqué AVANT son premier `{paramètre}`, SLASH
    FINAL GARDÉ (le chemin entier s'il n'en porte pas) : un front le teste par
    `startsWith`, sans connaître le gabarit exact ni le nom des placeholders. Le
    slash final n'est PAS un détail — sans lui, `/api/connectors/{connector}/…`
    (refusé) tronquerait sur `/api/connectors` et couvrirait par erreur la route
    EXACTE `/api/connectors` (whitelistée) : `startsWith("/api/connectors/")` ne
    matche pas `/api/connectors`, `startsWith("/api/connectors")` si."""
    from ..capabilities import registry

    prefixes: set[str] = set()
    for cap in registry.CAPABILITIES:
        for b in cap.rest_bindings():
            if b.verb != "GET":
                continue
            chemin = b.path
            if _ORG_DANS_LE_CHEMIN.match(chemin) or _EQUIPE_DANS_LE_CHEMIN.match(chemin):
                continue  # épinglé sur O, jamais refusé
            # `org=0` : inerte ici — la seule branche qui le lit (`/api/orgs|groups/…`)
            # est déjà écartée juste au-dessus.
            if _lecture_vue_bornee("GET", chemin, None, 0) is None:
                continue
            m = re.search(r"\{[^}]+\}", chemin)
            prefixes.add(chemin[:m.start()] if m else chemin)
    return sorted(prefixes)


# La cible du « voir en tant que » que `ViewAsMiddleware` a APPLIQUÉE à la requête,
# déposée dans le `scope` ASGI — le MÊME dict que celui de `RestCallLogger`, qui
# l'enveloppe et la relit dans son `finally` (même mécanique que `CLE_PRINCIPAL`).
# ⚠️ Pourquoi pas l'en-tête : `X-Oto-View-As` se pose par n'importe quel appelant. Le
# journal le recopiait tel quel — un non-opérateur refusé en 403 y figurait avec la
# cible qu'il avait saisie, une cible = soi ou inconnue passait `ok` avec la colonne
# remplie. Seul le middleware qui applique la vue sait ce qui a été appliqué.
CLE_VIEW_AS_APPLIQUE = "oto_view_as_applique"
# Écriture en « voir en tant que » ACCEPTÉE et exécutée (super_admin + en-tête
# d'acceptation) : le marqueur du journal (`args.view_as_write`), et l'org sous
# laquelle elle s'écrit (celle de la consultation, sinon l'org de contexte de la
# CIBLE) — sans elle, une pose faite sans `X-Oto-Org` n'apparaîtrait dans le journal
# d'aucune org.
CLE_VIEW_AS_ECRITURE = "oto_view_as_ecriture"
CLE_VIEW_AS_ORG = "oto_view_as_org"


class ViewAsMiddleware:
    """Middleware ASGI **brut** (pas BaseHTTPMiddleware, qui bufferiserait le
    streaming `/mcp`) : n'intervient QUE sur `/api/*` portant `X-Oto-Org`, sinon
    pass-through total. Pose l'org de consultation (contextvar `session_org`) lue
    par le seam `access.current_org` → toute la résolution REST (autz + handlers +
    visibilité) scope la consultation, **sans** persister ni muter l'identité.

    Anti-IDOR : l'appartenance est validée ici (org>0) ; on ne fait JAMAIS confiance
    à l'en-tête. Sans header, ou non authentifié → la route suit son cours normal."""

    def __init__(self, app, verifier: JWTVerifier):
        self.app = app
        self._verifier = verifier

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not scope.get("path", "").startswith("/api/"):
            return await self.app(scope, receive, send)
        request = Request(scope, receive)  # headers/query seulement → ne consomme pas le body
        view_org = _parse_view_org(request)
        view_group = _parse_view_group(request)
        view_user = _parse_view_user(request)
        if view_org is None and view_group is None and view_user is None:
            return await self.app(scope, receive, send)
        # sub RÉEL (apply_view_as=False) : sert à gater, jamais à appliquer la consultation.
        sub, err = await _authenticate(request, self._verifier, apply_view_as=False)
        if err:  # non authentifié → la route rendra son 401 ; pas de view-as
            return await self.app(scope, receive, send)
        from .. import access, db, group_store, org_store, roles, session_org
        read_only = False  # consultation en LECTURE SEULE (view-as user OU inspection org opérateur)
        # Écriture en « voir en tant que » demandée par le geste d'acceptation. Elle
        # n'a de sens qu'avec une cible APPLIQUÉE ; son droit (super_admin) est jugé
        # plus bas, et seulement si la requête écrit vraiment.
        ecriture_demandee = False
        borne = None  # org O d'une vue bornée d'org_admin (oto#270), sinon None
        if view_user:  # « voir en tant que » : opérateur plateforme, ou org_admin borné
            if await run_in_threadpool(access.is_platform_operator, sub):
                if view_user == sub or await run_in_threadpool(db.get_user, view_user) is None:
                    view_user = None  # cible = soi ou inconnue → pas de consultation (no-op)
                else:
                    read_only = True
                    ecriture_demandee = _parse_view_write(request)
            else:
                borne, refus = await run_in_threadpool(
                    _juger_vue_bornee, sub, view_user, view_org, view_group,
                    session_org.current_subdomain_candidate(),
                    bool((request.headers.get("x-oto-run") or "").strip()))
                if refus is not None:
                    return await _json_error(request, *refus)(scope, receive, send)
                if borne is None:
                    view_user = None  # cible = soi → pas de consultation (no-op)
                else:
                    read_only = True
                    ecriture_demandee = _parse_view_write(request)
        # Écriture ACCEPTÉE : cible appliquée + en-tête + opérateur super_admin.
        ecriture_acceptee = (ecriture_demandee
                             and await run_in_threadpool(access.is_super_admin, sub))
        if view_group:  # équipe consultée → valide la lecture + DÉRIVE son org parente (invariant)
            g = await run_in_threadpool(group_store.get_group, view_group)
            if g is None or not await run_in_threadpool(roles.can_read_group, sub, view_group):
                return await _json_error(request, 403, "forbidden")(scope, receive, send)
            view_org = g["org_id"]
            # Même règle que la consultation d'org et qu'`active_org_readonly` de
            # `/api/me` : un opérateur plateforme SANS rôle réel dans l'org parente
            # inspecte l'équipe en LECTURE SEULE — un super_admin passe
            # `can_read_group` par escalade, mais inspection ≠ escalade.
            role_reel = await run_in_threadpool(org_store.get_org_role, view_org, sub)
            if role_reel is None and await run_in_threadpool(access.is_platform_operator, sub):
                read_only = True
        elif view_org:  # org>0 (0=perso = profil global, pas de check)
            # Membership RÉELLE (colonne DB, PAS l'escalade super_admin) : un membre
            # consulte son org normalement (lecture + écriture selon son rôle).
            real_role = await run_in_threadpool(org_store.get_org_role, view_org, sub)
            if real_role is not None:
                pass  # membre réel — comportement inchangé (writes gatés par le rôle)
            elif await run_in_threadpool(access.is_platform_operator, sub):
                # Opérateur plateforme NON-membre : inspection d'une org tierce en LECTURE
                # SEULE (même patron que le view-as user), même pour un super_admin (mode
                # inspection ≠ escalade d'admin).
                read_only = True
            else:
                return await _json_error(request, 403, "forbidden")(scope, receive, send)
        # Garde LECTURE SEULE : le dashboard LIT en POST op-aware (`{op:'list'|'get'}`),
        # donc on ne peut pas gater par méthode. Sur une requête non-GET, on lit l'`op`
        # du corps : seules les OPS DE LECTURE passent ; toute mutation (op d'écriture,
        # ou write sans op) → 403, SAUF écriture en view-as ACCEPTÉE (super_admin +
        # `X-Oto-View-As-Write: 1`). Le corps est rejoué intact au handler.
        ecriture = False
        op = None
        if read_only and request.method != "GET":
            op, receive = await _peek_op(receive)
            if op not in _READ_OPS:
                if not ecriture_demandee:
                    return await _json_error(request, 403, "view_as_read_only")(scope, receive, send)
                if not ecriture_acceptee:
                    return await _json_error(request, 403, "view_as_write_forbidden")(
                        scope, receive, send)
                if view_org and (
                        await run_in_threadpool(org_store.get_org_role, view_org, view_user)
                        is None
                        or (view_group and not await run_in_threadpool(
                            roles.can_read_group, view_user, view_group))):
                    # C'est la CIBLE qui agit : elle doit être membre de l'org (et de
                    # l'équipe) consultée. On n'écrit pas en son nom dans une org qui
                    # n'est pas la sienne (anti-IDOR) ; une LECTURE y reste permise en
                    # inspection, comme avant.
                    return await _json_error(request, 403, "forbidden")(scope, receive, send)
                ecriture = True
        if borne is not None:
            # Vue bornée : la requête est une lecture (garde ci-dessus) ; reste à savoir
            # si c'est une lecture OUVERTE, et dans O.
            motif = await run_in_threadpool(_lecture_vue_bornee, request.method,
                                            scope.get("path", ""), op, borne)
            if motif is not None:
                return await _json_error(
                    request, 403, CODE_HORS_VUE,
                    f"Vue « en tant que » bornée à l'org #{borne} : {motif} n'y est pas "
                    "ouverte.")(scope, receive, send)
        operateur_token = None
        if view_user is not None:
            # Publié pour le journal : APRÈS toutes les gardes (un 403 est sorti plus
            # haut, une cible = soi ou inconnue a été remise à None).
            scope[CLE_VIEW_AS_APPLIQUE] = view_user
            if ecriture:
                scope[CLE_VIEW_AS_ECRITURE] = True
                scope[CLE_VIEW_AS_ORG] = (view_org if view_org is not None else
                                          await run_in_threadpool(access.current_org,
                                                                  view_user))
                operateur_token = session_org.set_view_as_operator(sub)
        usr_token = session_org.set_view_user(view_user) if view_user is not None else None
        # Écriture acceptée, publiée pour TOUTE requête (lecture comprise) : `/api/me`
        # en dit la lecture seule (oto#212) sans rejuger l'en-tête.
        accepte_token = (session_org.set_view_as_write_accepted(True)
                         if view_user is not None and ecriture_acceptee else None)
        org_token = session_org.set_view_org(view_org) if view_org is not None else None
        grp_token = session_org.set_view_group(view_group) if view_group is not None else None
        borne_token = (session_org.set_view_as_bound_org(borne)
                       if borne is not None else None)
        try:
            return await self.app(scope, receive, send)
        finally:
            if borne_token is not None:
                session_org.reset_view_as_bound_org(borne_token)
            if grp_token is not None:
                session_org.reset_view_group(grp_token)
            if org_token is not None:
                session_org.reset_view_org(org_token)
            if accepte_token is not None:
                session_org.reset_view_as_write_accepted(accepte_token)
            if usr_token is not None:
                session_org.reset_view_user(usr_token)
            if operateur_token is not None:
                session_org.reset_view_as_operator(operateur_token)


# --- Journalisation des appels REST dans le flux unifié (ADR 0017, kind='rest') ---
# La face MCP est tracée par otomata-calllog ; la face REST ne l'était PAS (3/4 de
# la plateforme invisibles au monitoring). Ce middleware comble le trou : une ligne
# tool_calls(kind='rest') par requête /api/*, dérivée du même substrat.

_REST_LOG_TASKS: set = set()  # garde les refs des tâches fire-and-forget (anti-GC)


def _claimed_sub(request: Request) -> str | None:
    """Sub revendiqué par le bearer JWT, **NON vérifié** — attribution de log
    uniquement (jamais d'autz ; la route, elle, vérifie pour de vrai). Best-effort :
    token API opaque (`oto_…`) ou JWT malformé → None (ligne anonyme).

    Qualifié par tenant (ADR 0052) avec le MÊME qualificateur que le verifier : deux
    utilisateurs de deux émetteurs peuvent porter le même sub Logto, et sans ça leurs
    requêtes s'écriraient sur la même ligne d'audit — celle de l'utilisateur `oto`."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    parts = auth[7:].strip().split(".")
    if len(parts) != 3:  # pas un JWT → token opaque, pas d'attribution
        return None
    try:
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(pad))
        return tenancy.current().qualify_claims(claims)
    # noqa: SILENT — sub revendiqué illisible ⇒ pas de view-as, la requête reste la sienne
    except Exception:
        return None


def _normalize_route(path: str) -> str:
    """Réduit la cardinalité pour l'agrégation : segments d'id → `:id`, paramètres
    déclarés secrets → `:token` / `:code`. `/api/orgs/7/audit-log` →
    `/api/orgs/:id/audit-log`. Le fond vit dans `oto_mcp.journal_secrets` : la
    réduction PAR FORME (ce que faisait cette fonction) ne voyait pas les quatre
    routes dont le secret est dans le chemin (#558)."""
    return journal_secrets.route_and_secrets(path)[0]


async def _emit_rest_event(row: dict) -> None:
    """Écrit l'événement hors event-loop (to_thread → insert sync non bloquant).
    Best-effort : une panne de log n'a jamais d'effet sur la requête servie."""
    try:
        await asyncio.to_thread(db.insert_tool_call, row)
    except Exception:  # noqa: BLE001 — le monitoring ne casse jamais le service
        logger.debug("rest call-log emit failed", exc_info=True)


class RestCallLogger:
    """Middleware ASGI **brut** : journalise chaque requête `/api/*` comme événement
    `kind='rest'` du flux unifié (ADR 0017). Pass-through total hors `/api/*` (ne
    touche JAMAIS le streaming `/mcp`) et sur les préflights `OPTIONS` (bruit CORS).
    `tool` = `MÉTHODE /route-normalisée` ; `ok` = 2xx/3xx ; les ≥400 portent le code
    dans `error`. Écriture en tâche de fond → zéro latence ajoutée, jamais bloquant."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not scope.get("path", "").startswith("/api/"):
            return await self.app(scope, receive, send)
        method = scope.get("method", "")
        if method == "OPTIONS":
            return await self.app(scope, receive, send)
        status = {"code": 0}

        async def _send(message):
            if message.get("type") == "http.response.start":
                status["code"] = message.get("status", 0)
            await send(message)

        request = Request(scope, receive)  # headers/query only → ne consomme pas le body
        org = _parse_view_org(request)  # org de consultation revendiquée (header), best-effort
        started = time.monotonic()
        try:
            await self.app(scope, receive, _send)
        finally:
            code = status["code"]
            # ⚠️ LU APRÈS la requête, pas avant : c'est l'authentification qui
            # résout le porteur, et elle n'a pas encore tourné au moment où le
            # middleware entre. Calculer le compte à l'entrée revenait à ne
            # pouvoir le lire que dans l'en-tête — donc à n'attribuer QUE les
            # JWT, et à écrire une ligne anonyme pour tout appel par jeton API ou
            # par jeton de délégation.
            principal = scope.get(api_base.CLE_PRINCIPAL) or {}
            # Le run JUGÉ par l'adaptateur (oto#229), même motif que le principal :
            # publié dans le scope, relu ici, zéro requête. Sous un run, l'org de la
            # ligne est celle du run — comme `access.current_org` la rend au calllog
            # MCP —, sans quoi la timeline d'org (`get_run(org_id=…)`) ne verrait
            # pas un appel fait sans `X-Oto-Org`.
            run = scope.get(_cap_rest_adapter.CLE_RUN) or {}
            if run.get("org_id") is not None:
                org = run["org_id"]
            sub = principal.get("sub") or _claimed_sub(request)
            route, masques = journal_secrets.route_and_secrets(scope.get("path", ""))
            ecriture_view_as = bool(scope.get(CLE_VIEW_AS_ECRITURE))
            if ecriture_view_as:
                # Une écriture faite AU NOM d'un autre compte : le marqueur la distingue
                # d'une consultation, et son org est celle où elle a agi (même sans
                # `X-Oto-Org`), pour que le journal de l'org de la cible la montre.
                masques = {**(masques or {}), "view_as_write": True}
                if org is None:
                    org = scope.get(CLE_VIEW_AS_ORG)
            row = {
                "kind": "rest",
                "tool": f"{method} {route}",
                # Le masque ne va JAMAIS dans `tool` (une empreinte par jeton ferait
                # exploser la cardinalité du `GROUP BY tool` du monitoring) : il va
                # dans `args`, où il répond à « le même jeton a-t-il été rejoué ? ».
                "args": masques,
                # ⚠️ `sub` = le PORTEUR du bearer, jamais la cible d'un « en tant
                # que ». `effective_sub` n'est pas là pour ça : le schéma en fait
                # le compte relu APRÈS le handler, dont toute divergence d'avec
                # `sub` EST un défaut. Y écrire une consultation view-as rendrait
                # normale la divergence que cette colonne existe pour dénoncer.
                "sub": sub,
                # Le jeton employé, NOMMÉ jamais écrit : deux appels du même compte
                # par deux jetons étaient indistinguables, et une délégation du
                # runner ressemblait à une session humaine.
                "token_id": principal.get("token_id"),
                "token_kind": principal.get("token_kind"),
                "org_id": org,
                # La cible du « voir en tant que » APPLIQUÉE (#572 point 4) : publiée
                # par `ViewAsMiddleware` dans ce scope, jamais l'en-tête brut — cf.
                # `CLE_VIEW_AS_APPLIQUE`. Jamais un remplacement de `sub` (l'opérateur
                # réel reste le sub de la ligne, volontairement) : un champ EN PLUS.
                "view_as_sub": scope.get(CLE_VIEW_AS_APPLIQUE),
                "run_id": run.get("run_id"),
                # Le geste de la requête (oto#273) : le même identifiant que celui
                # estampillé sur les lignes qu'elle a écrites. NULL hors capacité.
                "call_uid": scope.get(_cap_rest_adapter.CLE_GESTE),
                "ok": 200 <= code < 400,
                "error": (f"HTTP {code}" if code >= 400 else None),
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
            task = asyncio.create_task(_emit_rest_event(row))
            _REST_LOG_TASKS.add(task)
            task.add_done_callback(_REST_LOG_TASKS.discard)


def make_routes(verifier: JWTVerifier, mcp_instance=None) -> Iterable:
    from starlette.routing import Route

    datastore_routes = api_routes_datastore.make_routes(
        verifier=verifier,
        authenticate=_authenticate,
        json_response=_json,
        json_error=_json_error,
        cors_headers=_cors_headers,
        options_handler=options_handler,
    )

    sirene_routes = api_routes_sirene.make_routes(
        verifier=verifier,
        authenticate=_authenticate,
        json_response=_json,
        json_error=_json_error,
        options_handler=options_handler,
    )

    accords_routes = api_routes_accords.make_routes(
        verifier=verifier,
        authenticate=_authenticate,
        json_response=_json,
        json_error=_json_error,
        options_handler=options_handler,
    )

    # OAuth Zoho « server-based » — SECOND mode d'acquisition, le Self Client
    # restant intact et par défaut (les deux produisent le même credential).
    zoho_routes = api_routes_zoho.make_routes(
        verifier=verifier,
        authenticate=_authenticate,
        json_response=_json,
        json_error=_json_error,
        options_handler=options_handler,
    )

    salesforce_oauth_routes = api_routes_salesforce.make_routes(
        verifier=verifier,
        authenticate=_authenticate,
        json_response=_json,
        json_error=_json_error,
        options_handler=options_handler,
    )

    # Retour de consentement Instagram (statistiques) — le `/start` passe par le
    # seam commun (`connectors/flow`), seul le callback est une route.
    instagram_meta_routes = api_routes_instagram_meta.make_routes(
        verifier=verifier,
        authenticate=_authenticate,
        json_response=_json,
        json_error=_json_error,
        options_handler=options_handler,
    )

    # Couche capacité (ADR 0009) : routes REST dérivées du registre (no-op tant
    # qu'il est vide — canari). Même séquence autz→validation→handler que MCP.
    capability_routes = _cap_rest_adapter.make_routes(
        verifier, _authenticate, _json, _json_error, options_handler,
        _cap_registry.CAPABILITIES,
    )

    # Billing écrit à la main (ADR 0043, #488) : le webhook Mollie (non authentifié)
    # et le téléchargement du PDF d'une facture (authentifié — un octet n'est pas du
    # JSON, il ne peut donc pas passer par la couche capacité).
    billing_webhook_routes = api_routes_billing.make_routes(
        options_handler, verifier=verifier, authenticate=_authenticate,
        json_error=_json_error)

    # Le webhook des AGENTS (12/09/2026), à la main pour la même raison que celui
    # de Mollie : il accepte un corps JSON LIBRE, que l'adaptateur de capacité
    # refuserait champ par champ (`unknown_fields`). Il porte sa propre
    # authentification — un secret par déclencheur, `Authorization: Bearer otoh_…`
    # — et ne passe donc pas par `_authenticate`.
    hook_routes = api_routes_hooks.make_routes(options_handler)

    # Les RECEVEURS (28/09/2026) : un fournisseur y livre ce qu'oto a commandé — les
    # téléphones d'un reveal Apollo. À la main pour la même raison : corps libre, et
    # l'autorisation est le jeton du chemin, propre à une commande.
    receveur_routes = api_routes_receveurs.make_routes(options_handler)

    table = [
        Route("/favicon.svg", public.favicon, methods=["GET"]),
        Route("/favicon.ico", public.favicon, methods=["GET"]),
        # La version SERVIE (oto#33), sans auth : ce que CE processus exécute. La
        # même étiquette part sur chaque réponse en `X-Oto-Version` — l'endpoint
        # sert à qui demande, l'en-tête à qui relit son journal après coup.
        Route("/api/version", public.version, methods=["GET"]),
        Route("/api/version", options_handler, methods=["OPTIONS"]),
        Route("/api/mcp/catalog", bind(public.mcp_catalog, mcp_instance=mcp_instance), methods=["GET"]),
        Route("/api/mcp/catalog", options_handler, methods=["OPTIONS"]),
        # Descriptif de l'API REST, dérivé (cf. openapi.py). Servi aux deux chemins
        # usuels : un intégrateur sonde l'un ou l'autre, aucun n'est plus canonique.
        Route("/openapi.json", public.openapi_doc, methods=["GET"]),
        Route("/openapi.json", options_handler, methods=["OPTIONS"]),
        Route("/api/openapi.json", public.openapi_doc, methods=["GET"]),
        Route("/api/openapi.json", options_handler, methods=["OPTIONS"]),
        Route("/api/connectors", bind(public.connectors_catalog, verifier=verifier), methods=["GET"]),
        Route("/api/connectors", options_handler, methods=["OPTIONS"]),
        Route("/api/invitations/{token}", public.invite_preview, methods=["GET"]),
        Route("/api/invitations/{token}", options_handler, methods=["OPTIONS"]),
        Route("/api/me/avatar", bind(media.avatar_save, verifier=verifier), methods=["POST"]),
        Route("/api/me/avatar", options_handler, methods=["OPTIONS"]),
        Route("/api/me/projects/{project_id:int}/files", bind(projects.project_files_upload, verifier=verifier), methods=["POST"]),
        Route("/api/me/projects/{project_id:int}/files", options_handler, methods=["OPTIONS"]),
        # Dépôt DIRECT d'un audio à transcrire (multipart) — la forme par référence,
        # `POST …/transcriptions`, est une capacité (`capabilities/transcription.py`).
        Route("/api/me/projects/{project_id:int}/transcriptions/upload", bind(api_transcription.transcription_upload, verifier=verifier), methods=["POST"]),
        Route("/api/me/projects/{project_id:int}/transcriptions/upload", options_handler, methods=["OPTIONS"]),
        Route("/api/public/docs/{token}", public.public_doc, methods=["GET"]),
        Route("/api/public/docs/{token}", options_handler, methods=["OPTIONS"]),
        # Réception d'un upload signé out-of-bande (#105) — jeton dans l'URL, pas de JWT.
        # PUT/POST = agent (curl brut) / formulaire humain (multipart) ; GET = page d'upload.
        Route("/api/upload/{token}", uploads.upload_receive, methods=["PUT", "POST"]),
        Route("/api/upload/{token}", uploads.upload_form, methods=["GET"]),
        Route("/api/upload/{token}", options_handler, methods=["OPTIONS"]),
        # Page de partage publique server-rendered (lisible par un agent, ADR gap
        # « pages SPA non lisibles »). Servie sous dashboard.oto.ninja via Caddy.
        Route("/p/d/{token}", public.public_doc_view, methods=["GET"]),
        # Désinscription des relances (oto_admin_outreach) — jeton signé dans
        # l'URL, aucune session : c'est le lien du pied de page des mails.
        Route("/o/u/{token}", public.outreach_unsubscribe, methods=["GET"]),
        # Désinscription du DIGEST de signaux (oto#150) — même régime, route et
        # jeton (`typ`) distincts : jamais interchangeable avec la ligne au-dessus.
        Route("/o/d/{token}", public.digest_unsubscribe, methods=["GET"]),
        # Vitrine d'une procédure partagée par lien (/p/<token> côté front) : sans auth,
        # le jeton est le secret ; jamais le corps (capabilities/partages_procedure.py).
        Route("/api/public/process-shares/{token}", public.process_share_preview, methods=["GET"]),
        Route("/api/public/process-shares/{token}", options_handler, methods=["OPTIONS"]),
        # Désinscription du résumé des LECTEURS — même régime que la ligne au-dessus,
        # route, jeton (`typ`) et table distincts.
        Route("/o/r/{token}", public.readers_digest_unsubscribe, methods=["GET"]),
        Route("/api/orgs/{id}/logo", bind(media.org_logo_save, verifier=verifier), methods=["POST"]),
        Route("/api/orgs/{id}/logo", options_handler, methods=["OPTIONS"]),
        # /api/me/instructions* — migré en capacités (ADR 0009, capabilities/orgs/instructions.py),
        # monté par capability_routes plus bas.
        Route("/api/me/projects/{id}/export", bind(projects.me_project_export, verifier=verifier), methods=["GET"]),
        Route("/api/me/projects/{id}/export", options_handler, methods=["OPTIONS"]),
        # Export CSV d'un tableau entier — écrit à la main pour la même raison
        # structurelle que l'export ZIP juste au-dessus : `text/csv`, pas du
        # JSON (`api/datastore_export.py`). Le distingue : un CORPS STREAMÉ
        # (`base._file_stream`), pas un blob déjà en mémoire — voir le docstring
        # du module pour les trois contraintes mono-boucle qu'il tient.
        Route("/api/datastores/{datastore}/rows/export.csv",
              bind(datastore_export.export_csv, verifier=verifier), methods=["GET"]),
        Route("/api/datastores/{datastore}/rows/export.csv", options_handler,
              methods=["OPTIONS"]),
        *datastore_routes,
        *sirene_routes,
        *accords_routes,
        *zoho_routes,
        *salesforce_oauth_routes,
        *instagram_meta_routes,
        *capability_routes,
        *billing_webhook_routes,
        *hook_routes,
        *receveur_routes,
        # EN DERNIER, et c'est la garde : un alias déprécié ne peut capturer que ce
        # que rien d'autre ne sert. Monté plus haut, un de ses placeholders pourrait
        # éclipser une vraie route sans que rien ne le dise (#519, retrait #526).
        *alias_routes.make_routes(options_handler),
    ]
    # Le journal apprend ICI quels paramètres de route portent un secret — dérivé
    # de la table qu'on vient d'assembler, jamais d'une liste tenue à la main (#558).
    # Une route future qui déclare `{token}` est couverte le jour où elle est montée.
    journal_secrets.declare_routes(table)
    return table
