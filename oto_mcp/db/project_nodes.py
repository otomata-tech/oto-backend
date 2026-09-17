"""Les projets et leurs pages, LUS dans leurs tables et servis en forme de nœuds.

Pourquoi ce module existe. Le rail (`/api/me/shell`) et la fiche (`/api/me/nodes/{id}`)
ne lisaient que `nodes`. Tant que le démarrage recopiait `projects` et `docs` dans `nodes`,
un front branché sur ces deux surfaces voyait le contenu des orgs. La recopie s'est
arrêtée le 01/09/2026 (« les deux univers vivent côte à côte ») : depuis, ce front ne voit
que ce qu'on a créé en natif, et aucun projet — alors que le dashboard et le MCP écrivent
toujours dans `projects` et `docs`.

Ce module est un PROXY, pas une recopie : il lit les tables sources à chaque appel et ne
dépose rien. Une page modifiée par `oto_doc` se relit donc à jour, sans pont à tenir.

⚠️ **L'identifiant est RÉVERSIBLE, et c'est voulu** : `nod_prj_<id>` / `nod_doc_<id>`. La
fiche doit retrouver sa source depuis l'identifiant seul ; un md5 (la forme des anciennes
copies) obligerait à recalculer une empreinte sur chaque page de la base à chaque
ouverture. L'accès ne repose pas sur l'opacité : il se juge sur le projet
(`ownership.can_access`), et un refus rend le même 404 qu'un identifiant inconnu.

⚠️ **Le rail ne range plus les anciennes copies** (`est_une_copie`). Là où le résidu n'a
pas été retiré, il afficherait un contenu figé au 01/09 à côté de la vraie page. La fiche,
elle, ouvre encore une copie par son identifiant : c'est le contrat R5 de `edit_surface`
(`tests/api/test_node_page_native.py`), et le résidu se retire par sa commande de
maintenance, pas en le rendant illisible.

Ce qui n'est PAS couvert ici : un projet partagé EN DIRECT à une personne hors de son
propriétaire n'entre pas dans la section « Partagé » du rail (elle résout des nœuds), il
s'ouvre seulement par son identifiant. Les procédures ne passent pas par ce module : le
front les lit sur leur propre surface (`/api/me/instructions`).
"""
from __future__ import annotations

from typing import Iterable, Optional

from ._conn import _connect
from .nodes import ParentCycle

FAMILLE_PROJET = "prj"
FAMILLE_PAGE = "doc"
_FAMILLES = (FAMILLE_PROJET, FAMILLE_PAGE)
_PREFIXE = "nod_"
_KIND = "page"

# Même borne que le fil des nœuds (`capabilities/node_view._PROFONDEUR_FIL`) : c'est un
# budget d'affichage, pas une limite du modèle.
_PROFONDEUR_FIL = 12


def public_id(famille: str, ident) -> str:
    """L'identifiant servi d'un projet ou d'une page."""
    if famille not in _FAMILLES:
        raise ValueError(f"famille inconnue : {famille}")
    return f"{_PREFIXE}{famille}_{int(ident)}"


def cle_de(public_id_: str) -> Optional[tuple[str, int]]:
    """`(famille, id)` d'un identifiant servi par ce module, ou `None` s'il n'en est pas un."""
    for famille in _FAMILLES:
        tete = f"{_PREFIXE}{famille}_"
        if public_id_ and public_id_.startswith(tete):
            queue = public_id_[len(tete):]
            if queue.isdigit():
                return famille, int(queue)
    return None


def est_une_copie(source: Optional[dict]) -> bool:
    """Ce nœud est-il une ancienne copie d'un projet ou d'une page ?

    `source` : les `props` d'une fiche, ou une ligne du rail (qui en extrait `legacy`).
    Plus rien n'écrit `legacy` depuis l'arrêt de la recopie : un nœud qui le porte avec
    l'une de ces deux familles est du résidu, jamais la page vivante.
    """
    return (source or {}).get("legacy") in _FAMILLES


def _cle_arbre(famille: str, ident) -> str:
    """La clé de parenté dans l'arbre du rail — distincte des `nodes.id` entiers."""
    return f"{famille}:{int(ident)}"


def _ligne(famille: str, ident, parent: Optional[str], owner_type: str, owner_id: str,
           title: Optional[str], position=None) -> dict:
    """Une ligne à la forme de `db/shell._COLS`, pour que le rail l'arbore sans la connaître."""
    return {"public_id": public_id(famille, ident), "id": _cle_arbre(famille, ident),
            "parent_id": parent, "kind": _KIND, "owner_type": owner_type,
            "owner_id": owner_id, "position": position, "title": title or "",
            "role": None, "legacy": famille, "legacy_id": str(ident), "slug": None}


def lignes_pour_proprietaires(owners: Iterable[tuple[str, str]]) -> list[dict]:
    """Les projets non archivés de ces propriétaires, suivis de leurs pages.

    Deux requêtes, jamais une par projet. L'ordre est celui des surfaces d'origine :
    projets par nom, pages par position puis titre.
    """
    owners = list(owners)
    if not owners:
        return []
    with _connect() as conn:
        projets = conn.execute(
            "SELECT p.id, p.owner_type, p.owner_id, p.name FROM projects p "
            "WHERE p.archived_at IS NULL AND (p.owner_type, p.owner_id) IN "
            f"({','.join(['(%s, %s)'] * len(owners))}) ORDER BY p.name, p.id",
            [v for pair in owners for v in pair]).fetchall()
        ids = [p["id"] for p in projets]
        pages = conn.execute(
            "SELECT d.id, d.project_id, d.parent_id, d.title, d.position FROM docs d "
            "WHERE d.project_id = ANY(%s) "
            "ORDER BY d.position NULLS LAST, d.title, d.id", (ids,)).fetchall() if ids else []
    proprio = {p["id"]: (p["owner_type"], str(p["owner_id"])) for p in projets}
    out = [_ligne(FAMILLE_PROJET, p["id"], None, *proprio[p["id"]], p["name"])
           for p in projets]
    for d in pages:
        parent = (_cle_arbre(FAMILLE_PAGE, d["parent_id"]) if d["parent_id"] is not None
                  else _cle_arbre(FAMILLE_PROJET, d["project_id"]))
        out.append(_ligne(FAMILLE_PAGE, d["id"], parent, *proprio[d["project_id"]],
                          d["title"], d["position"]))
    return out


def lire(public_id_: str) -> Optional[dict]:
    """Un projet ou une page, à la forme qu'attend la fiche de nœud — ou `None`.

    Rend `{fiche, project_id, corps_md, chaine, freres}` :
    - `fiche` : la forme de `db/node_view.node_by_public_id` (props comprises) ;
    - `chaine` : de la racine (le projet) jusqu'au nœud inclus, comme `ancestors_of` ;
    - `freres` : `{parent_id de maillon: [voisins]}`, comme `siblings_of`.

    L'accès ne se juge PAS ici (même règle que `node_by_public_id`) : l'appelant le
    demande au projet.
    """
    cle = cle_de(public_id_)
    if cle is None:
        return None
    famille, ident = cle
    with _connect() as conn:
        if famille == FAMILLE_PROJET:
            projet = conn.execute(
                "SELECT id, owner_type, owner_id, name, brief_md, created_by, updated_at "
                "FROM projects WHERE id = %s AND archived_at IS NULL", (ident,)).fetchone()
            if not projet:
                return None
            pages_chaine: list[dict] = []
            corps, auteur, maj = projet["brief_md"], projet["created_by"], projet["updated_at"]
        else:
            page = conn.execute(
                "SELECT d.project_id, d.body_md, d.created_by, d.updated_at FROM docs d "
                "JOIN projects p ON p.id = d.project_id "
                "WHERE d.id = %s AND p.archived_at IS NULL", (ident,)).fetchone()
            if not page:
                return None
            projet = conn.execute(
                "SELECT id, owner_type, owner_id, name FROM projects WHERE id = %s",
                (page["project_id"],)).fetchone()
            pages_chaine = _pages_ancetres(conn, ident)
            corps, auteur, maj = page["body_md"], page["created_by"], page["updated_at"]
        freres = _freres(conn, projet, pages_chaine)

    chaine = [_maillon(FAMILLE_PROJET, projet["id"], None, projet["name"])]
    for p in pages_chaine:
        parent = (_cle_arbre(FAMILLE_PAGE, p["parent_id"]) if p["parent_id"] is not None
                  else _cle_arbre(FAMILLE_PROJET, projet["id"]))
        chaine.append(_maillon(FAMILLE_PAGE, p["id"], parent, p["title"]))
    moi = chaine[-1]
    props = dict(moi["props"], created_by=auteur)
    if famille == FAMILLE_PROJET:
        props["pinned"] = True
    else:
        props["project_id"] = projet["id"]
    fiche = {"id": moi["id"], "public_id": moi["public_id"], "parent_id": moi["parent_id"],
             "kind": _KIND, "owner_type": projet["owner_type"],
             "owner_id": str(projet["owner_id"]), "props": props, "updated_at": maj}
    return {"fiche": fiche, "project_id": projet["id"], "corps_md": corps or "",
            "chaine": chaine, "freres": freres}


def _maillon(famille: str, ident, parent: Optional[str], title: Optional[str]) -> dict:
    return {"id": _cle_arbre(famille, ident), "public_id": public_id(famille, ident),
            "parent_id": parent, "kind": _KIND,
            "props": {"legacy": famille, "legacy_id": str(ident), "title": title or ""}}


def _pages_ancetres(conn, doc_id: int) -> list[dict]:
    """Les pages de la racine du projet jusqu'à `doc_id` inclus.

    `docs.parent_id` porte une clé étrangère, mais rien n'y interdit une boucle : même
    garde qu'`ancestors_of` — un cycle se DIT (`ParentCycle`), il ne se sert pas.
    """
    rows = conn.execute(
        "WITH RECURSIVE chaine AS ("
        "  SELECT d.id, d.parent_id, d.title, 0 AS niveau FROM docs d WHERE d.id = %s"
        "  UNION ALL"
        "  SELECT p.id, p.parent_id, p.title, c.niveau + 1"
        "    FROM docs p JOIN chaine c ON p.id = c.parent_id WHERE c.niveau < %s"
        ") CYCLE id SET boucle USING chemin "
        "SELECT id, parent_id, title, boucle FROM chaine ORDER BY niveau DESC",
        (doc_id, _PROFONDEUR_FIL)).fetchall()
    chaine = [dict(r) for r in rows]
    # Pas de `any(c.pop(…) …)` : il s'arrêterait au premier vrai (cf. `ancestors_of`).
    marques = [bool(c.pop("boucle")) for c in chaine]
    if any(marques):
        raise ParentCycle(doc_id)
    return chaine


def _freres(conn, projet: dict, pages_chaine: list[dict], cap: int = 50) -> dict:
    """La fratrie de chaque maillon, bornée à `cap` — UNE requête par niveau de famille.

    Le projet a pour voisins les projets non archivés du MÊME propriétaire : sans cette
    borne, le popover du fil montrerait les projets d'une autre org. Une page a pour
    voisins les pages de même parent, dans le même projet.
    """
    voisins = conn.execute(
        "SELECT id, name FROM projects WHERE owner_type = %s AND owner_id = %s "
        "AND archived_at IS NULL ORDER BY name, id LIMIT %s",
        (projet["owner_type"], str(projet["owner_id"]), cap)).fetchall()
    out: dict = {None: [_frere(FAMILLE_PROJET, v["id"], v["name"]) for v in voisins]}
    if not pages_chaine:
        return out
    parents = [p["parent_id"] for p in pages_chaine]
    rows = conn.execute(
        "SELECT id, parent_id, title FROM docs WHERE project_id = %s "
        "AND (parent_id = ANY(%s) OR (%s AND parent_id IS NULL)) "
        "ORDER BY position NULLS LAST, title, id",
        (projet["id"], [p for p in parents if p is not None],
         any(p is None for p in parents))).fetchall()
    for r in rows:
        cle = (_cle_arbre(FAMILLE_PAGE, r["parent_id"]) if r["parent_id"] is not None
               else _cle_arbre(FAMILLE_PROJET, projet["id"]))
        seau = out.setdefault(cle, [])
        if len(seau) < cap:
            seau.append(_frere(FAMILLE_PAGE, r["id"], r["title"]))
    return out


def _frere(famille: str, ident, title: Optional[str]) -> dict:
    """Un voisin à la forme de `siblings_of`."""
    return {"public_id": public_id(famille, ident), "kind": _KIND, "title": title or "",
            "role": None, "legacy": famille, "legacy_id": str(ident), "slug": None}
