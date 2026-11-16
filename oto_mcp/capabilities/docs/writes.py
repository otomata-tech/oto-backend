"""Ce qui ÉCRIT : la page elle-même et sa place dans l'arbre — `create`, `bulk_create`,
`update`, `move`, `delete` — plus `set_public`, qui écrit son exposition.

Toute écriture de corps passe par `db.update_doc` : c'est lui qui prend le snapshot de
révision, re-résout les backlinks, propage un renommage et détecte le conflit optimiste.
`update` et `patch` s'y déclarent `regroupable` : une rafale d'enregistrements du
dashboard ne fait qu'une version (oto#274) ; `revert` ne l'est pas.
Un UPDATE de son cru perdrait les quatre — la règle vaut aussi pour `patch` et `revert`,
qui vivent à côté.
"""
from __future__ import annotations

from typing import Optional

from ... import db
from .. import _publication
from . import archivage, common, view
from .common import require


def create(sub: Optional[str], inp) -> dict:
    require(inp.project_id is not None, "missing_project", common.PROJET_REQUIS)
    require(inp.title and inp.title.strip(), "missing_title", "`title` is required.")
    require(common.can(sub, inp.project_id, "write"), "forbidden", "Write refused.", 403)
    if inp.parent_id is not None:
        parent = db.get_doc_by_id(int(inp.parent_id))
        require(parent and parent["project_id"] == inp.project_id, "bad_parent",
                "Invalid parent (another project, or nonexistent).")
        archivage.refuser_parent_archive(parent)
    trace: dict = {}
    did = db.create_doc(int(inp.project_id), inp.title.strip(), parent_id=inp.parent_id,
                        body_md=inp.body_md or "", kind=(inp.kind or "doc"), created_by=sub,
                        description=inp.description, trace=trace)
    db.log_project_activity(int(inp.project_id), sub, "doc.create", inp.title.strip())
    return {**view.projected(db.get_doc_by_id(did), sub, inp.fields,
                             brut_par_defaut=False, hint=view.HINT_ACCUSE), **trace}


#: Les clés qu'une page de `bulk_create` LIT. Fermée (#1354) : une clé hors liste n'était
#: lue nulle part et la page naissait autrement que demandé, sans un mot.
_CLES_DE_PAGE = ("title", "body_md", "kind", "description", "parent_index", "parent_id")


def _parent_du_projet(parent_id, project_id, cle: str) -> int:
    """Un parent posé dans ce projet, non archivé — refus nommé sinon."""
    par = db.get_doc_by_id(int(parent_id))
    require(par and par["project_id"] == project_id, "bad_parent",
            f"Invalid `{cle}` (another project, or nonexistent).")
    archivage.refuser_parent_archive(par)
    return int(parent_id)


def bulk_create(sub: Optional[str], inp) -> dict:
    # A4 (#6) : créer N pages en UN appel (33 pages ≠ 33 allers-retours). Arbre en un
    # coup via `parent_index` (index d'une page PLUS TÔT dans le lot) ; sinon le
    # `parent_id` de la page, sinon celui du lot.
    require(inp.project_id is not None, "missing_project", common.PROJET_REQUIS)
    require(common.can(sub, inp.project_id, "write"), "forbidden", "Write refused.", 403)
    require(bool(inp.pages), "missing_pages", "`pages` (non-empty list) is required.")
    if inp.parent_id is not None:
        _parent_du_projet(inp.parent_id, inp.project_id, "parent_id")
    # Tout le lot est jugé AVANT la première page écrite : un refus à la page 12 ne
    # laisse pas onze pages derrière lui.
    parents_de_page: list = []
    for i, p in enumerate(inp.pages):
        require(isinstance(p, dict), "bad_page", f"page #{i} is not an object.")
        title = str(p.get("title") or "").strip()
        require(bool(title), "missing_title", f"page #{i} has no `title`.")
        inconnues = sorted(k for k in p if k not in _CLES_DE_PAGE)
        require(not inconnues, "unknown_page_keys",
                f"page #{i}: {', '.join(f'`{k}`' for k in inconnues)} not read — a page "
                f"takes {', '.join(f'`{k}`' for k in _CLES_DE_PAGE)}. Nothing was created.")
        pi, ppid = p.get("parent_index"), p.get("parent_id")
        require(pi is None or ppid is None, "conflicting_parent",
                f"page #{i}: give `parent_index` (an earlier page of this batch) OR "
                f"`parent_id` (an existing page), not both. Nothing was created.")
        if pi is not None:
            require(isinstance(pi, int) and not isinstance(pi, bool) and 0 <= pi < i,
                    "bad_parent_index",
                    f"page #{i}: `parent_index` must be the index of an EARLIER page of "
                    f"this batch (0..{i - 1}), got {pi!r}. Nothing was created.")
        if ppid is not None:
            require(isinstance(ppid, int) and not isinstance(ppid, bool), "bad_parent",
                    f"page #{i}: `parent_id` must be a page id, got {ppid!r}.")
            _parent_du_projet(ppid, inp.project_id, f"pages[{i}].parent_id")
        parents_de_page.append((pi, ppid))
    created: list[int] = []
    for p, (pi, ppid) in zip(inp.pages, parents_de_page):
        parent = (created[pi] if pi is not None
                  else ppid if ppid is not None else inp.parent_id)
        created.append(db.create_doc(
            int(inp.project_id), str(p["title"]).strip(), parent_id=parent,
            body_md=p.get("body_md") or "", kind=(p.get("kind") or "doc"), created_by=sub,
            description=p.get("description")))
    db.log_project_activity(int(inp.project_id), sub, "doc.bulk_create", f"{len(created)} pages")
    return {"created": created, "count": len(created)}


def set_public(sub: Optional[str], inp, row: dict, pid: int) -> dict:
    # Partager publiquement (ou retirer) — action d'écriture (gap #4a).
    require(common.can(sub, pid, "write"), "forbidden", "Write refused.", 403)
    if inp.public:
        # Ouvrir au web exige d'appartenir au propriétaire du projet (#1176) ; refermer
        # réduit la portée et reste ouvert à qui écrit.
        _publication.exiger_appartenance(sub, pid)
    token = db.set_doc_public(int(inp.doc_id), bool(inp.public))
    db.log_project_activity(pid, sub, "doc.set_public",
                            f"{row.get('title')}:{bool(inp.public)}")
    return {"ok": True, "id": inp.doc_id, "public": bool(token),
            "public_url": view.public_doc_url(token, sub) if token else None}


def update(sub: Optional[str], inp, row: dict, pid: int) -> dict:
    require(common.can(sub, pid, "write"), "forbidden", "Write refused.", 403)
    trace: dict = {}
    try:
        db.update_doc(int(inp.doc_id), title=(inp.title.strip() if inp.title else None),
                      body_md=inp.body_md, kind=inp.kind, edited_by=sub,
                      description=inp.description, expected_rev=inp.expected_rev,
                      face=common.face_de_l_appel(), regroupable=True, trace=trace)
    except db.DocConflict as e:
        # Écrasement concurrent évité : le doc a changé depuis la lecture du client.
        require(False, "conflict",
                f"The doc changed in the meantime (current rev {e.current_rev}). "
                f"Read it again (op=get) and redo your edit on the current version.", 409)
    db.log_project_activity(pid, sub, "doc.update", row.get("title"))
    return {**view.projected(db.get_doc_by_id(int(inp.doc_id)), sub, inp.fields,
                             brut_par_defaut=False, hint=view.HINT_ACCUSE), **trace}


def delete(sub: Optional[str], inp, row: dict, pid: int) -> dict:
    # La cascade sur le sous-arbre était correcte et MUETTE (#657) : la réponse ne
    # disait pas ce qu'elle emportait, donc aucun front ne pouvait annoncer « ceci
    # supprimera N pages » ni prévenir que c'était sans retour. On ne change pas ce
    # que le geste FAIT — on le DÉCLARE, et `dry_run` permet de le demander avant.
    require(common.can(sub, pid, "write"), "forbidden", "Write refused.", 403)
    if inp.dry_run:
        n = db.count_doc_descendants(int(inp.doc_id))
        return {"ok": True, "id": inp.doc_id, "deleted": False, "dry_run": True,
                "descendants": n,
                "hint": (
                    f"Nothing was deleted. An op=delete on “{row.get('title')}” "
                    f"would take this page and {n} descendant(s), with their "
                    f"version history. It CANNOT be undone: op=revert restores "
                    f"a version of a page that still exists, it does not bring back "
                    f"a deleted page.")}
    n = db.delete_doc(int(inp.doc_id))
    db.log_project_activity(
        pid, sub, "doc.delete",
        f"{row.get('title')} (+{n} sous-page(s))" if n else row.get("title"))
    out = {"ok": True, "id": inp.doc_id, "deleted": True, "descendants": n}
    if n:
        # Même posture que `removed_subsections` sur op=patch : ce qui est parti EN
        # PLUS de la cible se dit, sinon la perte ne se découvre qu'à l'usage.
        out["warning"] = (
            f"This deletion also took {n} sub-page(s) and their version "
            f"history — the cascade follows the whole subtree. It cannot be undone: "
            f"op=revert only restores a version of an existing page. Pass "
            f"`dry_run: true` to know the count BEFORE deleting.")
    return out


def move(sub: Optional[str], inp, row: dict, pid: int) -> dict:
    # move — nouveau parent dans le MÊME projet (cycle profond non gardé en v1) ET/OU
    # réordonnancement (Ship 2 : `position` = index cible, la fratrie est réindexée).
    require(common.can(sub, pid, "write"), "forbidden", "Write refused.", 403)

    # A4 (#6) : déplacement CROSS-PROJET — la page + son sous-arbre changent de projet.
    # Écriture requise sur la SOURCE (ci-dessus) ET la CIBLE ; le parent proposé (si
    # fourni) doit appartenir au projet cible.
    if inp.to_project is not None and int(inp.to_project) != pid:
        tgt = int(inp.to_project)
        # F-016 : le droit AVANT l'existence — `can` est faux sur un projet absent, donc
        # même 403 pour « n'existe pas » et « pas à toi » (sinon oracle sur les ids).
        require(common.can(sub, tgt, "write"), "forbidden",
                "Write refused on the target project.", 403)
        require(db.get_project_by_id(tgt) is not None, "unknown_project",
                f"Unknown target project #{tgt}.", 404)
        if inp.parent_id is not None:
            require(int(inp.parent_id) != int(inp.doc_id), "bad_parent",
                    "A doc cannot be its own parent.")
            parent = db.get_doc_by_id(int(inp.parent_id))
            require(parent and parent["project_id"] == tgt, "bad_parent",
                    "Invalid parent (must be a page of the target project).")
            archivage.refuser_parent_archive(parent)
        n = db.move_doc_to_project(int(inp.doc_id), tgt,
                                   inp.parent_id if "parent_id" in inp.model_fields_set else None,
                                   position=inp.position)
        db.log_project_activity(pid, sub, "doc.move_out", f"{row.get('title')} → projet {tgt}")
        db.log_project_activity(tgt, sub, "doc.move_in", row.get("title"))
        out = view.projected(db.get_doc_by_id(int(inp.doc_id)), sub, inp.fields,
                             brut_par_defaut=False, hint=view.HINT_ACCUSE)
        out["moved_count"] = n
        return out

    if inp.parent_id is not None:
        require(int(inp.parent_id) != int(inp.doc_id), "bad_parent",
                "A doc cannot be its own parent.")
        parent = db.get_doc_by_id(int(inp.parent_id))
        require(parent and parent["project_id"] == pid, "bad_parent",
                "Invalid parent (another project, or nonexistent).")
        archivage.refuser_parent_archive(parent)
    # Trois intentions distinguées par `model_fields_set` (JSON null ≠ absent) :
    # parent FOURNI (id ou null=racine) = reparenter là ; absent + `position` posé =
    # réordonner DANS la fratrie courante ; absent + rien = racine (historique).
    if "parent_id" in inp.model_fields_set:
        target_parent = inp.parent_id
    elif inp.position is not None:
        target_parent = row.get("parent_id")
    else:
        target_parent = None
    db.move_doc(int(inp.doc_id), target_parent, position=inp.position)
    # Un déplacement ne touche à aucun contenu : rejouer le corps est du pur poids.
    return view.projected(db.get_doc_by_id(int(inp.doc_id)), sub, inp.fields,
                          brut_par_defaut=False, hint=view.HINT_ACCUSE)
