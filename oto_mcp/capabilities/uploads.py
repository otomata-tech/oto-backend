"""Upload out-of-bande de contenu volumineux (issue oto-backend#105).

`oto_upload_url(target=…)` rend une URL signée à usage unique + TTL court sur laquelle
l'agent PUT le contenu depuis le disque (`curl --data-binary @fichier`), au lieu de le
faire transiter INLINE par le contexte du LLM (coût tokens + troncature sur du verbatim).
Le backend matérialise dans la cible (`PUT /api/upload/<token>`, `api_routes`) en
réappliquant l'autz. MCP-only : c'est une amorce d'action agent, pas une surface
dashboard (le dashboard a déjà l'upload multipart humain).
"""
from __future__ import annotations

import os
from typing import Literal, Optional

from pydantic import BaseModel

from .. import upload_tokens
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx
from .registry import CAPABILITIES


class UploadUrlInput(BaseModel):
    target: Literal["doc", "project_file"]
    op: Literal["create", "update"] = "create"        # doc : create sous projet, ou update d'un doc
    project_id: Optional[int] = None                  # doc create / project_file
    parent_id: Optional[int] = None                   # doc create (None = 1er niveau)
    doc_id: Optional[int] = None                      # doc update
    title: Optional[str] = None                       # doc create (requis) / project_file (optionnel)
    kind: Optional[Literal["doc", "note", "source"]] = None  # doc create (défaut source)
    filename: Optional[str] = None                    # project_file (requis)
    description: Optional[str] = None                 # project_file (optionnel)
    content_type: Optional[str] = None                # project_file (sinon déduit à la réception)


def _upload_url(ctx: ResolvedCtx, inp: UploadUrlInput) -> dict:
    sub = ctx.sub
    # Descripteur de cible SCELLÉ dans le jeton — jamais accepté d'un param client à la
    # réception (verrou IDOR : sub/org/cible figés au mint).
    if inp.target == "doc":
        if inp.op == "update":
            if inp.doc_id is None:
                raise AuthzDenied(400, "missing_doc", "`doc_id` requis (op=update).")
            target = {"kind": "doc", "op": "update", "doc_id": int(inp.doc_id)}
        else:
            if inp.project_id is None:
                raise AuthzDenied(400, "missing_project", "`project_id` requis (op=create).")
            if not (inp.title and inp.title.strip()):
                raise AuthzDenied(400, "missing_title", "`title` requis (op=create).")
            target = {"kind": "doc", "op": "create", "project_id": int(inp.project_id),
                      "parent_id": int(inp.parent_id) if inp.parent_id is not None else None,
                      "title": inp.title.strip(), "doc_kind": inp.kind or "source"}
    else:  # project_file
        if inp.project_id is None:
            raise AuthzDenied(400, "missing_project", "`project_id` requis.")
        if not (inp.filename and inp.filename.strip()):
            raise AuthzDenied(400, "missing_filename", "`filename` requis.")
        target = {"kind": "project_file", "project_id": int(inp.project_id),
                  "filename": inp.filename.strip(),
                  "title": (inp.title.strip() if inp.title else None),
                  "description": (inp.description.strip() if inp.description else None),
                  "content_type": inp.content_type}

    # Fail-fast : refuse tout de suite sans l'écriture sur la cible (l'autz est
    # RÉAPPLIQUÉE à la réception — le jeton ne fait pas foi seul).
    try:
        upload_tokens.check_target_access(sub, target)
    except upload_tokens.UploadError as e:
        raise AuthzDenied(e.status, e.code, e.message)

    token, exp = upload_tokens.sign(sub, ctx.org_id, target)
    base = os.environ.get("OTO_MCP_PUBLIC_URL", "https://mcp.oto.ninja").rstrip("/")
    url = f"{base}/api/upload/{token}"
    ct = (target.get("content_type")
          or ("text/markdown; charset=utf-8" if target["kind"] == "doc"
              else "application/octet-stream"))
    return {
        "url": url,
        "method": "PUT",
        "expires_at": exp,
        "max_bytes": upload_tokens.max_bytes(),
        "headers": {"Content-Type": ct},
        "hint": (f"Push the raw content out-of-band: `curl -X PUT -H 'Content-Type: {ct}' "
                 f"--data-binary @FILE '{url}'`. Single-use, expires soon; the body never "
                 "returns through you (only a light receipt: id + length)."),
        "target": target,
    }


CAPABILITIES += [
    Capability(
        key="me.upload_url", handler=_upload_url, Input=UploadUrlInput, authz=SUB_ONLY,
        description=(
            "Get a SIGNED, single-use, short-TTL URL to PUSH large content OUT-OF-BAND "
            "into oto, instead of passing the body INLINE through your context. Use this "
            "whenever the content is big (meeting transcript, dataset, long doc, PDF/CSV) "
            "so it never round-trips through you (token cost + verbatim truncation). "
            "Returns {url, method:PUT, expires_at, max_bytes, headers}; then run e.g. "
            "`curl -X PUT --data-binary @FILE '<url>'` from your shell — the backend "
            "materializes it into the target and returns a light receipt (id + length), "
            "never the body. target='doc' writes a Documents page (op=create: project_id "
            "+ title [+ parent_id, kind]; op=update: doc_id) ; target='project_file' "
            "attaches a raw file (project_id + filename [+ title, description, "
            "content_type]) — fills the agent gap of depositing a PDF/CSV. Requires write "
            "access to the project."
        ),
        mcp="oto_upload_url",
    ),
]
