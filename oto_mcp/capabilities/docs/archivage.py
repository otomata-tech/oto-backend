"""`archive` / `unarchive` / `verify` / `set_owner` — l'état d'une page, posé par un geste
(#1087 ; le responsable : `db/responsables`).

Une page périmée remontait dans `oto_search` à égalité avec une page fraîche, et la seule
sortie était `op=delete`, irréversible. Deux gestes, chacun explicite :

- **archiver** retire la page — et son sous-arbre vivant — de la DÉCOUVERTE : `oto_search`
  et la liste par défaut (`op=list include_archived=true` la remontre). Rien n'est
  détruit, la page se lit encore par son id, et le geste se défait (`unarchive`) ;
- **vérifier** date la dernière relecture (`verified_at`, `verified_by`), sans toucher
  au corps, à `updated_at` ni aux versions : une coquille corrigée n'est pas une
  revalidation.

Droit : celui des autres gestes de page, `write` sur le projet (`common.can`) — archiver
se défait, ce n'est pas la suppression.
"""
from __future__ import annotations

from typing import Optional

from ... import db
from ...db import responsables
from . import common
from .common import require

# Le refus d'écrire SOUS une page archivée : une sous-page vivante y serait listée sous
# un parent que la liste ne montre pas, et cherchable alors que son parent ne l'est
# plus. Une seule phrase pour les trois portes (create, bulk_create, move).
PARENT_ARCHIVE = "parent_archived"


def refuser_parent_archive(parent: Optional[dict]) -> None:
    """Refuse (409 `parent_archived`) de ranger une page vivante sous une archivée."""
    if parent is not None and parent.get("archived_at"):
        require(False, PARENT_ARCHIVE,
                f"Page #{parent['id']} is archived: a live page cannot be placed under "
                f"it. Unarchive it first (op=unarchive doc_id={parent['id']}), or pick "
                "another parent.", 409)


def archive(sub: Optional[str], inp, row: dict, pid: int) -> dict:
    require(common.can(sub, pid, "write"), "forbidden", "Write refused.", 403)
    n, quand = db.archive_doc(int(inp.doc_id))
    if not n:
        # Idempotent : la date reste celle du premier retrait.
        return {"ok": True, "id": inp.doc_id, "archived": False,
                "archived_at": row.get("archived_at"),
                "hint": "Already archived — nothing changed."}
    db.log_project_activity(pid, sub, "doc.archive",
                            f"{row.get('title')} (+{n - 1} sous-page(s))" if n > 1
                            else row.get("title"))
    out = {"ok": True, "id": inp.doc_id, "archived": True, "archived_at": quand,
           "descendants": n - 1,
           "hint": ("Archived: out of oto_search and of op=list (include_archived=true "
                    "lists it), still readable with op=get. op=unarchive undoes it.")}
    return out


def unarchive(sub: Optional[str], inp, row: dict, pid: int) -> dict:
    require(common.can(sub, pid, "write"), "forbidden", "Write refused.", 403)
    if row.get("archived_at") and row.get("parent_id") is not None:
        # Rendre une page à la découverte sous un parent qui n'y est pas : le même
        # défaut que créer sous une archivée, par l'autre bout.
        refuser_parent_archive(db.get_doc_by_id(int(row["parent_id"])))
    n = db.unarchive_doc(int(inp.doc_id))
    if n:
        db.log_project_activity(pid, sub, "doc.unarchive", row.get("title"))
    return {"ok": True, "id": inp.doc_id, "unarchived": bool(n),
            "was_archived_at": row.get("archived_at"), "descendants": max(0, n - 1)}


def set_owner(sub: Optional[str], inp, row: dict, pid: int) -> dict:
    """Confie la page à `owner_sub` — la personne à qui s'adresser (`db/responsables`).

    Même droit que les autres gestes de page (`write` sur le projet). Le responsable
    doit pouvoir LIRE la page par son projet : nommer quelqu'un qui ne la voit pas
    enverrait les questions à une personne qui ne peut pas y répondre."""
    require(common.can(sub, pid, "write"), "forbidden", "Write refused.", 403)
    cible = (inp.owner_sub or "").strip()
    require(cible, "missing_owner", "`owner_sub` is required: the account to name.")
    require(common.can(cible, pid, "read"), "owner_cannot_read",
            "That account cannot read this page's project: name someone who can.", 422)
    fait = responsables.poser_responsable_de_page(int(inp.doc_id), cible)
    require(fait, "unknown_doc", f"Unknown doc #{inp.doc_id}.", 404)
    db.log_project_activity(pid, sub, "doc.set_owner", row.get("title"))
    return {"ok": True, "id": inp.doc_id, "owner_sub": cible,
            "previous_owner_sub": row.get("owner_sub") or row.get("created_by")}


def verify(sub: Optional[str], inp, row: dict, pid: int) -> dict:
    require(common.can(sub, pid, "write"), "forbidden", "Write refused.", 403)
    fait = db.verify_doc(int(inp.doc_id), sub)
    require(fait is not None, "unknown_doc", f"Unknown doc #{inp.doc_id}.", 404)
    db.log_project_activity(pid, sub, "doc.verify", row.get("title"))
    return {"ok": True, "id": inp.doc_id, **fait,
            "previous_verified_at": row.get("verified_at")}
