"""Capacité « commentaires d'une ligne » : ce que les personnes disent d'une ligne, à
côté de ses valeurs (`docs/datastore.md` §Commentaires) — et, depuis le 20/11/2026,
d'une CELLULE (la ligne + `column`) ou d'une COLONNE entière
(`/api/datastores/{datastore}/columns/{column}/comments`).

**Cellule.** Un commentaire de cellule EST un commentaire de sa ligne qui nomme une
colonne : le fil de la ligne le rend (avec `column`), `?column=` ne garde que cette
cellule, et le compte de la ligne le compte. **Colonne.** Sans ligne : ni le fil d'une
ligne ni son compte ne le rendent ; ses routes à elle, et `columns` dans les comptes.
**Face agent.** Les commentaires de cellule suivent l'`agent_access` de leur colonne,
comme l'historique d'une ligne (`history.py`) : sur un appel d'agent, une colonne
masquée (`"none"`) n'a pas de commentaires — exclus en SQL à la lecture, pour que la
pagination reste juste — et ne se commente pas (`400 unknown_column`, le refus d'une
colonne qui n'existe pas : l'agent n'en apprend pas plus que du schéma servi).

Une colonne se nomme par sa clé : déclarée au schéma, ou présente dans les données
(un tableau sans schéma complet a des colonnes non déclarées). Une colonne retirée du
schéma garde ses commentaires, toujours servis : rien ne se détruit en silence.

`GET|POST /api/datastores/{datastore}/rows/{row_id}/comments`, `DELETE
…/comments/{comment_id}` ; sur la face agent `data_row_comments` (lire) et
`data_row_comment` (écrire). La source est `datastore_row_comments`
(`db/datastore_commentaires.py`) — PAS une colonne de la ligne : commenter ne fait
avancer ni sa `rev` ni son journal.

**Accès.** Lire ET écrire un commentaire, c'est l'accès de LECTURE de la ligne : le
tableau se résout par le store (org active, ownership, périmètre d'un endpoint partagé),
comme `GET …/rows/{row_id}`. Un lecteur à qui l'on a partagé un tableau peut donc en
commenter les lignes — c'est l'usage visé : une personne qui ne change pas la donnée dit
ce qu'elle en pense. Un tableau hors périmètre est un 404.

**Une ligne supprimée garde ses commentaires**, lisibles par qui GOUVERNE le tableau
seulement (même règle et même raison que l'historique, `history.py`) ; personne n'y
écrit plus : `404 row_not_found`.

**Retirer** un commentaire : son auteur, ou qui gouverne le tableau. Le retrait est
doux (`deleted_at`) et le commentaire ne se sert plus.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator

from ... import db, ownership
from ...datastore import acces_agent as aga
from ...datastore import identite
from ...datastore.core import DatastoreNotFound, make_store
from ...datastore.identite import Adresse
from .._authz import SUB_ONLY
from .._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES
from ._refus import _JETON_MAL_PLACE
from .common import EntreeDatastore, HORODATAGE, ns_not_found
from .rows import _adresse

LIMITE_DEFAUT = 50
LIMITE_MAX = 200
LONGUEUR_MAX = 5000


class _Ligne(EntreeDatastore):
    datastore: Adresse
    row_id: str


class RowCommentsInput(_Ligne):
    column: Optional[str] = Field(default=None, description=(
        "Keep only the comments of this CELL (the row's value in this column). Omitted: "
        "the row's comments AND its cells'."))
    limit: int = Field(default=LIMITE_DEFAUT, description=(
        f"Comments per page, the most recent page first ({LIMITE_MAX} at most)."))
    before_id: Optional[int] = Field(default=None, description=(
        "Read the next (older) page: the previous answer's `next_before_id`."))


class RowCommentInput(_Ligne):
    body: str = Field(description=f"The comment, plain text, 1 to {LONGUEUR_MAX} characters.")
    column: Optional[str] = Field(default=None, description=(
        "Comment on one CELL: the column key (declared in the schema or present in the "
        "row). Omit to comment on the whole row."))

    @field_validator("body", mode="after")
    @classmethod
    def _corps(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("`body` is empty")
        if len(v) > LONGUEUR_MAX:
            raise ValueError(f"`body` is longer than {LONGUEUR_MAX} characters")
        return v


class DeleteRowCommentInput(_Ligne):
    comment_id: int


class Comment(BaseModel):
    id: int
    column: Optional[str] = Field(default=None, description=(
        "The column this comment is about: with a row, a CELL; null = the whole row "
        "(on a column's own thread, always that column)."))
    author: Optional[str] = Field(default=None, description="The author's sub.")
    author_email: Optional[str] = Field(default=None, description=(
        "Resolved at read time; absent for an unknown account."))
    body: str
    at: Optional[str] = Field(default=None, description=HORODATAGE)
    mine: bool = Field(description="Written by the caller.")
    can_delete: bool = Field(description="The caller may remove it (author or governor).")


class RowComments(BaseModel):
    datastore: Optional[str] = None
    ns_id: Optional[int] = Field(default=None, description=identite.DESCRIPTION)
    row_id: str
    comments: list[Comment] = Field(description="Oldest first within the page.")
    next_before_id: Optional[int] = Field(default=None, description=(
        "Pass as `before_id` to read the older page. `null` = nothing older."))


class _Colonne(EntreeDatastore):
    datastore: Adresse
    column: str = Field(description="The column key.")


class ColumnCommentsInput(_Colonne):
    limit: int = Field(default=LIMITE_DEFAUT, description=(
        f"Comments per page, the most recent page first ({LIMITE_MAX} at most)."))
    before_id: Optional[int] = Field(default=None, description=(
        "Read the next (older) page: the previous answer's `next_before_id`."))


class ColumnCommentInput(_Colonne):
    body: str = Field(description=f"The comment, plain text, 1 to {LONGUEUR_MAX} characters.")

    @field_validator("body", mode="after")
    @classmethod
    def _corps(cls, v: str) -> str:
        return RowCommentInput._corps(v)


class DeleteColumnCommentInput(_Colonne):
    comment_id: int


class ColumnComments(BaseModel):
    datastore: Optional[str] = None
    ns_id: Optional[int] = Field(default=None, description=identite.DESCRIPTION)
    column: str
    comments: list[Comment] = Field(description="Oldest first within the page.")
    next_before_id: Optional[int] = Field(default=None, description=(
        "Pass as `before_id` to read the older page. `null` = nothing older."))


MAX_IDS = 100


class CommentCountsInput(EntreeDatastore):
    datastore: Adresse
    # Même forme que `fields`/`versions` des lignes (#367) : une URL ne transporte que
    # des chaînes — `?row_ids=a,b` et/ou `?row_ids=a&row_ids=b`, les deux se combinent.
    row_ids: list[str] | str = Field(description=(
        f"The rows to count, at most {MAX_IDS}: comma-separated, or the parameter "
        "repeated."))

    @field_validator("row_ids", mode="after")
    @classmethod
    def _ids_en_liste(cls, v):
        brut = v if isinstance(v, list) else str(v).split(",")
        ids = list(dict.fromkeys(m for m in (str(x).strip() for x in brut) if m))
        if not ids:
            raise ValueError("`row_ids` is empty: name the rows to count")
        if len(ids) > MAX_IDS:
            raise ValueError(f"`row_ids` names {len(ids)} rows, at most {MAX_IDS}")
        return ids


class LatestComment(BaseModel):
    id: int
    body: str = Field(description=(
        f"The first {db.datastore_commentaires.APERCU} characters; the whole text is "
        "served by the row's comment list."))
    body_truncated: bool
    author: Optional[str] = None
    author_email: Optional[str] = None
    at: Optional[str] = Field(default=None, description=HORODATAGE)


class CommentCounts(BaseModel):
    datastore: Optional[str] = None
    ns_id: Optional[int] = Field(default=None, description=identite.DESCRIPTION)
    counts: dict[str, int] = Field(description=(
        "`{row_id: n}` live comments per requested row that still exists; rows "
        "without any are omitted."))
    total: int
    latest: dict[str, LatestComment] = Field(description=(
        "`{row_id: comment}` each counted row's newest live comment."))
    cells: dict[str, dict[str, int]] = Field(default_factory=dict, description=(
        "`{row_id: {column: n}}` live CELL comments of the requested rows (also counted "
        "in `counts`); cells without any are omitted."))
    columns: dict[str, int] = Field(default_factory=dict, description=(
        "`{column: n}` live comments on each column itself, for the whole table; "
        "columns without any are omitted."))


class Deleted(BaseModel):
    deleted: bool


def _ligne(ctx: ResolvedCtx, inp: _Ligne, *, ecrire: bool = False):
    """Le tableau et la ligne, sous l'accès de LECTURE de l'appelant. Rend `(store, ns,
    ns_id, row_id, gouverne, absente)`."""
    ns, row_id = _adresse(inp.datastore, inp.row_id)
    store = make_store(ctx.sub)
    try:
        ns_id = store.resolve_ns_id(ns)
    except DatastoreNotFound:
        raise ns_not_found(ctx.sub, ns)
    gouverne = ownership.can_govern(ctx.sub, ownership.TYPE_RESSOURCE_DATASTORE, str(ns_id))
    absente = db.datastore_get_row(ns_id, row_id) is None
    if absente and (ecrire or not gouverne):
        raise AuthzDenied(404, "row_not_found")
    return store, ns, ns_id, row_id, gouverne, absente


def _tableau(ctx: ResolvedCtx, datastore: str):
    """Le tableau, sous l'accès de LECTURE de l'appelant : `(store, ns, ns_id, gouverne)`."""
    ns, _ = _adresse(datastore)
    store = make_store(ctx.sub)
    try:
        ns_id = store.resolve_ns_id(ns)
    except DatastoreNotFound:
        raise ns_not_found(ctx.sub, ns)
    gouverne = ownership.can_govern(ctx.sub, ownership.TYPE_RESSOURCE_DATASTORE, str(ns_id))
    return store, ns, ns_id, gouverne


def _colonne_connue(ns_id: int, colonne: str, donnees: Optional[dict] = None) -> str:
    """La clé de colonne, si le tableau la connaît : déclarée au schéma, présente dans la
    ligne visée (`donnees`), ou, pour une colonne, dans les données du tableau. Sinon
    `400 unknown_column` — un commentaire sur une colonne qui n'existe pas ne se
    retrouverait jamais."""
    cle = (colonne or "").strip()
    if cle:
        tableau = db.get_datastore_by_id(ns_id) or {}
        declarees = {f.get("key") for f in ((tableau.get("schema") or {}).get("fields") or [])
                     if isinstance(f, dict)}
        if cle in declarees:
            return cle
        if donnees is not None:
            if cle in donnees:
                return cle
        elif cle in db.datastore_row_keys(ns_id):
            return cle
    raise _refus_colonne(colonne)


def _refus_colonne(colonne: str) -> AuthzDenied:
    return AuthzDenied(400, "unknown_column", (
        f"No column `{colonne}` in this table: name a column key declared in its schema "
        "(or present in its data)."))


def _masquees(ns_id: int) -> frozenset:
    """Les colonnes masquées à l'appelant : celles d'`agent_access: "none"` sur un appel
    d'agent, aucune ailleurs (l'écran du propriétaire voit tout) — la règle de
    l'historique (`history._masquees`)."""
    if not aga.appel_d_agent():
        return frozenset()
    ns = db.get_datastore_by_id(ns_id) or {}
    return frozenset(aga.masquees(ns.get("schema")))


def _servi(c: dict, sub: Optional[str], gouverne: bool, emails: dict) -> dict:
    auteur = c["auteur_sub"]
    a_moi = auteur is not None and auteur == sub
    return {
        "id": c["id"],
        "column": c.get("column_key"),
        "author": auteur,
        "author_email": emails.get(auteur),
        "body": c["body"],
        "at": c["created_at"],
        "mine": a_moi,
        "can_delete": a_moi or gouverne,
    }


def _row_comments(ctx: ResolvedCtx, inp: RowCommentsInput) -> dict:
    store, ns, ns_id, row_id, gouverne, absente = _ligne(ctx, inp)
    limit = max(1, min(LIMITE_MAX, inp.limit))
    cachees = _masquees(ns_id)
    lus = ([] if inp.column in cachees else db.datastore_commentaires.lister(
        ns_id, row_id, colonne=inp.column, avant_id=inp.before_id, limit=limit,
        sauf_colonnes=cachees))
    # Comme l'historique : une ligne absente sans rien à montrer est une ligne
    # inconnue, pas une ligne sans commentaire.
    if absente and not lus and inp.before_id is None:
        raise AuthzDenied(404, "row_not_found")
    suite = lus[limit - 1]["id"] if len(lus) > limit else None
    page = list(reversed(lus[:limit]))
    emails = db.emails_by_subs([c["auteur_sub"] for c in page])
    return {
        **identite.de_releve(store.dernier_tableau, ns),
        "row_id": row_id,
        "comments": [_servi(c, ctx.sub, gouverne, emails) for c in page],
        "next_before_id": suite,
    }


def _comment_counts(ctx: ResolvedCtx, inp: CommentCountsInput) -> dict:
    ns, _ = _adresse(inp.datastore)
    store = make_store(ctx.sub)
    try:
        ns_id = store.resolve_ns_id(ns)
    except DatastoreNotFound:
        raise ns_not_found(ctx.sub, ns)
    lignes = db.datastore_commentaires.comptes(ns_id, inp.row_ids)
    emails = db.emails_by_subs([l["auteur_sub"] for l in lignes])
    cellules: dict[str, dict[str, int]] = {}
    for c in db.datastore_commentaires.comptes_cellules(ns_id, inp.row_ids):
        cellules.setdefault(c["row_id"], {})[c["column_key"]] = int(c["n"])
    return {
        **identite.de_releve(store.dernier_tableau, ns),
        "counts": {l["row_id"]: int(l["n"]) for l in lignes},
        "total": sum(int(l["n"]) for l in lignes),
        "latest": {l["row_id"]: {
            "id": l["id"], "body": l["apercu"], "body_truncated": l["tronque"],
            "author": l["auteur_sub"],
            "author_email": emails.get(l["auteur_sub"]), "at": l["created_at"],
        } for l in lignes},
        "cells": cellules,
        "columns": db.datastore_commentaires.comptes_colonnes(ns_id),
    }


def _row_comment(ctx: ResolvedCtx, inp: RowCommentInput) -> dict:
    _, _, ns_id, row_id, gouverne, _ = _ligne(ctx, inp, ecrire=True)
    colonne = None
    if inp.column is not None:
        if inp.column.strip() in _masquees(ns_id):
            raise _refus_colonne(inp.column)
        colonne = _colonne_connue(ns_id, inp.column,
                                  (db.datastore_get_row(ns_id, row_id) or {}).get("data") or {})
    c = db.datastore_commentaires.ajouter(ns_id, row_id, auteur_sub=ctx.sub, body=inp.body,
                                          colonne=colonne)
    return _servi(c, ctx.sub, gouverne, db.emails_by_subs([ctx.sub]))


def _column_comments(ctx: ResolvedCtx, inp: ColumnCommentsInput) -> dict:
    store, ns, ns_id, gouverne = _tableau(ctx, inp.datastore)
    limit = max(1, min(LIMITE_MAX, inp.limit))
    lus = db.datastore_commentaires.lister_colonne(ns_id, inp.column, avant_id=inp.before_id,
                                                   limit=limit)
    suite = lus[limit - 1]["id"] if len(lus) > limit else None
    page = list(reversed(lus[:limit]))
    emails = db.emails_by_subs([c["auteur_sub"] for c in page])
    return {
        **identite.de_releve(store.dernier_tableau, ns),
        "column": inp.column,
        "comments": [_servi(c, ctx.sub, gouverne, emails) for c in page],
        "next_before_id": suite,
    }


def _column_comment(ctx: ResolvedCtx, inp: ColumnCommentInput) -> dict:
    _, _, ns_id, gouverne = _tableau(ctx, inp.datastore)
    colonne = _colonne_connue(ns_id, inp.column)
    c = db.datastore_commentaires.ajouter(ns_id, None, auteur_sub=ctx.sub, body=inp.body,
                                          colonne=colonne)
    return _servi(c, ctx.sub, gouverne, db.emails_by_subs([ctx.sub]))


def _delete_column_comment(ctx: ResolvedCtx, inp: DeleteColumnCommentInput) -> dict:
    _, _, ns_id, gouverne = _tableau(ctx, inp.datastore)
    c = db.datastore_commentaires.lire_colonne(ns_id, inp.column, inp.comment_id)
    if c is None:
        raise AuthzDenied(404, "comment_not_found")
    if not gouverne and (ctx.sub is None or c["auteur_sub"] != ctx.sub):
        raise AuthzDenied(403, "comment_not_yours")
    db.datastore_commentaires.retirer(c["id"])
    return {"deleted": True}


def _delete_row_comment(ctx: ResolvedCtx, inp: DeleteRowCommentInput) -> dict:
    _, _, ns_id, row_id, gouverne, _ = _ligne(ctx, inp)
    c = db.datastore_commentaires.lire(ns_id, row_id, inp.comment_id)
    if c is None:
        raise AuthzDenied(404, "comment_not_found")
    if not gouverne and (ctx.sub is None or c["auteur_sub"] != ctx.sub):
        raise AuthzDenied(403, "comment_not_yours")
    db.datastore_commentaires.retirer(c["id"])
    return {"deleted": True}


_CHEMIN = "/api/datastores/{datastore}/rows/{row_id}/comments"
_CHEMIN_COLONNE = "/api/datastores/{datastore}/columns/{column}/comments"
_COLONNE_INCONNUE = DeclaredError(400, "unknown_column",
                                  "aucune colonne de cette clé dans ce tableau (schéma ou "
                                  "données)")
_TABLEAU_INCONNU = DeclaredError(404, "datastore_not_found",
                                 "le tableau ne se voit pas depuis l'org de l'appel")

CAPABILITIES += [
    Capability(
        key="me.datastore.row_comments",
        handler=_row_comments,
        Input=RowCommentsInput,
        Output=RowComments,
        authz=SUB_ONLY,
        mcp="data_row_comments",
        rest=RestBinding(verb="GET", path=_CHEMIN, provisoire=True),
        errors=(
            _TABLEAU_INCONNU,
            DeclaredError(404, "row_not_found",
                          "aucune ligne de cet `_id` dans ce tableau ; les commentaires "
                          "d'une ligne SUPPRIMÉE ne se lisent que par qui gouverne le "
                          "tableau"),
            _JETON_MAL_PLACE,
        ),
        description=(
            "Read the comments people left on ONE datastore row and its cells (each "
            "tagged with its `column`; `column` keeps one cell), oldest first within a "
            "page; `before_id` (= the previous `next_before_id`) reads older ones. "
            "Comments are not row values: they never change the row or its history."
        ),
    ),
    # REST seule : le front la lit une fois par chargement de tableau (icônes, colonne
    # « Commentaires ») ; un agent lit le fil d'une ligne, il n'a pas besoin du compte.
    Capability(
        key="me.datastore.comment_counts",
        handler=_comment_counts,
        Input=CommentCountsInput,
        Output=CommentCounts,
        authz=SUB_ONLY,
        rest=RestBinding(verb="GET", path="/api/datastores/{datastore}/comments/counts",
                         provisoire=True),
        errors=(_TABLEAU_INCONNU, _JETON_MAL_PLACE),
        description=(
            "Count the live comments of the named rows of a datastore (`row_ids`, at "
            f"most {MAX_IDS}), with each row's newest as a preview; rows that no longer "
            "exist or have no comment are omitted."
        ),
    ),
    Capability(
        key="me.datastore.row_comment",
        handler=_row_comment,
        Input=RowCommentInput,
        Output=Comment,
        authz=SUB_ONLY,
        mcp="data_row_comment",
        rest=RestBinding(verb="POST", path=_CHEMIN, status=201, provisoire=True),
        errors=(
            _TABLEAU_INCONNU,
            DeclaredError(404, "row_not_found",
                          "aucune ligne de cet `_id` dans ce tableau (une ligne "
                          "supprimée ne se commente plus)"),
            _COLONNE_INCONNUE,
            _JETON_MAL_PLACE,
        ),
        description=(
            "Add a comment to ONE datastore row, or to one of its cells (`column`), as the "
            "caller. Anyone who can read the row can comment. It does not change the row: "
            "to change a value, write it."
        ),
    ),
    Capability(
        key="me.datastore.delete_row_comment",
        handler=_delete_row_comment,
        Input=DeleteRowCommentInput,
        Output=Deleted,
        authz=SUB_ONLY,
        rest=RestBinding(verb="DELETE", path=_CHEMIN + "/{comment_id}", provisoire=True),
        errors=(
            _TABLEAU_INCONNU,
            DeclaredError(404, "row_not_found", "aucune ligne de cet `_id` dans ce tableau"),
            DeclaredError(404, "comment_not_found",
                          "aucun commentaire vivant de cet `id` sur cette ligne"),
            DeclaredError(403, "comment_not_yours",
                          "seuls son auteur et qui gouverne le tableau retirent un "
                          "commentaire"),
            _JETON_MAL_PLACE,
        ),
        description="Remove one comment from a datastore row (its author or the table's governor).",
    ),
    # Les commentaires d'une COLONNE entière : REST seule, comme les comptes — l'écran
    # du tableau les montre dans l'en-tête de colonne ; aucun outil agent de plus.
    Capability(
        key="me.datastore.column_comments",
        handler=_column_comments,
        Input=ColumnCommentsInput,
        Output=ColumnComments,
        authz=SUB_ONLY,
        rest=RestBinding(verb="GET", path=_CHEMIN_COLONNE, provisoire=True),
        errors=(_TABLEAU_INCONNU, _JETON_MAL_PLACE),
        description=("Read the comments people left on ONE column of a datastore (the column "
                     "itself, not its cells), oldest first within a page."),
    ),
    Capability(
        key="me.datastore.column_comment",
        handler=_column_comment,
        Input=ColumnCommentInput,
        Output=Comment,
        authz=SUB_ONLY,
        rest=RestBinding(verb="POST", path=_CHEMIN_COLONNE, status=201, provisoire=True),
        errors=(_TABLEAU_INCONNU, _COLONNE_INCONNUE, _JETON_MAL_PLACE),
        description=("Add a comment to ONE column of a datastore, as the caller. Anyone who "
                     "can read the table can comment. It does not change the table."),
    ),
    Capability(
        key="me.datastore.delete_column_comment",
        handler=_delete_column_comment,
        Input=DeleteColumnCommentInput,
        Output=Deleted,
        authz=SUB_ONLY,
        rest=RestBinding(verb="DELETE", path=_CHEMIN_COLONNE + "/{comment_id}",
                         provisoire=True),
        errors=(
            _TABLEAU_INCONNU,
            DeclaredError(404, "comment_not_found",
                          "aucun commentaire vivant de cet `id` sur cette colonne"),
            DeclaredError(403, "comment_not_yours",
                          "seuls son auteur et qui gouverne le tableau retirent un "
                          "commentaire"),
            _JETON_MAL_PLACE,
        ),
        description="Remove one comment from a datastore column (its author or the table's governor).",
    ),
]
