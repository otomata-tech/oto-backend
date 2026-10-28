"""Le dépôt d'un fichier dans une app ouverte — `POST /api/me/apps/{app_id}/files`.

Route de NATURE (corps multipart, hors du moule capacité) qui ne fait qu'ADAPTER :
tout le jugement — l'app, son manifeste (`files: true`), le consentement du lecteur,
le type jugé sur les octets, la taille, le stockage — est dans
`capabilities/apps_appel.deposer`. Session interactive seule, comme l'appel d'une app :
un jeton `oto_` (délégation d'agent comprise) est refusé, un agent n'est jamais le
lecteur d'une app.
"""
from __future__ import annotations

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from .. import access, geste
from ..capabilities import apps_appel
from ..capabilities._types import AuthzDenied, ContratDeRoute, DeclaredError, ResolvedCtx
from .base import _authenticate, _json, _json_error


async def app_files_upload(request: Request, *, verifier: JWTVerifier) -> JSONResponse:
    sub, err = await _authenticate(request, verifier, allow_api_token=False)
    if err:
        return err
    try:
        app_id = int(request.path_params["app_id"])
    except (KeyError, ValueError):
        return _json_error(request, 404, "unknown_app")
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001 — un corps qui n'est pas du multipart lisible
        return _json_error(request, 400, "invalid_multipart")
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return _json_error(request, 400, "missing_file")
    data = await upload.read()
    if not data:
        return _json_error(request, 400, "missing_file")
    consent = form.get("consent")

    def _deposer():
        # Tout ce qui lit la base tourne ICI, hors de la boucle (le serveur est
        # mono-loop : `docs/event-loop-perf.md`) — l'org active et le rôle compris.
        ctx = ResolvedCtx(sub=sub, org_id=access.current_org(sub), role=access.get_user_role(sub))
        # Le GESTE de la requête, comme `_rest_adapter` le pose : le consentement brûlé
        # porte son `geste_id`, le journal des gestes d'app sait d'où vient le dépôt.
        with geste.portee(geste.CONSOLE, sub):
            return apps_appel.deposer(ctx, app_id, str(consent) if consent else None,
                                      getattr(upload, "filename", None) or "file",
                                      getattr(upload, "content_type", None), data)

    try:
        out = await run_in_threadpool(_deposer)
    except AuthzDenied as e:
        return _json_error(request, e.status, e.code, e.message)
    return _json(request, out)


app_files_upload.contrat = ContratDeRoute(
    description=(
        "Dépose un fichier dans une app ouverte, pour que ses agents le traitent "
        "(manifeste `files: true`). Corps multipart : `file` (le fichier ; son type est "
        "jugé sur les octets — PDF, Word, Excel, texte, CSV, JSON ; "
        "20 Mo au plus) et `consent`, le jeton que `POST /api/me/apps/{app_id}/consent` "
        "a rendu pour `action=upload_file` et `file={name, size, media_type}` — ce qui "
        "arrive doit y répondre. Seul le lecteur qui l'a déposé lit le fichier, et les "
        "runs qu'il lance (`start_agent` avec `files`). Session interactive seule."),
    Output=apps_appel.AppCallOut,
    errors=(
        DeclaredError(400, "invalid_multipart", "le corps n'est pas un multipart lisible"),
        DeclaredError(400, "missing_file", "aucune partie `file`, ou un fichier vide"),
        DeclaredError(403, "consent_required", "dépôt sans `consent`"),
        DeclaredError(403, "consent_invalid",
                      "jeton brûlé, expiré, d'un autre lecteur ou pour un autre fichier"),
        DeclaredError(403, "files_not_allowed", "l'app ne déclare pas `files: true`"),
        DeclaredError(404, "unknown_app", "app inconnue ou illisible pour l'appelant"),
        DeclaredError(413, "file_too_large", "plus de 20 Mo"),
        DeclaredError(413, "app_storage_full", "le lecteur garde déjà 1 Go de fichiers dans l'app"),
        DeclaredError(415, "file_type", "type refusé, ou octets qui ne sont pas ce qu'ils disent"),
    ),
    corps={"POST": {"multipart/form-data": {
        "type": "object", "required": ["file", "consent"],
        "properties": {"file": {"type": "string", "format": "binary"},
                       "consent": {"type": "string"}}}}},
)
