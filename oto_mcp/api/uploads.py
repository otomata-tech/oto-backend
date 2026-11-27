"""Handlers de la RÉCEPTION d'un upload signé (issue #105) — `/api/upload/{token}`.

**Pas de JWT** : le jeton scellé DANS l'URL fait foi (sub / org / cible, TTL court,
usage unique, `upload_tokens.py`). Trois voies pour le même jeton :

- `PUT`  → un agent qui a un shell y pousse le corps brut (`curl --data-binary`)
- `POST` → multipart `file`, le formulaire humain (fallback quand l'agent n'a pas
           de shell : claude.ai transmet le lien à la personne)
- `GET`  → la page HTML d'upload, autoportée (aucun asset externe,
           `api/upload_page.py`) ; le jeton n'y est PAS consommé, seulement au
           POST du fichier

La matérialisation RÉAPPLIQUE l'autz de la cible (`check_target_access`), borne la
taille, puis consomme le jeton AVANT d'écrire — anti-rejeu et anti-double-écriture.
L'accusé est léger : jamais le corps reçu.

⚠️ **Le plafond de taille se tient PENDANT la lecture, pas après** (#562). Jusqu'au
23/09/2026, le PUT faisait `await request.body()` et le POST `await request.form()`
sans borne, puis comparait la longueur au plafond : un porteur du lien — un tiers, par
construction — faisait tenir en mémoire (ou spooler sur disque) un corps de taille
arbitraire, sur un serveur mono-boucle. Le corps se lit désormais en flux, et la
lecture s'arrête au premier octet au-delà du plafond ; un `Content-Length` déclaré
au-delà est refusé sans rien lire.

La table de routes (chemins, méthodes, ORDRE) reste assemblée dans
`api.routes.make_routes` ; ce module ne porte que les handlers — et leur CONTRAT
(`ContratDeRoute`, oto#106) : la route ne sera jamais une capacité (corps brut, pas de
JWT), mais un client REST doit pouvoir l'ouvrir en ne lisant que `/openapi.json`. Il y
voyait une souche « écrite à la main », sans corps ni accusé ni refus.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .. import db, geste, upload_tokens
from ..capabilities._types import ContratDeRoute, DeclaredError
from .base import _file, _json, _json_error, _locale_from_accept_language
from .upload_page import SCRIPT_UPLOAD, upload_page_html


# Ce que l'enveloppe multipart ajoute autour du fichier (frontières, en-têtes de
# partie, nom de fichier) : le formulaire humain n'envoie qu'une partie `file`. Le
# plafond du FICHIER reste `max_bytes()`, revérifié sur la partie extraite.
_MARGE_MULTIPART = 64 * 1024


class _CorpsTropGros(Exception):
    """Le corps dépasse le plafond : la lecture s'est arrêtée là."""


def _annonce_trop_gros(request: Request, plafond: int) -> bool:
    """Un `Content-Length` déclaré au-delà du plafond se refuse sans rien lire. Absent
    (envoi `chunked`), c'est la lecture en flux qui tient la borne."""
    annonce = request.headers.get("content-length", "")
    return annonce.isdigit() and int(annonce) > plafond


async def _lire_borne(request: Request, plafond: int) -> bytes:
    """Lit le corps en flux et s'ARRÊTE dès que `plafond` est dépassé (#562) — jamais
    un `request.body()` qui prendrait tout avant de compter."""
    recu = bytearray()
    async for morceau in request.stream():
        recu += morceau
        if len(recu) > plafond:
            raise _CorpsTropGros
    return bytes(recu)


def _rejouer(corps: bytes):
    """Un `receive` ASGI qui rend le corps déjà lu — pour confier au parseur multipart
    un corps borné plutôt que le flux brut de la requête."""
    async def _receive() -> dict:
        return {"type": "http.request", "body": corps, "more_body": False}
    return _receive


async def _do_signed_upload(request: Request, payload: dict, data: bytes,
                            ct: str | None) -> JSONResponse:
    """Cœur commun des réceptions d'upload signé : autz réappliquée → borne de
    taille → consommation à usage unique → matérialisation. DB sync → threadpool."""
    from .. import garde_identite, upload_tokens
    sub, target = payload["sub"], payload["target"]
    # Le lien porte le sub SCELLÉ au mint, et sert sans aucun jeton : c'est un porteur
    # d'identité à part entière, 15 minutes durant. Il passe donc par la garde d'identité
    # comme les autres portes — compte en pause, tenant désactivé (#1165).
    if (coupe := await run_in_threadpool(garde_identite.refus, sub)):
        return _json_error(request, 403, *coupe)
    try:
        await run_in_threadpool(upload_tokens.check_target_access, sub, target)
    except upload_tokens.UploadError as e:
        return _json_error(request, e.status, e.code, e.message or None)
    if not data:
        return _json_error(request, 400, "empty_body")
    if len(data) > upload_tokens.max_bytes():
        return _json_error(request, 413, "content_too_large")
    # Consommer AVANT de matérialiser (anti-rejeu / double-écriture).
    if not await run_in_threadpool(db.consume_upload_token, payload["jti"]):
        return _json_error(request, 409, "token_already_used")
    # Le GESTE (oto#273) : `upload`. Il ne devient `import` que si le jeton a scellé
    # `donnees_d_origine=true` au mint — c'est le store qui requalifie, comme pour toute
    # face : un fichier n'est pas une origine parce qu'il est un fichier. L'acteur est
    # le compte scellé au jeton (celui qui l'a frappé, pas le porteur anonyme du lien),
    # et le geste est le jeton lui-même : son `jti`, à usage unique, désigne ce dépôt.
    try:
        with geste.portee(geste.UPLOAD, sub, payload["jti"]):
            result = await run_in_threadpool(upload_tokens.materialize, sub, target,
                                             data, ct)
    except upload_tokens.UploadError as e:
        # La phrase part avec le code : « ligne NDJSON 4 312 invalide » dit où reprendre,
        # `bad_ndjson` seul ne le dit pas (oto#106).
        return _json_error(request, e.status, e.code, e.message or None)
    return _json(request, result)


class CorpsRefuse(Exception):
    """Un corps refusé à la lecture : `status` et `code` de la réponse d'erreur."""

    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


async def lire_corps_borne(request: Request, plafond: int) -> bytes:
    """Le corps brut, refusé au-delà de `plafond` : sur l'annonce, sinon en cours de
    lecture (#562)."""
    if _annonce_trop_gros(request, plafond):
        raise CorpsRefuse(413, "content_too_large")
    try:
        return await _lire_borne(request, plafond)
    except _CorpsTropGros:
        raise CorpsRefuse(413, "content_too_large") from None


async def lire_multipart_borne(request: Request, plafond: int):
    """Un formulaire multipart dont la partie `file` pèse au plus `plafond` : rend
    `(form, upload, octets)`. Le parseur lit le corps DÉJÀ BORNÉ (plafond + l'enveloppe)
    et n'accepte qu'une partie fichier. Quelques champs texte restent tolérés
    (`max_fields`) : un formulaire sans fichier, ou un `file` envoyé en texte, doit
    rendre `missing_file`, pas `invalid_multipart` — le corps borné rend leur coût
    négligeable. Lève `CorpsRefuse`."""
    corps = await lire_corps_borne(request, plafond + _MARGE_MULTIPART)
    try:
        form = await Request(request.scope, _rejouer(corps)).form(max_files=1, max_fields=16)
    except Exception:  # noqa: SILENT — tout échec du parseur EST le refus rendu
        raise CorpsRefuse(400, "invalid_multipart") from None
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise CorpsRefuse(400, "missing_file")
    data = await upload.read()
    if len(data) > plafond:
        raise CorpsRefuse(413, "content_too_large")
    return form, upload, data


async def upload_receive(request: Request) -> JSONResponse:
    """Réception d'un upload signé (issue #105) : PAS de JWT — le jeton signé DANS
    l'URL fait foi (scellé sub/org/cible, TTL, usage unique). Deux voies :
    **PUT** = un agent avec shell y pousse le corps brut (`curl --data-binary`) ;
    **POST** multipart `file` = le formulaire humain (fallback claude.ai). On
    matérialise en RÉAPPLIQUANT l'autz de la cible. Accusé léger, jamais le body."""
    payload = upload_tokens.verify(request.path_params.get("token", ""))
    if payload is None:
        return _json_error(request, 401, "invalid_or_expired_token")
    plafond = upload_tokens.max_bytes()
    try:
        if request.method == "POST":
            _form, upload, data = await lire_multipart_borne(request, plafond)
            ct = getattr(upload, "content_type", None)
        else:  # PUT — corps brut
            data = await lire_corps_borne(request, plafond)
            ct = request.headers.get("content-type")
    except CorpsRefuse as e:
        return _json_error(request, e.status, e.code)
    return await _do_signed_upload(request, payload, data, ct)


async def upload_form(request: Request) -> Response:
    """Page HTML d'upload d'un lien signé (GET) — **fallback humain** quand l'agent
    n'a pas de shell (claude.ai lui transmet ce lien). Le jeton n'est PAS consommé
    au GET (seulement au POST du fichier). Autoportée (aucun asset externe)."""
    payload = upload_tokens.verify(request.path_params.get("token", ""))
    from ..entetes_securite import csp_upload
    headers = {"Cache-Control": "private", "Referrer-Policy": "no-referrer",
               "Content-Security-Policy": csp_upload(SCRIPT_UPLOAD)}
    lang = _locale_from_accept_language(request.headers.get("accept-language")) or "en"
    headers["Vary"] = "Accept-Language"
    if payload is None:
        return HTMLResponse(upload_page_html(None, lang), status_code=401, headers=headers)
    return HTMLResponse(
        upload_page_html(upload_tokens.target_label(payload["target"], lang), lang),
        headers=headers
    )


# ── Gros fichiers : dépôt multipart direct au stockage (`large_uploads`) ─────────────

async def _large_payload(request: Request):
    from .. import large_uploads
    payload = large_uploads.verifier(request.path_params.get("token", ""))
    if payload is None:
        return None, _json_error(request, 401, "invalid_or_expired_token")
    return payload, None


async def large_part(request: Request) -> Response:
    """`GET /api/upload/large/{token}/part/{n}` → en TEXTE, l'URL présignée où `PUT` la
    partie n — lisible par `$(curl …)` sans parseur JSON."""
    from .. import large_uploads
    payload, refus = await _large_payload(request)
    if refus:
        return refus
    try:
        url = await run_in_threadpool(large_uploads.url_de_partie, payload,
                                      int(request.path_params["n"]))
    except upload_tokens.UploadError as e:
        return _json_error(request, e.status, e.code, e.message or None)
    return _file(request, url, media_type="text/plain",
                 headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


async def large_complete(request: Request) -> JSONResponse:
    """`POST /api/upload/large/{token}/complete` : clôt le dépôt, range le fichier."""
    from .. import large_uploads
    payload, refus = await _large_payload(request)
    if refus:
        return refus
    try:
        with geste.portee(geste.UPLOAD, payload["sub"], payload["jti"]):
            result = await run_in_threadpool(large_uploads.terminer, payload)
    except upload_tokens.UploadError as e:
        return _json_error(request, e.status, e.code, e.message or None)
    return _json(request, result)


async def large_abort(request: Request) -> JSONResponse:
    """`DELETE /api/upload/large/{token}` : abandonne le dépôt et ses parties."""
    from .. import large_uploads
    payload, refus = await _large_payload(request)
    if refus:
        return refus
    try:
        result = await run_in_threadpool(large_uploads.abandonner, payload)
    except upload_tokens.UploadError as e:
        return _json_error(request, e.status, e.code, e.message or None)
    return _json(request, result)


async def large_form(request: Request) -> Response:
    """La page d'upload d'un gros fichier (GET) : le navigateur découpe le fichier et
    envoie chaque partie au stockage lui-même. La CSP ouvre l'origine du stockage."""
    from .. import large_uploads, media_store
    from ..entetes_securite import csp_upload_large
    from .upload_page import SCRIPT_UPLOAD_LARGE
    payload = large_uploads.verifier(request.path_params.get("token", ""))
    lang = _locale_from_accept_language(request.headers.get("accept-language")) or "en"
    headers = {"Cache-Control": "private", "Referrer-Policy": "no-referrer",
               "Vary": "Accept-Language",
               "Content-Security-Policy": csp_upload_large(
                   SCRIPT_UPLOAD_LARGE, media_store.origines_de_depot())}
    if payload is None:
        return HTMLResponse(upload_page_html(None, lang), status_code=401, headers=headers)
    return HTMLResponse(
        upload_page_html(large_uploads.libelle(payload["target"], lang), lang,
                         part_size=int(payload["target"]["part_size"]),
                         notice=large_uploads.retention_notice(lang)),
        headers=headers)


# ── Le contrat publié (oto#106) ───────────────────────────────────────────────────

class AccuseDeDepot(BaseModel):
    """L'accusé LÉGER d'un dépôt : jamais le corps reçu, jamais un identifiant interne
    (oto#86). Les champs présents dépendent de la cible scellée au jeton (`kind`)."""
    ok: bool
    kind: Literal["doc", "project_file", "datastore", "image"]
    bytes: int = Field(description="taille reçue, en octets")
    op: Optional[Literal["create", "update"]] = Field(None, description="`doc`")
    chars: Optional[int] = Field(None, description="`doc` : caractères de la page")
    filename: Optional[str] = Field(None, description="`project_file`")
    expires_at: Optional[str] = Field(None, description=(
        "`project_file` déposé par parties (`large`) : la date (ISO 8601, UTC) où il est "
        "supprimé automatiquement"))
    notice: Optional[str] = Field(None, description=(
        "`project_file` déposé par parties : la durée de garde, en une phrase à dire à "
        "l'utilisateur"))
    parts: Optional[int] = Field(None, description="`project_file` déposé par parties")
    url: Optional[str] = Field(None, description="`image` : l'adresse publique, permanente")
    datastore: Optional[str] = Field(None, description="`datastore` : le tableau chargé")
    inserted: Optional[int] = Field(None, description="`datastore` : lignes créées")
    updated: Optional[int] = Field(None, description="`datastore` : lignes mises à jour "
                                                     "(même clé d'upsert)")
    count: Optional[int] = Field(None, description="`datastore` : lignes du fichier")
    fusions: Optional[list[dict]] = Field(None, description=(
        "`datastore` : les lignes du fichier qui ont FUSIONNÉ sur la clé (oto#141) — "
        "`{rang, dans_rang, id, cle}`, `dans_rang` = la ligne du fichier qui a posé la "
        "ligne visée, `null` pour une ligne déjà en base"))
    entetes_traduits: Optional[dict[str, str]] = Field(None, description=(
        "`datastore` CSV : en-têtes renommés (un point ne peut pas figurer dans un nom "
        "de colonne) — ancien → nouveau"))
    # Les relevés d'écriture (`valeurs_ecartees`, `notices`…) du
    # datastore s'ajoutent tels quels : ce sont ceux de toute écriture de lignes.
    model_config = {"extra": "allow"}


_REFUS_DE_LA_RECEPTION = (
    DeclaredError(401, "invalid_or_expired_token",
                  "le jeton de l'adresse est illisible, falsifié ou expiré (TTL court) : "
                  "en frapper un autre (`POST /api/me/upload-url`)"),
    DeclaredError(409, "token_already_used",
                  "le jeton est à usage unique et a déjà servi : en frapper un autre"),
    DeclaredError(413, "content_too_large",
                  "le corps dépasse `max_bytes` (rendu à la frappe) — un `Content-Length` "
                  "annoncé au-delà est refusé sans rien lire"),
    DeclaredError(400, "empty_body", "le corps est vide"),
    DeclaredError(400, "invalid_multipart", "`POST` : le corps n'est pas un multipart lisible"),
    DeclaredError(400, "missing_file",
                  "`POST` : pas de partie `file` ; ou un fichier vide (image, fichier "
                  "de projet)"),
    DeclaredError(403, "forbidden",
                  "la cible est RE-jugée à la réception : le compte qui a frappé le jeton "
                  "n'y a plus l'écriture"),
    DeclaredError(404, "unknown_doc", "`doc` : la page visée n'existe plus"),
    DeclaredError(404, "unknown_project", "le projet visé n'existe plus"),
    DeclaredError(400, "bad_parent",
                  "`doc` (`op=create`) : la page parente n'existe plus, ou n'est pas "
                  "dans le projet de la cible"),
    DeclaredError(400, "not_utf8", "`doc` ou `datastore` : le corps n'est pas de l'UTF-8"),
    DeclaredError(400, "bad_ndjson",
                  "`datastore` NDJSON : une ligne n'est pas un objet JSON — `detail` "
                  "dit laquelle"),
    DeclaredError(400, "empty_dataset",
                  "`datastore` : aucune ligne (un CSV exige son en-tête)"),
    DeclaredError(400, "entete_en_collision",
                  "`datastore` CSV : un en-tête traduit tombe sur une colonne déjà "
                  "déclarée"),
    DeclaredError(400, "bad_row",
                  "`datastore` : une ligne est refusée par le stockage — `detail` dit "
                  "laquelle et pourquoi"),
    DeclaredError(409, "business_key_exists",
                  "`datastore`, jeton frappé sans `upsert` (oto#141) : des lignes du fichier partagent une valeur de clé, ou — "
                  "frappé sans `key`, le fichier AJOUTE — en portent une que le "
                  "tableau a déjà. `detail` les nomme, rien n'est écrit ; frapper un "
                  "jeton avec `key` (désigner) ou `upsert=true` (fusionner)"),
    DeclaredError(400, "unknown_column",
                  "`datastore` (oto#124) : le fichier pose une "
                  "colonne que le schéma du tableau ne déclare pas. `detail` la nomme, "
                  "rien n'est écrit ; la déclarer (`PATCH …/schema`) puis renvoyer"),
    DeclaredError(413, "image_too_large", "`image` : au-delà de 2 Mo"),
    DeclaredError(400, "unsupported_type",
                  "`image` : ni png, ni jpeg, ni gif, ni webp (jugé sur les octets)"),
)

_FICHIER_BRUT = {"type": "string", "format": "binary"}

upload_receive.contrat = ContratDeRoute(
    description=(
        "Dépose le contenu d'un lien signé frappé par `POST /api/me/upload-url` (ou "
        "l'outil agent `oto_upload_url`). Aucun en-tête `Authorization` : le jeton de "
        "l'adresse fait foi — il scelle le compte, l'org et la cible, vit 15 minutes et "
        "ne sert qu'une fois. `PUT` : le corps BRUT, sous le `Content-Type` rendu à la "
        "frappe — pour un tableau, NDJSON (`application/x-ndjson`, un objet JSON par "
        "ligne) ou CSV (`text/csv`, en-tête requis), upserté en lot sur la clé choisie à "
        "la frappe (sinon la clé métier du tableau) ; pour une page, du Markdown ; pour "
        "un fichier de projet ou une image, les octets. `POST` : le même dépôt en "
        "multipart, une seule partie `file` (le formulaire de la page `GET`). La cible "
        "est RE-jugée à la réception, et la réponse est un accusé léger, jamais le "
        "corps."),
    Output=AccuseDeDepot,
    errors=_REFUS_DE_LA_RECEPTION,
    corps={
        "PUT": {"application/x-ndjson": {"type": "string"},
                "text/csv": {"type": "string"},
                "text/markdown": {"type": "string"},
                "application/octet-stream": _FICHIER_BRUT},
        "POST": {"multipart/form-data": {
            "type": "object", "required": ["file"],
            "properties": {"file": _FICHIER_BRUT}}},
    },
    authentifiee=False,
)

upload_form.contrat = ContratDeRoute(
    description=(
        "La page HTML de dépôt d'un lien signé — la voie d'un humain, quand l'agent n'a "
        "pas de shell. Aucun en-tête `Authorization`, et le jeton n'est PAS consommé : "
        "il ne l'est qu'au dépôt (`POST` du formulaire). Lien invalide ou expiré : la "
        "même page, en 401, sans formulaire."),
    media="text/html",
    authentifiee=False,
)
