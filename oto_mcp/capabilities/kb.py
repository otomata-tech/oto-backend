"""Base de connaissance d'org = la zone « Documents » qui remplace Memento (réunion
30/06, fusion KB↔Document). Une SEULE base par org : un projet dédié « Base de
connaissance », possédé par l'org active, résolu (et créé paresseusement) ici. La
zone Documents du dashboard l'ouvre via le composant doc existant — on réutilise
tout le substrat docs (pages arborescentes, versions, partage public, demande de
modif) sans nouvelle table.

Isolé dans son fichier (pas de collision avec `projects.py`) ; n'utilise que des
fonctions db existantes (zéro schéma neuf). Le marqueur = le NOM réservé du projet."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from .. import db
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

KB_NAME = "Base de connaissance"
KB_BRIEF = ("La base de connaissance de l'organisation : pages de référence partagées "
            "(processus, contexte, conventions). Une seule par org.")


class KbInput(BaseModel):
    op: Literal["get"] = "get"


def _kb(ctx: ResolvedCtx, inp: KbInput) -> dict:
    org = ctx.org_id
    if org is None:
        raise AuthzDenied(400, "no_active_org", "Aucune org active.")
    owner = ("org", str(org))
    existing = db.list_projects_for_owners([owner])
    kb = next((p for p in existing if p.get("name") == KB_NAME), None)
    if kb is None:
        pid = db.create_project("org", str(org), KB_NAME, KB_BRIEF, created_by=ctx.sub)
        kb = db.get_project_by_id(pid)
        db.log_project_activity(pid, ctx.sub, "kb.create", KB_NAME)
    return {"project_id": kb["id"], "name": kb["name"], "brief_md": kb.get("brief_md", "")}


CAPABILITIES += [
    Capability(
        key="me.kb", handler=_kb, Input=KbInput, authz=SUB_ONLY,
        description=(
            "Resolve the active org's KNOWLEDGE BASE — a single dedicated project "
            "« Base de connaissance » (created on first use). Returns project_id : its "
            "pages are managed with oto_doc (tree, versions, public share, change "
            "requests). This is the org-wide Documents space (replaces Memento)."
        ),
        mcp="oto_kb",
        rest=RestBinding("POST", "/api/me/kb"),
    ),
]
