"""Base de connaissance d'org = la zone « Documents » qui remplace Memento (réunion
30/06, fusion KB↔Document). Une SEULE base par org : un projet dédié « Base de
connaissance », possédé par l'org active, résolu (et créé paresseusement) ici. La
zone Documents du dashboard l'ouvre via le composant doc existant — on réutilise
tout le substrat docs (pages arborescentes, versions, partage public, demande de
modif) sans nouvelle table.

Isolé dans son fichier (pas de collision avec `projects.py`) ; n'utilise que des
fonctions db existantes (zéro schéma neuf).

**Ancré PAR ID depuis le lot 3 (chantier 0.3)** : `orgs.kb_project_id` est la source
de vérité — le nom n'est plus un marqueur (renommer la KB ne casse plus rien, deux
appels concurrents ne créent plus deux KB). Auto-réparation : une ancre pendouillante
(projet archivé ou transféré hors org) est levée puis re-posée sur un projet neuf ;
verrou = claim optimiste (`claim_kb_project`), le perdant archive son doublon."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from .. import db, org_store
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

KB_NAME = "Base de connaissance"
KB_BRIEF = ("La base de connaissance de l'organisation : pages de référence partagées "
            "(processus, contexte, conventions). Une seule par org.")


class KbInput(BaseModel):
    op: Literal["get"] = "get"


def _anchored_kb(org: int) -> "tuple[Optional[int], Optional[dict]]":
    """(ancre, projet) — projet None si l'ancre est absente OU pendouillante
    (projet disparu / archivé / plus org-owned de CETTE org, ex. transféré)."""
    pid = org_store.get_kb_project_id(org)
    if pid is None:
        return None, None
    p = db.get_project_by_id(pid)
    ok = (p is not None and p.get("archived_at") is None
          and p.get("owner_type") == "org" and str(p.get("owner_id")) == str(org))
    return pid, (p if ok else None)


def _kb(ctx: ResolvedCtx, inp: KbInput) -> dict:
    org = ctx.org_id
    if org is None:
        raise AuthzDenied(400, "no_active_org", "Aucune org active.")
    pid, kb = _anchored_kb(org)
    if kb is None:
        if pid is not None:
            # Ancre pendouillante — compare-and-clear (jamais écraser une réparation
            # concurrente déjà re-posée).
            org_store.clear_kb_project(org, pid)
        new_pid = db.create_project("org", str(org), KB_NAME, KB_BRIEF, created_by=ctx.sub)
        if org_store.claim_kb_project(org, new_pid):
            db.log_project_activity(new_pid, ctx.sub, "kb.create", KB_NAME)
        else:
            # Un appel concurrent a gagné le claim — son projet est LA KB, le
            # doublon fraîchement créé est archivé (pas de delete dur des projets).
            db.archive_project(new_pid)
        _, kb = _anchored_kb(org)
        if kb is None:
            raise AuthzDenied(409, "kb_unavailable",
                              "La base de connaissance n'a pas pu être résolue — réessaie.")
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
