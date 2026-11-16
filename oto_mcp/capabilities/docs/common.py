"""Ce que les modules du domaine « doc » partagent — aucun descripteur ici.

Trois choses : le refus nommé, LE droit d'accès aux pages d'un projet, et l'exception
unique à ce droit — la page partagée SEULE (signal #1084). Le deuxième est un **seam
unique** : chaque branche du dispatcher l'appelle par `common.can(...)`, jamais par un
nom importé à l'unité. Un `from .common import can` figerait la fonction à l'import, et
l'unique point de bascule du domaine deviendrait autant de copies qu'il y a de modules —
ce que le fichier plat d'avant garantissait par construction et qu'un découpage perdrait
en silence.
"""
from __future__ import annotations

from typing import Optional

from ... import db, ownership, session_org
from .._types import AuthzDenied

PROJECT_RTYPE = "project"

# Le type de ressource d'UNE page, tel qu'il s'écrit dans `resource_grants` (ADR 0030 :
# la colonne est un TEXT libre, aucun DDL). Valeur PERSISTÉE — la renommer sans migrer les
# lignes ferait disparaître les partages en silence (même avertissement que
# `ownership.TYPE_RESSOURCE_DATASTORE`).
DOC_RTYPE = "doc"

# Ops servies au destinataire d'un projet publié : LECTURE seule. Tout le reste
# (création, édition, déplacement, publication de page) exige un `sub`
# — même posture que les tools de gouvernance du datastore.
# `search` en est ABSENT : il délègue à `search_mod.search(sub, …)`, dont le scoping
# est bâti sur un `sub` (projets accessibles). Le destinataire lit l'arbre (`list`)
# puis la page (`get`) — pas de chemin de recherche tant qu'il n'est pas scopé.
SHARED_READ_OPS = frozenset({"list", "get", "outline", "revisions", "backlinks"})


def face_de_l_appel() -> str:
    """La porte de l'écriture en cours, pour `db.update_doc` (oto#274) : `mcp` si l'appel
    est entré par un tool, sinon `rest` — une capacité du domaine n'a que ces deux
    portes, et `session_org.current_call_face()` n'est posée que sur la face MCP."""
    if session_org.current_call_face() == session_org.FACE_MCP:
        return db.DOC_FACE_MCP
    return db.DOC_FACE_REST


# Le refus `missing_project` des ops par projet (create, bulk_create, list, search) :
# sur la face MCP, le jeton d'appel `_project` en tient lieu (`core._projet_de_l_appel`).
PROJET_REQUIS = "`project_id` is required (on MCP, the `_project` call token works too)."


def require(cond, code: str, msg: str, status: int = 400) -> None:
    if not cond:
        raise AuthzDenied(status, code, msg)


def can(sub: Optional[str], project_id: int, want: str) -> bool:
    """Droit d'accès aux pages d'un projet. `sub is None` = destinataire d'un endpoint
    publié (ADR 0032) : LECTURE seule, et seulement sur LE projet publié — jamais
    l'arbre documentaire de l'org (pendant de `_anon_project_tableau_ns_ids`).
    Fail-closed : hors de ce projet, ou pour une écriture, c'est non."""
    if sub is None:
        if want != "read":
            return False
        from ... import subdomain_project
        pid = subdomain_project.current_anon_project_id()
        return (pid is not None and int(pid) == int(project_id)
                and subdomain_project.current_anon_docs_exposed())
    return ownership.can_access(sub, PROJECT_RTYPE, str(project_id), want)


# Comment le lecteur atteint une page : par son PROJET, ou par un partage de CETTE page.
PAR_LE_PROJET, PAR_LA_PAGE = "project", "page"


def acces_a_la_page(sub: Optional[str], row: dict) -> Optional[str]:
    """LIRE une page : `PAR_LE_PROJET`, `PAR_LA_PAGE`, ou None (refus).

    ⚠️ **C'est la seule porte qui honore un partage de page, et elle ne sert que
    `op=get` et `op=outline`** (le plan de la MÊME page, #1266). Toute autre op (`list`, `search`, `revisions`, `backlinks`, les
    écritures) reste gardée par `can()` sur le PROJET : un destinataire qui n'a que la
    page n'atteint ni ses sœurs, ni ses sous-pages, ni son historique, ni ce qui la cite.
    Le grant est lu sur `(doc, id)` de CETTE ligne — jamais sur le projet, jamais sur un
    parent : une sous-page d'une page partagée n'hérite de rien (même sémantique que
    `set_public`, « this page ALONE »). Jamais pour un lecteur anonyme : un partage de
    page désigne un compte, une org ou une équipe, pas un lien."""
    if can(sub, row["project_id"], "read"):
        return PAR_LE_PROJET
    if sub is not None and ownership.can_access(sub, DOC_RTYPE, str(row["id"]), "read"):
        return PAR_LA_PAGE
    return None


# ── Le kind `doc` du seam `ownership` ─────────────────────────────────────────────
# Une page n'a PAS de propriétaire propre : elle est au propriétaire de son projet, et
# c'est son PROJET qui la gouverne (`governed_by`) — qui peut partager le projet peut
# partager une de ses pages, un lecteur de la page ne le peut jamais.

def _page(rid: str) -> Optional[dict]:
    return db.get_doc_by_id(int(rid)) if str(rid).isdigit() else None


def _page_owner(rid: str) -> Optional[tuple[str, str]]:
    row = _page(rid)
    return ownership.owner_of(PROJECT_RTYPE, str(row["project_id"])) if row else None


def _page_project(rid: str) -> Optional[tuple[str, str]]:
    row = _page(rid)
    return (PROJECT_RTYPE, str(row["project_id"])) if row else None


def _page_reparent(rid: str, new_owner_type: str, new_owner_id: str) -> None:
    raise ValueError("A page has no owner of its own: it belongs to its project. Move it "
                     "with oto_doc op=move (to_project), or transfer the project.")


ownership.register_kind(DOC_RTYPE, ownership.ResourceKind(
    owner_getter=_page_owner, reparent=_page_reparent, governed_by=_page_project))
