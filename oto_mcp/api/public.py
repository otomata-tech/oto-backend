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
- `GET|POST /o/u/{token}`                  → désinscription d'une relance (le jeton EST le secret ; GET demande, POST désinscrit)
- `GET|POST /o/d/{token}`                  → désinscription du DIGEST de signaux (oto#150), même régime, table distincte
- `GET /api/public/process-shares/{token}` → VITRINE d'une procédure partagée par lien (jamais le corps)
- `GET|POST /o/r/{token}`                  → désinscription du résumé des LECTEURS d'une procédure partagée, même régime
- `GET /api/public/community/processes[/{slug}]` → VITRINE de la communauté (métadonnées seules, jamais le corps)
- `GET /api/public/community/apps[/{slug}]` → même régime pour les apps (jamais le HTML)

`/api/connectors` est la seule MIXTE : anonyme pour la vitrine, authentifiée pour
le dashboard qui y scope son catalogue sur l'org active — d'où son `verifier`.

La table de routes (chemins, méthodes, ORDRE) reste assemblée dans
`api.routes.make_routes` : l'ordre de montage est un contrat, il se lit d'un seul
endroit. Ce module ne porte que les handlers.
"""
from __future__ import annotations

import hashlib
import threading
import weakref

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


# Le descriptif public pèse ~1,2 Mo et sa construction tient la boucle des centaines de
# millisecondes (oto-backend#1232) : sur une route anonyme, c'était le moyen le moins cher
# de charger le processus. Il ne dépend que de ce qui est figé au démarrage — la table de
# routes, le registre de capacités, la version servie, les drapeaux d'environnement —, et
# de l'adresse par laquelle on est entré (`servers`). On le rend donc UNE fois par
# application, sans `servers`, et on ajoute `servers` à chaque réponse : c'est la dernière
# clé du document, le corps reste octet pour octet celui que `_json` sérialisait.
_DESCRIPTIFS: "weakref.WeakKeyDictionary[object, _DescriptifRendu]" = weakref.WeakKeyDictionary()
_VERROU_DESCRIPTIF = threading.Lock()
# Il ne change qu'au déploiement : cinq minutes de fraîcheur, puis une revalidation que
# l'`ETag` rend gratuite (304, sans corps). `Vary: Origin` parce que l'en-tête CORS, lui,
# suit l'origine : un cache partagé ne doit pas resservir celui d'une autre.
_ENTETES_DESCRIPTIF = {"Cache-Control": "public, max-age=300", "Vary": "Origin"}


def _en_json(contenu) -> bytes:
    """La sérialisation de `_json` (celle de `JSONResponse`), pour en garder les octets."""
    return JSONResponse(contenu).body


class _DescriptifRendu:
    """Le document public rendu, sans `servers`, et son empreinte."""

    __slots__ = ("corps", "empreinte")

    def __init__(self, corps: bytes):
        if not corps.endswith(b"}"):
            raise ValueError("le descriptif OpenAPI rendu n'est pas un objet JSON")
        self.corps = corps
        self.empreinte = hashlib.sha256(corps).digest()

    def etag(self, server_url: str | None) -> str:
        """`ETag` FORT : l'empreinte du corps servi, que fixent le document rendu et
        l'adresse ajoutée. Composée plutôt que recalculée sur 1,2 Mo à chaque requête."""
        h = hashlib.sha256(self.empreinte)
        h.update((server_url or "").encode())
        return f'"{h.hexdigest()[:32]}"'

    def corps_pour(self, server_url: str | None) -> bytes:
        if not server_url:
            return self.corps
        return self.corps[:-1] + b',"servers":' + _en_json([{"url": server_url}]) + b"}"


def _descriptif_de(app) -> _DescriptifRendu:
    """Rend le descriptif de `app` une seule fois, dans le threadpool : plusieurs
    premières requêtes simultanées attendent le même rendu au lieu d'en lancer chacune un."""
    with _VERROU_DESCRIPTIF:
        rendu = _DESCRIPTIFS.get(app)
        if rendu is None:
            rendu = _DescriptifRendu(_en_json(openapi.build(app.routes)))
            _DESCRIPTIFS[app] = rendu
        return rendu


def _deja_en_main(si_aucun: str | None, etag: str) -> bool:
    """`If-None-Match` (RFC 9110 §13.1.2) : comparaison FAIBLE, `*` compris."""
    if not si_aucun:
        return False
    for candidat in si_aucun.split(","):
        candidat = candidat.strip()
        if candidat == "*" or candidat.removeprefix("W/") == etag:
            return True
    return False


async def openapi_doc(request: Request) -> Response:
    """Descriptif OpenAPI de l'API REST — **dérivé** du registre de capacités et
    de la table de routes VIVANTE (`request.app.routes`), donc jamais désynchronisé.

    Pas d'auth, comme `/api/mcp/catalog` : un descriptif d'API décrit des FORMES,
    aucune valeur. Sans lui, chaque intégrateur redécouvre la surface par sondage
    de chemins — et conclut faux (cf. `openapi.py`). `/api/admin/*` est exclu.

    Rendu une fois par application, jamais sur la boucle, puis servi de la mémoire avec
    un `ETag` fort et un 304 sur `If-None-Match` (oto-backend#1232).
    """
    app = request.app
    rendu = _DESCRIPTIFS.get(app)
    if rendu is None:
        rendu = await run_in_threadpool(_descriptif_de, app)
    base = str(request.base_url).rstrip("/") or None
    etag = rendu.etag(base)
    entetes = {**_ENTETES_DESCRIPTIF, "ETag": etag}
    if _deja_en_main(request.headers.get("if-none-match"), etag):
        return _file(request, b"", media_type="application/json", headers=entetes,
                     status_code=304)
    return _file(request, rendu.corps_pour(base), media_type="application/json",
                 headers=entetes)


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


# Le lien porte son secret dans le CHEMIN : ni cache partagé, ni Referer vers une
# ressource tierce, quelle que soit la page servie (question, confirmation, refus).
_ENTETES_DESINSCRIPTION = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _desinscription(request: Request, *, sub: str | None, ecrire, kind: str) -> Response:
    """Le régime commun des deux liens de désinscription (`/o/u/`, `/o/d/`).

    **GET : une question, jamais une écriture.** Le lien voyage hors des mains du
    destinataire — dans les copies (`cc`) d'un email d'activation, que reçoit l'équipe
    du tenant, et sous les yeux des scanners de liens des messageries d'entreprise.
    Un GET qui écrivait désinscrivait le destinataire à sa place. La page pose donc la
    question, et c'est son formulaire qui POSTe.

    **POST : la désinscription**, quel que soit le corps — le formulaire de la page,
    ou le désabonnement en un clic d'un client mail (RFC 8058, `List-Unsubscribe=
    One-Click`). Le jeton signé du chemin EST l'autorisation. Idempotent et strictement
    soustractif : un second POST n'est pas une erreur.

    Un jeton illisible n'écrit rien, quelle que soit la méthode (400, « lien invalide »).
    La langue suit `?lang=` (un mail servi dans une langue FIXÉE, l'activation, le dit
    dans son lien), sinon la préférence DÉCLARÉE du compte ; compte inconnu ⇒ FR, et le
    refus se pose quand même — il ne dépend pas de l'existence d'une fiche.
    """
    from .. import outreach_optout
    if not sub:
        return HTMLResponse(outreach_optout.page_refus(), status_code=400,
                            headers=_ENTETES_DESINSCRIPTION)
    lang = request.query_params.get("lang")
    locale = lang if lang in ("fr", "en") else (db.get_user(sub) or {}).get("locale")
    if request.method != "POST":
        return HTMLResponse(outreach_optout.page_demande(locale, kind=kind),
                            headers=_ENTETES_DESINSCRIPTION)
    ecrire(sub)
    return HTMLResponse(outreach_optout.page_confirmation(locale, kind=kind),
                        headers=_ENTETES_DESINSCRIPTION)


@en_thread
def outreach_unsubscribe(request: Request) -> Response:
    """Désinscription des relances — route `/o/u/<token>`, **sans auth** (GET : la
    question ; POST : le refus, cf. `_desinscription`).

    Le jeton signé EST l'autorisation : demander une session ici ferait dépendre un
    refus de la capacité à se reconnecter, alors que c'est précisément la personne qui
    ne veut plus rien avoir à faire avec nous. Server-rendered, sans JS : un lien de
    désinscription doit marcher dans un webmail d'entreprise comme dans un lecteur
    texte. Le refus va dans `outreach_optouts`, COMMUN aux relances et à l'activation.
    """
    from .. import outreach_optout
    from ..db import outreach as db_outreach
    return _desinscription(
        request, sub=outreach_optout.verify(request.path_params.get("token", "")),
        ecrire=lambda sub: db_outreach.desinscrire(sub, source="link"), kind="relance")


@en_thread
def digest_unsubscribe(request: Request) -> Response:
    """Désinscription du DIGEST de signaux (oto#150) — route `/o/d/<token>`,
    **sans auth**. Sœur d'`outreach_unsubscribe`, même régime (`_desinscription`).

    ⚠️ **Table distincte, jamais `db_outreach.desinscrire`** : ce jeton porte un
    `typ` propre (`outreach_optout.verify_digest`, jamais `verify`), et l'écriture
    va dans `signal_digest_optouts` (`db.usage.opt_out_signal_digest`) — décision
    d'Alexis (oto#150), le digest de signaux et les relances sont deux canaux, deux
    refus. Un jeton de relance présenté ici est refusé (mauvais `typ`), comme
    l'inverse sur `/o/u/<token>`.
    """
    from .. import outreach_optout
    from ..db import usage as db_usage
    return _desinscription(
        request, sub=outreach_optout.verify_digest(request.path_params.get("token", "")),
        ecrire=lambda sub: db_usage.opt_out_signal_digest(sub, source="link"),
        kind="digest")


# Le seau anti-martelage de la vitrine d'un partage de procédure. Il réutilise celui des
# projets publiés sans login (`subdomain_project`, mêmes réglages `OTO_ANON_RATE_*`) sous
# une clé à lui : l'id de projet 0 n'existe pas (BIGSERIAL), donc `(ip, 0)` ne se
# confond avec le seau d'aucun projet.
_SEAU_VITRINE = 0


@en_thread
def process_share_preview(request: Request) -> JSONResponse:
    """La VITRINE d'une procédure partagée par lien — `GET /api/public/process-shares/
    {token}`, sans auth (le jeton est le secret ; masqué au journal par son nom).

    ⚠️ Jamais le corps, ni aucun texte d'étape : titre, description, auteur,
    connecteurs, et la FORME du graphe validée à l'écriture par un schéma fermé
    (`capabilities/partages_procedure.vitrine`, seul constructeur de cette réponse,
    partagé avec la lecture connectée). Le retrait des vitrines anonymes de la
    bibliothèque (oto#84) est précisément ce qui l'exige.

    Compte une vue par jour et par partage — un nombre, aucune IP gardée."""
    import time

    from .. import subdomain_project
    from ..capabilities import partages_procedure as partages
    from ..capabilities._types import AuthzDenied
    from ..db import partages_procedure as db_partages
    ip = subdomain_project._client_ip(request.scope, dict(request.scope.get("headers") or []))
    if not subdomain_project._check_bucket((ip, _SEAU_VITRINE), time.monotonic()):
        return _json_error(request, 429, "rate_limited", "Trop de requêtes, réessayez.")
    try:
        share, instr = partages._procedure_partagee(request.path_params.get("token", ""))
    except AuthzDenied:
        return _json_error(request, 404, "not_found")
    db_partages.compter_vue(share["id"])
    return _json(request, partages.vitrine(share, instr), extra_headers={
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff"})


@en_thread
def readers_digest_unsubscribe(request: Request) -> Response:
    """Désinscription du RÉSUMÉ DES LECTEURS d'une procédure partagée — route
    `/o/r/<token>`, **sans auth**. Même régime que `digest_unsubscribe` (`_desinscription` :
    GET demande, POST désinscrit) ; jeton (`typ`) et table
    distincts : il ne désinscrit ni des relances ni du digest de signaux."""
    from .. import outreach_optout
    from ..db import partages_procedure as db_partages
    return _desinscription(
        request, sub=outreach_optout.verify_lecteurs(request.path_params.get("token", "")),
        ecrire=lambda sub: db_partages.refuser_resume(sub, source="link"),
        kind="lecteurs")


# Le seau de la vitrine de la COMMUNAUTÉ : même réglage que les autres surfaces anonymes,
# clé à part (-1 n'est l'id d'aucun projet, ni le 0 de la vitrine d'un partage).
_SEAU_COMMUNAUTE = -1
#: Plafond de `limit` sur la liste anonyme, et son défaut.
COMMUNAUTE_LIMITE_MAX, COMMUNAUTE_LIMITE_DEFAUT = 200, 100
#: Cache public court : le build du site relit, un navigateur ne martèle pas.
_CACHE_COMMUNAUTE = "public, max-age=300"


def _communaute_refus_rythme(request: Request):
    import time

    from .. import subdomain_project
    ip = subdomain_project._client_ip(request.scope, dict(request.scope.get("headers") or []))
    if not subdomain_project._check_bucket((ip, _SEAU_COMMUNAUTE), time.monotonic()):
        return _json_error(request, 429, "rate_limited", "Trop de requêtes, réessayez.")
    return None


def _communaute_entetes() -> dict[str, str]:
    return {"Cache-Control": _CACHE_COMMUNAUTE, "X-Content-Type-Options": "nosniff"}


@en_thread
def community_processes(request: Request) -> JSONResponse:
    """La VITRINE de la communauté — `GET /api/public/community/processes`, sans auth.
    Lue au build de la page communauté du site (SEO) et par tout visiteur.

    ⚠️ Métadonnées seules, par la liste FERMÉE `capabilities.communaute.vitrine` :
    jamais `body_md` (retrait des vitrines anonymes, oto#84), jamais un identifiant
    d'org ou de compte. Seules les entrées `public` et listées : ni `unlisted` (un lien
    secret), ni ce que le personnel a retiré.

    Paramètres : `sort` (trending | most_added | new ; absent = dernière publication
    d'abord), `category`, `limit` (défaut 100, plafond 200). Un paramètre invalide = 400."""
    from ..capabilities import communaute
    refus = _communaute_refus_rythme(request)
    if refus is not None:
        return refus
    q = request.query_params
    sort = q.get("sort") or None
    if sort is not None and sort not in org_store.SORTS:
        return _json_error(request, 400, "bad_sort",
                           f"`sort` attendu : {' | '.join(org_store.SORTS)}.")
    try:
        limit = int(q.get("limit") or COMMUNAUTE_LIMITE_DEFAUT)
    except ValueError:
        return _json_error(request, 400, "bad_limit", "`limit` doit être un entier.")
    limit = max(1, min(limit, COMMUNAUTE_LIMITE_MAX))
    rows = org_store.list_library(category=q.get("category") or None,
                                  include_unlisted=False, limit=limit, sort=sort)
    return _json(request, {"processes": [communaute.vitrine(r) for r in rows]},
                 extra_headers=_communaute_entetes())


@en_thread
def community_process(request: Request) -> JSONResponse:
    """Une entrée de la vitrine — `GET /api/public/community/processes/{slug}`, sans
    auth. Mêmes champs que la liste. 404 identique pour une entrée inconnue, `unlisted`
    ou retirée : la surface anonyme ne dit pas laquelle."""
    from ..capabilities import communaute
    refus = _communaute_refus_rythme(request)
    if refus is not None:
        return refus
    entry = org_store.get_library_entry(slug=request.path_params.get("slug", ""),
                                        include_unlisted=False, include_removed=False)
    if not entry:
        return _json_error(request, 404, "not_found")
    return _json(request, communaute.vitrine(entry), extra_headers=_communaute_entetes())


def _limite(request: Request):
    try:
        return max(1, min(int(request.query_params.get("limit") or COMMUNAUTE_LIMITE_DEFAUT),
                          COMMUNAUTE_LIMITE_MAX)), None
    except ValueError:
        return None, _json_error(request, 400, "bad_limit", "`limit` doit être un entier.")


@en_thread
def community_apps(request: Request) -> JSONResponse:
    """La vitrine des APPS de la communauté — `GET /api/public/community/apps`, sans
    auth. Même régime que `community_processes` : métadonnées seules par la liste fermée
    `capabilities.communaute.vitrine_app` (jamais le HTML, le manifeste ni un schéma,
    jamais un identifiant d'org ou de compte), entrées `public` et listées seulement."""
    from ..capabilities import communaute
    from ..db import app_library
    refus = _communaute_refus_rythme(request)
    if refus is not None:
        return refus
    sort = request.query_params.get("sort") or None
    if sort is not None and sort not in org_store.SORTS:
        return _json_error(request, 400, "bad_sort",
                           f"`sort` attendu : {' | '.join(org_store.SORTS)}.")
    limit, refus = _limite(request)
    if refus is not None:
        return refus
    rows = app_library.lister(category=request.query_params.get("category") or None,
                              sort=sort, limit=limit)
    return _json(request, {"apps": [communaute.vitrine_app(r) for r in rows]},
                 extra_headers=_communaute_entetes())


@en_thread
def community_app(request: Request) -> JSONResponse:
    """Une app de la vitrine — `GET /api/public/community/apps/{slug}`, sans auth. 404
    identique pour une entrée inconnue, `unlisted` ou retirée."""
    from ..capabilities import communaute
    from ..db import app_library
    refus = _communaute_refus_rythme(request)
    if refus is not None:
        return refus
    entry = app_library.lire(slug=request.path_params.get("slug", ""))
    if not entry:
        return _json_error(request, 404, "not_found")
    return _json(request, communaute.vitrine_app(entry), extra_headers=_communaute_entetes())
