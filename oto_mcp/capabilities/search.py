"""Capacité `search` (lot 3, Ship 1) — le verbe « retrouver », sur deux faces.

MCP `oto_search` + REST `GET /api/me/search` : un seul chemin de code
(`oto_mcp/search.py`), le dashboard (popup ⌘K / page /search) consomme la face
REST. Erreurs (plan Ship 1 §3) : pas d'org active → 400 `no_active_org`
(invocation DÉLIBÉRÉE — ≠ l'inbox de Ship 3, qui rendra des listes vides) ;
`scope='project'` sans `project` → 400 ; projet hors contexte → refus neutre.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, field_validator

from .. import ownership, search as search_mod
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class SearchInput(BaseModel):
    q: str
    scope: Literal["org", "project"] = "org"
    project: Optional[int] = None
    kinds: Optional[list[str]] = None      # sous-ensemble de search_mod.KINDS
    limit: int = 20

    @field_validator("kinds", mode="before")
    @classmethod
    def _csv(cls, v):
        # Face REST (GET) : `?kinds=page,tableau` arrive en string CSV.
        if isinstance(v, str):
            v = [k.strip() for k in v.split(",") if k.strip()]
        return v or None

    @field_validator("kinds")
    @classmethod
    def _known(cls, v):
        if v:
            bad = set(v) - set(search_mod.KINDS)
            if bad:
                raise ValueError(f"kinds inconnus : {sorted(bad)} (valides : {list(search_mod.KINDS)})")
        return v

    @field_validator("limit")
    @classmethod
    def _cap(cls, v):
        return max(1, min(int(v), 50))


def _search(ctx: ResolvedCtx, inp: SearchInput) -> dict:
    if ctx.org_id is None:
        raise AuthzDenied(400, "no_active_org",
                          "Aucune org active — passe `org=<id>` ou `oto_use_org`.")
    q = (inp.q or "").strip()
    if len(q) < 2:
        raise AuthzDenied(400, "query_too_short", "Requête trop courte (≥ 2 caractères).")
    if inp.scope == "project":
        if inp.project is None:
            raise AuthzDenied(400, "project_required",
                              "`scope='project'` exige `project=<id>`.")
        # Refus NEUTRE (pas de distinction inexistant/inaccessible) — plan Ship 1 §3.
        if not ownership.visible_in_org(ctx.sub, ctx.org_id, "project", str(inp.project)):
            raise AuthzDenied(404, "unknown_project", f"Projet #{inp.project} inconnu.")

    # Source connecteurs : le catalogue VISIBLE (activation × RBAC), injecté ici —
    # le module search reste sous la couche capacité. Best-effort (source optionnelle).
    catalog: list[dict] = []
    try:
        from . import connectors_selection
        catalog = connectors_selection._visible_catalog(ctx)
    except Exception:  # noqa: BLE001
        pass

    return search_mod.search(
        ctx.sub, ctx.org_id, q, scope=inp.scope, project_id=inp.project,
        kinds=inp.kinds, limit=inp.limit, connectors_catalog=catalog)


CAPABILITIES += [
    Capability(
        key="me.search",
        handler=_search,
        Input=SearchInput,
        authz=SUB_ONLY,
        description=(
            "SEARCH across everything readable in the active org — one query, ranked "
            "together: project pages & briefs (passages with highlighted fragment), "
            "procedures, guides, and containers by name (tableaux, files, connectors). "
            "Lexical (French stemming, accent-insensitive); reformulate with the exact "
            "words if 0 hits. `scope='project'`+`project=<id>` narrows to one project. "
            "`kinds` filters (page|brief|procedure|guide|tableau|fichier|connecteur). "
            "SEARCH when you know what you're looking for; NAVIGATE (oto_project op=get "
            "include=['spine']) when the question is structural. Then open the hit: "
            "oto_doc op=get (page), data_rows (tableau), oto_procedure op=get."),
        mcp="oto_search",
        rest=RestBinding("GET", "/api/me/search"),
    ),
]
