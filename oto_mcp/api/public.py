"""Handlers des surfaces REST **publiques** — servies sans en-tête d'auth.

Ce qu'elles ont en commun n'est pas un sujet métier mais un régime : l'adaptateur
REST des capacités authentifie TOUJOURS, donc une surface anonyme ne peut pas y
passer et reste écrite à la main (l'argument est déjà dans `guide_library_public`).
Quatre d'entre elles sont consommées par un PROGRAMME sans en-tête : le build du
site vitrine (`refresh-catalog.mjs` → catalog/connectors/bibliothèque/guides) et celui
de docs.oto.cx (`refresh-openapi.mjs` → openapi.json).

- `GET /favicon.svg` + `/favicon.ico`      → mark de marque (l'endpoint MCP n'a pas de page racine)
- `GET /api/version`                       → la version SERVIE (`version.py`)
- `GET /api/mcp/catalog`                   → catalogue des tools MCP (autodoc)
- `GET /openapi.json` + `/api/openapi.json` → descriptif REST dérivé (`openapi.py`)
- `GET /api/connectors`                    → catalogue des connecteurs (auth OPTIONNELLE)
- `GET /api/guide-library[/{slug}]`        → bibliothèque publique de guides (marketplace)
- `GET /api/guides/library[/{slug}]`       → guides PLATEFORME
- `GET /api/invitations/{token}` + `/code/{code}` → aperçu d'invitation (le jeton EST le secret)
- `GET /api/public/docs/{token}`           → doc partagé (JSON)
- `GET /p/d/{token}`                       → le même, server-rendered (lisible par un agent sans JS)
- `GET /o/u/{token}`                       → désinscription d'une relance (le jeton EST le secret)

`/api/connectors` est la seule MIXTE : anonyme pour la vitrine, authentifiée pour
le dashboard qui y scope son catalogue sur l'org active — d'où son `verifier`.

La table de routes (chemins, méthodes, ORDRE) reste assemblée dans
`api.routes.make_routes` : l'ordre de montage est un contrat, il se lit d'un seul
endroit. Ce module ne porte que les handlers.
"""
from __future__ import annotations

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                                 Response)

from .. import (access, deprecations, providers, db, guide_store, openapi,
                org_store, version as oto_version)
from ..connectors import activation as connector_activation
from ..connectors import cardinality as connector_cardinality
from .base import _authenticate, _json, _json_error


async def favicon(request: Request) -> Response:
    """Favicon de marque servi sur mcp.oto.cx (mark canonique, aligné oto.cx).

    L'endpoint MCP n'a pas de page HTML racine → un navigateur/annuaire qui
    sonde `/favicon.svg` ou `/favicon.ico` tombait sur un 404 (aucune icône
    de marque). On sert le mark Otomata (source unique `brand.py`) sur les
    deux chemins.
    """
    from .. import brand
    return Response(
        brand.FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def version(request: Request) -> JSONResponse:
    """La version SERVIE par ce processus — publique, sans auth (oto#33).

    Sans auth, et c'est le point : un consommateur qui constate un changement de
    comportement doit pouvoir le dater **avant** d'avoir résolu quoi que ce soit
    d'identité, et un contrôle externe (Uptime Kuma, un script de déploiement, un
    agent) n'a pas de jeton. Le document ne porte AUCUNE valeur — un ref git, un
    SHA, deux horodatages —, exactement comme `/api/openapi.json` et
    `/api/mcp/catalog`.

    Écrite à la main plutôt qu'en capacité (ADR 0009) pour la raison donnée en tête
    de module : l'adaptateur REST des capacités authentifie TOUJOURS, une surface
    anonyme ne peut pas y passer.

    ⚠️ Ce que ce processus EXÉCUTE, pas ce que le dernier workflow a déployé — la
    différence, et pourquoi elle mord, sont dans `oto_mcp/version.py`.
    """
    return _json(request, oto_version.instantane())


async def mcp_catalog(request: Request, *, mcp_instance) -> JSONResponse:
    """Liste publique des tools MCP exposés — alimente l'autodoc oto.ninja.

    Pas d'auth : la doc des tools (nom, description, schémas) est de toute
    façon découvrable via tools/list du protocole MCP. CORS large pour
    permettre fetch côté oto.ninja.
    """
    if mcp_instance is None:
        return _json(request, {"tools": []})
    try:
        tools = await mcp_instance.list_tools(run_middleware=False)
    except Exception as e:
        return _json_error(request, 500, f"list_tools_failed:{e}")
    payload = []
    # (Le filtre « bridges remote per-namespace » a été retiré — ADR 0034 B4 :
    # le namespace `bridge` est générique, aucun nom client n'atteint l'autodoc.)
    for t in tools:
        # Tool object exposes name, description, parameters (input schema),
        # output_schema. Some attributes may be None depending on the type.
        payload.append({
            "name": t.name,
            "description": (t.description or "").strip(),
            "input_schema": getattr(t, "parameters", None),
            "output_schema": getattr(t, "output_schema", None),
        })
    return _json(request, {"tools": payload, "count": len(payload)})


async def openapi_doc(request: Request) -> JSONResponse:
    """Descriptif OpenAPI de l'API REST — **dérivé** du registre de capacités et
    de la table de routes VIVANTE (`request.app.routes`), donc jamais désynchronisé.

    Pas d'auth, comme `/api/mcp/catalog` : un descriptif d'API décrit des FORMES,
    aucune valeur. Sans lui, chaque intégrateur redécouvre la surface par sondage
    de chemins — et conclut faux (cf. `openapi.py`). `/api/admin/*` est exclu.
    """
    try:
        routes = getattr(request.app, "routes", None)
    # noqa: SILENT — document OpenAPI servi même si la table de routes n'est pas lisible
    except Exception:                                   # pas d'app Starlette exposée
        routes = None
    base = str(request.base_url).rstrip("/") or None
    return _json(request, openapi.build(routes, server_url=base))


async def connectors_catalog(request: Request, *, verifier: JWTVerifier) -> JSONResponse:
    """Catalogue des connecteurs (registre source unique), auth optionnelle.

    Cran d'activation (ADR 0010) filtré EN AMONT de la visibilité : un
    connecteur non activé (master global OFF sans override d'org ON) n'apparaît
    pas dans la vue PRODUIT (anonyme + non-admin). L'**admin voit tout le
    registre** — sa vue de gouvernance sert justement à activer/désactiver.
    Ensuite, visibilité : anonyme → self-serve seuls (les `platform_granted`,
    dont les bridges client-sensibles ADR 0003, sont deny-by-default comme sur
    la face MCP) ; non-admin authentifié → + ceux dont un namespace est entitled
    pour le sub (override d'org appliqué via son org active).

    Enfin, `auth.cardinality` : le registre est PUR, donc la ligne qu'il produit
    porte le défaut du CODE. Dès qu'il y a un requérant, il y a une org de
    contexte, donc une réponse EFFECTIVE — et c'est elle qu'on sert
    (`connectors.cardinality.overlay_for_org`, oto-backend#732). Sans ça, une org
    élargie par surcharge lisait « single » sur un connecteur dont le serveur
    accepte un second compte : un geste offert par la base et jamais par l'écran.
    """
    cat = providers.public_catalog()
    if not request.headers.get("authorization"):
        exposed = connector_activation.exposed_connectors(None)
        cat = [c for c in cat if c["name"] in exposed]
        cat = [c for c in cat if c["availability"] != "platform_granted"]
        # Aucun requérant ⟹ aucune org de contexte : la cardinalité servie ne peut
        # être que le défaut du code (surchargeable seulement au cran PLATEFORME,
        # que l'overlay applique aussi avec `org=None`). C'est la vitrine.
        return _json(request, {"connectors": connector_cardinality.overlay_for_org(cat, None)})
    sub, err = await _authenticate(request, verifier)
    if err:
        return err
    # Org de CONTEXTE (seam ADR 0023 : consultation X-Oto-Org > maison) — le
    # catalogue suit l'org consultée au dashboard, comme status_for. Lue une fois :
    # elle sert la visibilité ET la cardinalité, qui doivent parler de la même org.
    org = access.current_org(sub)
    if not access.is_platform_operator(sub):
        # Visibilité par l'activation (master × override d'org). Un connecteur à
        # clé plateforme réservé (ex. scaleway) est tenu hors des orgs non
        # autorisées par son activation (master OFF + override org ON), plus par
        # un grant de namespace (retiré, ADR 0031).
        exposed = connector_activation.exposed_connectors(org)
        cat = [c for c in cat if c["name"] in exposed]
    return _json(request, {"connectors": connector_cardinality.overlay_for_org(cat, org)})


# --- Ce que la vitrine anonyme a le droit de voir d'une entrée de bibliothèque ---
#
# Une entrée publiée est du CONTENU public ; les identifiants qui la rattachent à
# quelqu'un ne le sont pas. La ligne `guide_library` porte les deux, et les deux
# sortaient : `published_by` (identifiant d'UTILISATEUR, forme `<tenant>:<sub>` —
# son préfixe nomme le tenant d'où l'entrée a été publiée), `author_org_id` et
# `source_org_id` (identifiants d'ORG), `id` et `forked_from` (identifiants de
# LIGNE). Servis à un appelant sans jeton, ils faisaient de la vitrine un
# annuaire des orgs et des comptes qui publient.
#
# **Allowlist, pas liste de champs retirés.** Ce qui n'est pas nommé ici ne sort
# pas : une colonne ajoutée demain à `guide_library` ne peut donc pas fuir par
# inadvertance. Une liste de champs à retirer, elle, ne protège que du passé —
# elle est muette sur la colonne qui n'existe pas encore, c'est-à-dire sur le
# seul cas qu'on ne relira pas.
#
# Ces champs sont ceux, et seulement ceux, que lit le consommateur réel (oto.cx :
# `web/src/views/` pour la page de détail, `web/scripts/refresh-catalog.mjs`
# pour le cliché du build), plus ce qui décrit le guide lui-même. La face
# AUTHENTIFIÉE `/api/me/guide-library` n'est pas touchée et continue de tout
# servir — c'est elle qui porte le fork et la dépublication, qui visent une
# entrée par son `id`.
_VITRINE_META = ("slug", "title", "description", "author_kind", "author_display",
                 "category", "tags", "visibility", "version", "created_at",
                 "updated_at", "snippet")
_VITRINE_ENTREE = _VITRINE_META + ("body_md", "slots", "source_slug")


def _vitrine(entree: dict, champs: tuple) -> dict:
    """Une entrée de bibliothèque réduite aux champs publiables."""
    return {k: v for k, v in entree.items() if k in champs}


async def guide_library_public(request: Request) -> JSONResponse:
    """Catalogue PUBLIC des guides (bibliothèque/marketplace) — pas d'auth.

    Alimente le site vitrine oto.ninja. Deny-by-default sur DEUX axes : quelles
    ENTRÉES (`visibility='public'` uniquement, jamais 'unlisted' ni les brouillons
    d'org) et quels CHAMPS (`_VITRINE_META`). Filtres gros grain en query params
    (`q`/`category`/`author`) ; le filtrage fin reste client. Route écrite à la
    main car l'adaptateur REST des capacités authentifie toujours (l'anonyme ne
    peut pas y passer).
    """
    q = request.query_params
    try:
        limit = min(int(q.get("limit", "100")), 200)
    except ValueError:
        limit = 100
    items = org_store.list_library(
        query=q.get("q"), category=q.get("category"),
        author_kind=q.get("author"), include_unlisted=False, limit=limit)
    # ⚠️ Projeter AVANT le doublage des noms : `avec_les_deux_noms` republie la
    # MÊME liste sous l'ancien nom, et une projection posée après servirait des
    # entrées réduites sous un nom et complètes sous l'autre.
    items = [_vitrine(e, _VITRINE_META) for e in items]
    # Les deux noms le temps du préavis (#519) : le build de la vitrine lit encore
    # `doctrines`, et il est déployé ailleurs que dans ce dépôt.
    return _json(request, deprecations.avec_les_deux_noms({"guides": items}))


async def guide_library_public_get(request: Request) -> JSONResponse:
    """Un guide PUBLIC complet (markdown) par slug — vitrine, pas d'auth.
    Public-only : une entrée 'unlisted' n'est jamais servie ici, et seuls les
    champs de `_VITRINE_ENTREE` en sortent (les identifiants restent dedans)."""
    entry = org_store.get_library_entry(
        slug=request.path_params["slug"], include_unlisted=False)
    if not entry:
        return _json_error(request, 404, "unknown_entry")
    return _json(request, _vitrine(entry, _VITRINE_ENTREE))


async def guides_library_public(request: Request) -> JSONResponse:
    """Catalogue PUBLIC des guides PLATEFORME — pas d'auth.

    Même rôle que `guide_library_public` : alimenter la vitrine (snapshot
    build-time du site) et rendre lisible par un humain ce que l'agent charge
    via `oto_guide`. Deny-by-default par CONSTRUCTION plutôt que par filtre :
    `list_guides_for()` sans `sub` ni `org_id` ne rend que le scope plateforme
    — un guide d'org ou d'user ne peut pas fuir ici, même par erreur d'appel.
    """
    return _json(request, {"guides": guide_store.list_guides_for()})


async def guides_library_public_get(request: Request) -> JSONResponse:
    """Un guide PLATEFORME complet (markdown) par slug — vitrine, pas d'auth.
    `scope='platform'` est EXPLICITE : sans lui, `read_guide_scoped` cherche
    aussi org puis user, ce qu'une route anonyme ne doit jamais faire."""
    g = guide_store.read_guide_scoped(request.path_params["slug"], scope="platform")
    if not g:
        return _json_error(request, 404, "unknown_guide")
    return _json(request, g)


async def invite_preview(request: Request) -> JSONResponse:
    """Aperçu PUBLIC d'une invitation (pas d'auth — le token est le secret).
    Alimente la page d'accueil « vous êtes invité·e » avant la création de
    compte : email visé + inviteur, pour accompagner l'onboarding."""
    p = org_store.preview_invitation(request.path_params.get("token", ""))
    if not p:
        return _json_error(request, 404, "invalid_or_expired")
    return _json(request, p)


async def invite_preview_by_code(request: Request) -> JSONResponse:
    """Aperçu PUBLIC d'une invitation d'org par code court (/invitation/<code>)."""
    p = org_store.preview_invitation_by_code(request.path_params.get("code", ""))
    if not p:
        return _json_error(request, 404, "invalid_or_expired")
    return _json(request, p)


async def public_doc(request: Request) -> JSONResponse:
    """Lecture publique d'un doc partagé par token (gap #4a) — PAS d'auth,
    lecture seule. Le dashboard rend le markdown sur sa route publique /p/d/<token>."""
    token = request.path_params.get("token", "")
    doc = db.get_doc_by_public_token(token) if token else None
    if not doc:
        return _json_error(request, 404, "not_found")
    return _json(request, {"title": doc["title"], "body_md": doc["body_md"],
                           "updated_at": doc.get("updated_at")})


async def public_doc_view(request: Request) -> Response:
    """Page de partage PUBLIQUE d'un doc — route `/p/d/<token>`, **server-rendered**
    pour être lisible par un agent (WebFetch sans JS) autant que par un navigateur.
    Négocie sur `Accept` : `application/json` → JSON, `text/markdown` → markdown brut,
    sinon HTML autoporté (`public_doc_page`). PAS d'auth, lecture seule."""
    from .. import public_doc_page
    token = request.path_params.get("token", "")
    doc = db.get_doc_by_public_token(token) if token else None
    accept = request.headers.get("accept", "").lower()
    wants_json = "application/json" in accept
    if not doc:
        if wants_json:
            return _json_error(request, 404, "not_found")
        return HTMLResponse(public_doc_page.render_missing(), status_code=404)
    title, body_md = doc["title"], doc.get("body_md") or ""
    if wants_json:
        return _json(request, {"title": title, "body_md": body_md,
                               "updated_at": doc.get("updated_at")})
    if "text/markdown" in accept:
        md = f"# {title}\n\n{body_md}" if title else body_md
        return PlainTextResponse(md, media_type="text/markdown; charset=utf-8",
                                 headers={"Cache-Control": "public, max-age=300"})
    html_page = public_doc_page.render(title=title, body_md=body_md,
                                       updated_at=doc.get("updated_at"))
    return HTMLResponse(html_page, headers={"Cache-Control": "public, max-age=300"})


async def outreach_unsubscribe(request: Request) -> Response:
    """Désinscription des relances — route `/o/u/<token>`, **sans auth**.

    Le jeton signé EST l'autorisation : demander une session ici ferait dépendre un
    refus de la capacité à se reconnecter, alors que c'est précisément la personne qui
    ne veut plus rien avoir à faire avec nous. Server-rendered, sans JS : un lien de
    désinscription doit marcher dans un webmail d'entreprise comme dans un lecteur
    texte.

    **GET qui écrit**, en connaissance de cause : les clients mail ne savent poster
    que depuis un formulaire, et l'écriture est idempotente et strictement
    soustractive (elle ne fait que RETIRER un destinataire). Un préchargeur qui
    suivrait le lien désinscrirait quelqu'un — assumé : la conséquence d'un faux
    positif est de ne plus recevoir de la publicité, celle du sens inverse est
    d'écrire à qui n'en veut plus.
    """
    from .. import outreach_optout
    from ..db import outreach as db_outreach
    sub = outreach_optout.verify(request.path_params.get("token", ""))
    if not sub:
        return HTMLResponse(outreach_optout.page_refus(), status_code=400)
    db_outreach.desinscrire(sub, source="link")
    # La langue de la page de confirmation suit la préférence DÉCLARÉE du compte,
    # comme le mail qui a porté le lien. Compte inconnu (supprimé entre-temps) ⇒ FR :
    # le refus est enregistré quand même, il ne dépend pas de l'existence d'une fiche.
    locale = (db.get_user(sub) or {}).get("locale")
    return HTMLResponse(outreach_optout.page_confirmation(locale),
                        headers={"Cache-Control": "no-store"})
