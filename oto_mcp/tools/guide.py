"""oto_guide — guides d'usage d'oto, chargés à la demande (oto-backend#111).

`oto_guide(op=list)` = le catalogue (slug/titre/description) ; `oto_guide(op=read,
slug)` = le corps markdown d'un guide. Contenu = fichiers `oto_mcp/guides/*.md`
(plateforme, versionnés, revus en PR) — cf. `guide_store`. Pendant des « claude docs ».

Spine : chargé explicitement dans `register_all`, hors gate d'activation, toujours
visible (`PROTECTED_TOOLS`). Lecture seule, pas de dépendance externe. La description
de l'outil embarque l'INDEX des guides (comme `skills_index_md` pour les doctrines) →
l'agent les découvre au `tools/list`, sans coût de prompt permanent.
"""
from __future__ import annotations

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import guide_store

_BASE_DESC = (
    "Load an oto usage guide (platform how-to) on demand. op=list → the catalog "
    "[{slug, title, description}] ; op=read (slug) → the guide's markdown body. Read "
    "the relevant guide BEFORE a non-trivial task (e.g. bulk-load).")


def register(mcp: FastMCP) -> None:
    index = guide_store.guides_index_md()
    description = f"{_BASE_DESC}\n\n{index}" if index else _BASE_DESC

    @mcp.tool(description=description)
    def oto_guide(op: str = "list", slug: str | None = None) -> dict:
        if op == "list":
            return {"guides": guide_store.list_guides()}
        if op == "read":
            if not slug:
                raise McpError(ErrorData(code=INVALID_PARAMS,
                                         message="`slug` requis pour op=read (cf. op=list)."))
            guide = guide_store.read_guide(slug)
            if guide is None:
                raise McpError(ErrorData(
                    code=INVALID_PARAMS,
                    message=f"guide `{slug}` inconnu — liste les guides avec op=list."))
            return guide
        raise McpError(ErrorData(code=INVALID_PARAMS,
                                 message=f"op `{op}` inconnu (attendu: list | read)."))
