"""oto_guide — guides d'usage d'oto, chargés à la demande (ADR 0041/0042).

Surface unique des guides on-demand, à trois scopes : **platform** (fichiers
`oto_mcp/guides/*.md`, versionnés en PR), **org** et **user** (DB, écrits par l'org /
l'utilisateur). `op=list` = catalogue visible (platform ∪ org active ∪ user) ;
`op=read(slug[,scope])` = le corps ; `op=write`/`delete(slug, scope=org|user, body_md…)`
= éditer un guide d'org (admin d'org) ou perso (self). Distinct des PROCÉDURES
(`oto_get_doctrine`, avec slots) — un guide est de la PROSE (ADR 0042).

Spine : chargé explicitement dans `register_all`, hors gate, toujours visible
(`PROTECTED_TOOLS`). La description embarque l'index des guides PLATEFORME (statique) ;
les guides d'org/user se découvrent via `op=list` (scopé au caller).
"""
from __future__ import annotations

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import guide_store
from ..auth_hooks import current_user_sub_from_token

_BASE_DESC = (
    "Load or author an oto usage guide (a how-to, PROSE — not a procedure) on demand. "
    "op=list → the catalog you can see (platform ∪ your org ∪ your own) [{slug, scope, "
    "title, description}] ; op=read (slug, optional scope) → its markdown body ; "
    "op=write / delete (slug, scope=org|user, body_md, title?, description?) → author a "
    "guide for your ORG (org admin) or YOURSELF (scope=user). Platform guides are files "
    "(edited via PR). Read the relevant guide BEFORE a non-trivial task (e.g. bulk-load).")


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def register(mcp: FastMCP) -> None:
    index = guide_store.guides_index_md()
    description = f"{_BASE_DESC}\n\n{index}" if index else _BASE_DESC

    @mcp.tool(description=description)
    def oto_guide(op: str = "list", slug: str | None = None, scope: str | None = None,
                  body_md: str | None = None, title: str | None = None,
                  description: str | None = None) -> dict:
        from .. import access, roles
        sub = current_user_sub_from_token()
        org_id = access.current_org(sub) if sub else None

        if op == "list":
            return {"guides": guide_store.list_guides_for(sub, org_id)}

        if op == "read":
            if not slug:
                raise _bad("`slug` requis pour op=read (cf. op=list).")
            guide = guide_store.read_guide_scoped(slug, scope=scope, org_id=org_id, sub=sub)
            if guide is None:
                raise _bad(f"guide `{slug}` inconnu — liste les guides avec op=list.")
            return guide

        if op in ("write", "delete"):
            if not sub:
                raise _bad("authentification requise pour éditer un guide.")
            if not slug:
                raise _bad("`slug` requis.")
            sc = scope or "user"
            if sc == "user":
                owner_id = sub
            elif sc == "org":
                if org_id is None:
                    raise _bad("aucune org active pour un guide de scope org.")
                if not roles.is_org_admin(sub, org_id):
                    raise _bad("réservé à un admin de l'org (guide de scope org).")
                owner_id = str(org_id)
            else:
                raise _bad("scope éditable = org | user (platform = fichiers, édités en PR).")

            if op == "delete":
                deleted = guide_store.delete_guide(sc, owner_id, slug)
                return {"slug": slug, "scope": sc, "deleted": deleted}

            if not (body_md or "").strip():
                raise _bad("`body_md` requis pour op=write.")
            if len(body_md.encode()) > 64 * 1024:
                raise _bad("`body_md` > 64 KB.")
            try:
                return guide_store.set_guide(sc, owner_id, slug, body_md,
                                             title or "", description or "")
            except guide_store.GuideError as e:
                raise _bad(str(e))

        raise _bad(f"op `{op}` inconnu (attendu: list | read | write | delete).")
