"""Documents bruts d'un projet exposés à l'AGENT (carte « Autre document », ADR 0032 §3).

MCP-only : la face REST (upload multipart + list + delete) vit dans `api_routes`
(corps binaire, hors couche capacité ADR 0009). Ici on donne juste à Claude la
LECTURE de ce qui est attaché au projet — titre/description/résumé + une URL de
téléchargement signée — pour qu'il puisse consommer les PDF/HTML du projet sans
passer par une surface humaine. Pas de duplication d'écriture : lecture seule.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from .. import db, media_store
from . import projects as _projects
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx
from .registry import CAPABILITIES


class ProjectFilesInput(BaseModel):
    op: Literal["list"]
    project_id: int


def _project_files(ctx: ResolvedCtx, inp: ProjectFilesInput) -> dict:
    row = db.get_project_by_id(inp.project_id)
    if row is None:
        raise AuthzDenied(404, "unknown_project", f"Projet #{inp.project_id} inconnu.")
    # Même gate de contexte d'org que `oto_project op=get` (ADR 0023) : les docs d'un
    # projet ne sont lisibles que DANS l'org du projet, pas depuis une autre de mes orgs.
    _projects._require_active_org_visible(ctx, row)
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
            "List a project's raw documents (« Autre document » — PDF/HTML/etc. attached "
            "to the project), each with filename + title/description/summary + a signed "
            "download_url to fetch it. Requires read access to the project. Upload & delete "
            "are dashboard-only (multipart)."
        ),
        mcp="oto_project_files",
    ),
]
