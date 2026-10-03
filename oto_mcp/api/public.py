"""Handlers des surfaces REST **publiques** — servies sans en-tête d'auth.

Ce qu'elles ont en commun n'est pas un sujet métier mais un régime : l'adaptateur
REST des capacités authentifie TOUJOURS, donc une surface anonyme ne peut pas y
passer et reste écrite à la main.
Trois d'entre elles sont consommées par un PROGRAMME sans en-tête : le build du
site vitrine (`refresh-catalog.mjs` → catalog/connectors) et celui de docs.oto.cx
(`refresh-openapi.mjs` → openapi.json).

Les vitrines anonymes des guides (`/api/guide-library[/{slug}]`, marché, et
`/api/guides/library[/{slug}]`, plateforme) sont RETIRÉES (otomata-tech/oto#84) :
elles servaient le corps complet sans jeton, et la version servie survivait au
nettoyage du dépôt. Les guides se lisent authentifié (`/api/me/guide-library`,
`oto_guide`).

- `GET /favicon.svg` + `/favicon.ico`      → mark de marque (l'endpoint MCP n'a pas de page racine)
- `GET /api/version`                       → la version SERVIE (`version.py`)
- `GET /api/mcp/catalog`                   → catalogue des tools MCP (autodoc)
- `GET /openapi.json` + `/api/openapi.json` → descriptif REST dérivé (`openapi.py`)
- `GET /api/connectors`                    → catalogue des connecteurs (auth OPTIONNELLE)
- `GET /api/invitations/{token}`            → aperçu d'invitation (le jeton EST le secret)
- `GET /api/public/docs/{token}`           → doc partagé (JSON)
- `GET /p/d/{token}`                       → le même, server-rendered (lisible par un agent sans JS)
- `GET /o/u/{token}`                       → désinscription d'une relance (le jeton EST le secret)
- `GET /o/d/{token}`                       → désinscription du DIGEST de signaux (oto#150), même régime, table distincte

`/api/connectors` est la seule MIXTE : anonyme pour la vitrine, authentifiée pour
le dashboard qui y scope son catalogue sur l'org active — d'où son `verifier`.

La table de routes (chemins, méthodes, ORDRE) reste assemblée dans
`api.routes.make_routes` : l'ordre de montage est un contrat, il se lit d'un seul
endroit. Ce module ne porte que les handlers.
"""
from __future__ import annotations

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .. import access, providers, db, openapi, org_store, version as oto_version
from ..capabilities._types import ContratDeRoute
from ..connectors import activation as connector_activation
from ..connectors import cardinality as connector_cardinality
from .base import _authenticate, _file, _json, _json_error, en_thread


async def favicon(request: Request) -> Response:
    """Favicon de marque servi sur mcp.oto.cx (mark canonique, aligné oto.cx).

    L'endpoint MCP n'a pas de page HTML racine → un navigateur/annuaire qui
    sonde `/favicon.svg` ou `/favicon.ico` tombait sur un 404 (aucune icône
    de marque). On sert le mark Otomata (source unique `brand.py`) sur les
    deux chemins.
    """
    from .. import brand
    return _file(
        request,
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


version.contrat = ContratDeRoute(
    description=(
        "La version SERVIE par ce processus, sans auth. `version` est l'étiquette "
        "`<ref>+<commit court>` que portent aussi `info.version` de ce document et "
        "l'en-tête `X-Oto-Version` de chaque réponse ; `oto_core` le tag oto-core "
        "réellement installé. `deps_sha`, `lock_sha` et `deps_conformes` disent quelles "
        "dépendances sont installées et si elles égalent le verrou `uv.lock` de l'arbre "
        "— relevés une fois, au démarrage du processus."),
    Output=oto_version.VersionServie,
    authentifiee=False,
)


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
    if not request.headers.get("authorization"):
        return await run_in_threadpool(_catalogue_public, request)
    sub, err = await _authenticate(request, verifier)
    if err:
        return err
    return await run_in_threadpool(_catalogue_du_requerant, request, sub)


def _catalogue_public(request: Request) -> JSONResponse:
    """Le corps SYNCHRONE de `connectors_catalog` sans requérant : les lectures de
    l'activation et de la cardinalité parlent à la base, donc jamais dans la boucle."""
    cat = providers.public_catalog()
    exposed = connector_activation.exposed_connectors(None)
    cat = [c for c in cat if c["name"] in exposed]
    cat = [c for c in cat if c["availability"] != "platform_granted"]
    # Aucun requérant ⟹ aucune org de contexte : la cardinalité servie ne peut
    # être que le défaut du code (surchargeable seulement au cran PLATEFORME,
    # que l'overlay applique aussi avec `org=None`). C'est la vitrine.
    return _json(request, {"connectors": connector_cardinality.overlay_for_org(cat, None)})


def _catalogue_du_requerant(request: Request, sub: str) -> JSONResponse:
    """Idem, pour un requérant authentifié (org de contexte, visibilité)."""
    cat = providers.public_catalog()
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


@en_thread
def invite_preview(request: Request) -> JSONResponse:
    """Aperçu PUBLIC d'une invitation (pas d'auth — le token est le secret).
    Alimente la page d'accueil « vous êtes invité·e » avant la création de
    compte : email visé + inviteur, pour accompagner l'onboarding."""
    p = org_store.preview_invitation(request.path_params.get("token", ""))
    if not p:
        return _json_error(request, 404, "invalid_or_expired")
    return _json(request, p)


@en_thread
def public_doc(request: Request) -> JSONResponse:
    """Lecture publique d'un doc partagé par token (gap #4a) — PAS d'auth,
    lecture seule. Le dashboard rend le markdown sur sa route publique /p/d/<token>."""
    token = request.path_params.get("token", "")
    doc = db.get_doc_by_public_token(token) if token else None
    if not doc:
        return _json_error(request, 404, "not_found")
    return _json(request, {"title": doc["title"], "body_md": doc["body_md"],
                           "updated_at": doc.get("updated_at")})


@en_thread
def public_doc_view(request: Request) -> Response:
    """Page de partage PUBLIQUE d'un doc — route `/p/d/<token>`, **server-rendered**
    pour être lisible par un agent (WebFetch sans JS) autant que par un navigateur.
    Négocie sur `Accept` : `application/json` → JSON, `text/markdown` → markdown brut,
    sinon HTML autoporté (`public_doc_page`). PAS d'auth, lecture seule."""
    from .. import public_doc_page
    token = request.path_params.get("token", "")
    doc = db.get_doc_by_public_token(token) if token else None
    accept = request.headers.get("accept", "").lower()
    wants_json = "application/json" in accept
    # La marque du PROPRIÉTAIRE, résolue depuis la donnée que la lecture remonte —
    # le lecteur n'a ni compte ni session, et le jeton seul ne dit rien de qui partage.
    # Jeton inconnu ⟹ pas de propriétaire ⟹ la nôtre : on ne peut rien déduire d'un
    # jeton qui ne désigne rien, et supposer serait pire que dire « nous ».
    from .. import brand
    marque = (brand.marque_du_proprietaire(doc.get("owner_type"), doc.get("owner_id"))
              if doc else None)
    # Les trois variantes portent le MÊME secret d'URL (le jeton) : les en-têtes qui
    # empêchent sa fuite (cache partagé, Referer, sniffing, cadre) doivent porter sur
    # les trois, pas seulement sur celle qu'on a regardée en premier — c'est l'oubli
    # qui a laissé passer markdown et JSON nus (oto-backend#565). La CSP est celle de
    # la page HTML (`entetes_securite`) ; sur les deux autres variantes elle est inerte.
    from ..entetes_securite import CSP_DOC_PUBLIC
    _entetes_page_a_jeton = {
        "Cache-Control": "private, max-age=300",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": CSP_DOC_PUBLIC,
    }
    if not doc:
        if wants_json:
            return _json_error(request, 404, "not_found")
        # La page d'un jeton périmé porte le jeton dans son URL comme les autres.
        # Jamais en cache, lui : un jeton re-partagé ne doit pas rester « introuvable ».
        return HTMLResponse(public_doc_page.render_missing(), status_code=404,
                            headers={**_entetes_page_a_jeton, "Cache-Control": "no-store"})
    title, body_md = doc["title"], doc.get("body_md") or ""
    if wants_json:
        return _json(request, {"title": title, "body_md": body_md,
                               "updated_at": doc.get("updated_at")},
                     extra_headers=_entetes_page_a_jeton)
    if "text/markdown" in accept:
        md = f"# {title}\n\n{body_md}" if title else body_md
        return _file(request, md, media_type="text/markdown; charset=utf-8",
                     headers=_entetes_page_a_jeton)
    html_page = public_doc_page.render(title=title, body_md=body_md,
                                       updated_at=doc.get("updated_at"), marque=marque)
    return HTMLResponse(html_page, headers=_entetes_page_a_jeton)


@en_thread
def outreach_unsubscribe(request: Request) -> Response:
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
    # Un mail servi dans une langue FIXÉE (l'activation, en anglais) le dit dans son
    # lien (`?lang=en`) : la page parle alors la langue du mail qu'on vient de lire.
    lang = request.query_params.get("lang")
    locale = lang if lang in ("fr", "en") else (db.get_user(sub) or {}).get("locale")
    return HTMLResponse(outreach_optout.page_confirmation(locale),
                        headers={"Cache-Control": "no-store"})


@en_thread
def digest_unsubscribe(request: Request) -> Response:
    """Désinscription du DIGEST de signaux (oto#150) — route `/o/d/<token>`,
    **sans auth**. Sœur d'`outreach_unsubscribe`, même régime (GET qui écrit,
    server-rendered, idempotent, strictement soustractif) — voir son docstring pour
    ce qui le justifie.

    ⚠️ **Table distincte, jamais `db_outreach.desinscrire`** : ce jeton porte un
    `typ` propre (`outreach_optout.verify_digest`, jamais `verify`), et l'écriture
    va dans `signal_digest_optouts` (`db.usage.opt_out_signal_digest`) — décision
    d'Alexis (oto#150), le digest de signaux et les relances sont deux canaux, deux
    refus. Un jeton de relance présenté ici est refusé (mauvais `typ`), comme
    l'inverse sur `/o/u/<token>`.
    """
    from .. import outreach_optout
    from ..db import usage as db_usage
    sub = outreach_optout.verify_digest(request.path_params.get("token", ""))
    if not sub:
        return HTMLResponse(outreach_optout.page_refus(), status_code=400)
    db_usage.opt_out_signal_digest(sub, source="link")
    # Même raisonnement que `outreach_unsubscribe` : la langue suit la préférence
    # DÉCLARÉE du compte, et le refus se pose même si le compte est introuvable.
    locale = (db.get_user(sub) or {}).get("locale")
    return HTMLResponse(outreach_optout.page_confirmation(locale, kind="digest"),
                        headers={"Cache-Control": "no-store"})
