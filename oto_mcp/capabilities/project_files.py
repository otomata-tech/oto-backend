"""Documents bruts d'un projet exposés à l'AGENT (carte « Autre document », ADR 0032 §3).

MCP-only : la face REST (upload multipart + list + delete) vit dans `api_routes`
(corps binaire, hors couche capacité ADR 0009). Ici on donne à Claude la LECTURE
de ce qui est attaché au projet — titre/description/résumé + une URL de
téléchargement signée — et la SUPPRESSION (purger un doc périmé sans surface
humaine ; miroir du DELETE REST). L'upload reste dashboard-only (multipart).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from .. import db, media_store, ownership
from . import projects as _projects
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx
from .registry import CAPABILITIES


class ProjectFilesInput(BaseModel):
    op: Literal["list", "delete"]
    project_id: int
    file_id: Optional[int] = None               # requis pour op=delete


def _project_files(ctx: ResolvedCtx, inp: ProjectFilesInput) -> dict:
    row = db.get_project_by_id(inp.project_id)
    if row is None:
        raise AuthzDenied(404, "unknown_project", f"Projet #{inp.project_id} inconnu.")
    # Même gate de contexte d'org que `oto_project op=get` (ADR 0023) : les docs d'un
    # projet ne sont lisibles que DANS l'org du projet, pas depuis une autre de mes orgs.
    _projects._require_active_org_visible(ctx, row)

    if inp.op == "delete":
        if inp.file_id is None:
            raise AuthzDenied(400, "missing_file_id", "op=delete requiert file_id.")
        existing = db.get_project_file(inp.file_id)
        if not existing or existing["project_id"] != inp.project_id:
            raise AuthzDenied(404, "unknown_file", f"Fichier #{inp.file_id} inconnu sur ce projet.")
        if not ownership.can_access(ctx.sub, _projects.RTYPE, str(inp.project_id), "write"):
            raise AuthzDenied(403, "forbidden", "Écriture refusée sur ce projet.")
        db.delete_project_file(inp.file_id)
        media_store.delete_by_key(existing["s3_key"])
        db.log_project_activity(inp.project_id, ctx.sub, "project.file_delete",
                                existing.get("title") or existing.get("filename"))
        return {"ok": True}

    files = []
    for r in db.list_project_files(inp.project_id):
        key = r.pop("s3_key", None)
        try:
            r["download_url"] = media_store.presign_get(key) if key else None
        except media_store.MediaError:
            r["download_url"] = None
        files.append(r)
    return {"files": files}


CAPABILITIES += [
    Capability(
        key="me.project_files", handler=_project_files, Input=ProjectFilesInput,
        authz=SUB_ONLY,
        description=(
            "A project's raw documents (« Autre document » — PDF/HTML/etc. attached to "
            "the project). op=list → each file with filename + title/description/summary "
            "+ a signed download_url to fetch it (read access). op=delete + file_id → "
            "remove an outdated document (write access; deletes the stored object too). "
            "Upload is dashboard-only (multipart)."
        ),
        mcp="oto_project_files",
    ),
]
