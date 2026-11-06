"""Partager UNE page, sans son projet (signal #1084) — ce que le domaine « doc » apporte
au « Partager » unifié (ADR 0048) et ce qu'il rend à celui qui reçoit.

Le besoin : faire lire une page récapitulative à deux personnes d'une AUTRE org. Le seul
geste disponible partageait tout le projet — elles voyaient ses vingt pages, internes
comprises. `set_public` n'est pas la réponse : il ouvre la page à quiconque a le lien.

**La surface est celle des autres ressources**, pas un outil de plus (ADR 0047) :
`oto_resource op=share resource_type="doc" resource_id=<page>` avec une audience
`person`/`team`/`org`, `op=unshare` pour retirer. Le grant vit dans `resource_grants`
(TEXT libre : aucun DDL) sous le kind `doc` déclaré dans `common`.

**Ce que le partage ouvre, et rien d'autre** — la page SEULE, en lecture :

- `oto_doc op=get` sur CETTE page (`common.acces_a_la_page`, la seule porte) ;
- `oto_doc op=shared_with_me`, pour la retrouver ;
- et c'est tout. `list`, `search`, `revisions`, `backlinks` restent gardées par le
  PROJET : ni les pages sœurs, ni les sous-pages (qui n'héritent de rien), ni
  l'historique (une version antérieure peut porter ce que l'auteur a retiré avant de
  partager), ni ce qui la cite (d'autres pages, par leur titre). Les tableaux intégrés
  (`oto-data`) gardent leur propre droit : partager la page n'en donne aucun.

**Rôle `viewer` seulement.** Éditer une page hors de son projet ouvrirait une écriture
que rien d'autre dans le domaine ne sait borner à une ligne ; qui doit écrire reçoit le
projet. **Qui partage** : qui GOUVERNE le projet (`ownership.can_govern`, via
`governed_by`) — jamais un simple lecteur de la page.
"""
from __future__ import annotations

from typing import Callable, Optional

from ... import db, ownership
from ...db import shell as db_shell
from .._types import AuthzDenied
from . import common, view

# La phrase SERVIE par les deux surfaces de gouvernance (`oto_resource`, `oto_resource_v2`)
# — écrite une fois : deux copies divergeraient au premier correctif.
DESCRIPTION = (
    "resource_type=\"doc\" shares ONE PAGE ALONE (resource_id = its oto_doc id), "
    "read-only: role `viewer` (the default — editor/manager are refused with "
    "`doc_viewer_only`), audience person/team/org only (public/secret do not apply to a "
    "page). The recipient reads it with oto_doc op=get and finds it with oto_doc "
    "op=shared_with_me — never its project, sibling pages, sub-pages, revisions or "
    "backlinks. Sharing a page takes the right to govern its project; op=unshare closes "
    "it at once; op=list lists the pages you shared one by one; a page is never "
    "transferred on its own.")

# `principal_type` du grant → le mot d'AUDIENCE que le reste de la surface emploie.
_AUDIENCE = {"user": "person", "org": "org", "group": "team"}


def require_viewer(role: str) -> None:
    """Une page se partage en lecture. Le refus dit quoi faire à la place."""
    if role != "viewer":
        raise AuthzDenied(
            400, "doc_viewer_only",
            "A page is shared read-only: pass role=\"viewer\" (or omit it). To let "
            "someone edit it, share its project instead (resource_type=\"project\").")


def enrich(row: dict, owner_label: Callable[[str, str], Optional[str]]) -> dict:
    """La fiche de gouvernance d'une page — son identité et le propriétaire de son
    PROJET (une page n'en a pas d'autre). Jamais le corps : ce plan gouverne sans lire."""
    if "owner_type" in row:
        otype, oid = row.get("owner_type"), row.get("owner_id")
    else:
        otype, oid = ownership.owner_of(common.DOC_RTYPE, str(row["id"])) or (None, None)
    return {
        "resource_type": common.DOC_RTYPE,
        "resource_id": str(row["id"]),
        "title": row.get("title"),
        "project_id": row.get("project_id"),
        "owner_type": otype,
        "owner_id": oid,
        "owner_label": owner_label(otype or "", oid or ""),
        "updated_at": row.get("updated_at"),
    }


# Le dispatch de `resources._OPS` pour la famille `doc` (sauf `enrich`, qui prend le
# libellé de propriétaire de `resources` — l'importer d'ici ferait un cycle).
OPS = {
    "list_all": lambda: db.list_shared_docs(None),
    "list_for_owners": lambda owners: db.list_shared_docs(owners),
    "get_by_id": lambda i: db.get_doc_by_id(i),
}


def portee(rid: str) -> dict:
    """De quoi nommer l'élargissement dans `portee_elargissements` (ADR 0068 §4) : le
    titre, et l'AUTEUR de la page — la personne dont le contenu vient de s'ouvrir, qui
    n'est pas toujours celle qui partage (un gérant du projet partage la page d'un autre)."""
    row = db.get_doc_by_id(int(rid)) if str(rid).isdigit() else None
    if not row:
        return {}
    return {"ressource_nom": row.get("title"), "proprietaire_sub": row.get("created_by")}


# Les portées de `op=shared_with_me` (`scope`). Le défaut (None) est la vue historique
# — l'union de tout ce que l'appelant reçoit, toutes orgs confondues — et le reste :
# un contrat servi se DOUBLE, il ne se durcit pas en place.
PORTEES = ("me", "org")


def _principaux(sub: str, scope: Optional[str], org_id: Optional[int]) -> list[tuple[str, str]]:
    """Les principals dont on lit les partages reçus, selon la portée.

    - `None` : l'appelant, toutes ses orgs et toutes ses équipes (vue historique) ;
    - `"me"` : l'appelant SEUL — un partage à une personne n'appartient à aucune org ;
    - `"org"` : l'org CONSULTÉE et les équipes de l'appelant DANS cette org (même seam
      que les listes de projets et de tableaux) ; ses partages nominatifs sont rangés à
      part par `_pages_recues` (06/11/2026)."""
    if scope == "me":
        return [("user", sub)]
    if scope == "org":
        return ownership.proprietaires_collectifs_de_liste(sub, org_id)
    return ownership.accessor_scope(sub).principal_pairs()


def _pages_recues(sub: str, scope: Optional[str], org_id: Optional[int]) -> list[dict]:
    """Les pages reçues selon la portée. Pour `scope="org"`, une page partagée à moi en
    personne se range dans l'org de SON projet si j'en suis membre, sinon dans ma maison
    (`ownership.partages_a_moi_ici`, 06/11/2026) — comme les projets et les tableaux."""
    if scope != "org":
        return db.list_docs_granted_to(_principaux(sub, scope, org_id))
    collectives = db.list_docs_granted_to(
        ownership.proprietaires_collectifs_de_liste(sub, org_id))
    a_moi = db.list_docs_granted_to([("user", sub)])
    projets: dict[int, Optional[dict]] = {}
    for r in a_moi:
        pid = int(r["project_id"])
        if pid not in projets:
            projets[pid] = db.get_project_by_id(pid)

    def _rang(r: dict) -> Optional[int]:
        p = projets.get(int(r["project_id"]))
        return ownership.org_de_rangement(p) if p else None

    # Une ligne par page, représentée par son grant le plus ancien (même règle que
    # `list_docs_granted_to`).
    vus, out = set(), []
    # À date égale, le partage fait à la personne d'abord (tri stable).
    for r in sorted(ownership.partages_a_moi_ici(sub, org_id, a_moi, rang=_rang) + collectives,
                    key=lambda r: str(r.get("granted_at") or "")):
        if r["id"] not in vus:
            vus.add(r["id"])
            out.append(r)
    return sorted(out, key=lambda r: r["updated_at"], reverse=True)


def recus(sub: str, scope: Optional[str] = None, org_id: Optional[int] = None) -> dict:
    """`oto_doc op=shared_with_me` : les pages partagées à l'appelant. Sans `scope`, à
    lui, à l'une de ses orgs ou de ses équipes — toutes orgs confondues (un partage reçu
    ne disparaît pas quand on change d'org active) ; `scope="me"` à lui seul ;
    `scope="org"` à l'org consultée (`org_id`) et à ses équipes dans cette org, plus à
    lui-même rangé dans l'org de son projet (06/11/2026). La
    réponse nomme la portée appliquée (`scope`, `null` = l'union historique).

    Une entrée NOMME la page, elle ne la livre pas : le corps se lit par `op=get`.
    `url` est None quand l'appelant n'a que la page — l'adresse servie ouvre la page
    dans son projet, qu'il ne lit pas (cf. `reads.get`)."""
    # Vue bornée (oto#270) : seules les pages visibles dans O, quelle que soit la portée.
    rows = ownership.borner_a_la_vue(sub, common.DOC_RTYPE,
                                     _pages_recues(sub, scope, org_id),
                                     rid=lambda r: r["id"])
    noms = db_shell.names_of(r.get("granted_by") for r in rows)
    docs = [{
        "id": r["id"],
        "title": r.get("title"),
        "updated_at": r.get("updated_at"),
        "role": r.get("role"),
        "via": _AUDIENCE.get(r.get("principal_type") or "", r.get("principal_type")),
        "shared_by": noms.get(r.get("granted_by")),
        "url": (view.doc_url(sub, r)
                if common.acces_a_la_page(sub, r) == common.PAR_LE_PROJET else None),
    } for r in rows]
    return {"docs": docs, "count": len(docs), "scope": scope}
