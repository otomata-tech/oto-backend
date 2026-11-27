"""Commentaires de page (doc) : lecture et écriture de `doc_comments`
(`docs/commentaires-de-procedure.md` §Pages de mémoire).

Le pendant exact de `procedure_commentaires`, clé par la page, sans `version`.
NON aplati dans `db.*` : `lister`/`ajouter`/`retirer` sont trop communs pour la surface
plate. Les appelants écrivent `db.doc_commentaires.<fn>`.

Un commentaire retiré garde sa ligne (`deleted_at`) et ne se sert plus : aucune lecture
d'ici ne le rend, sauf la racine d'un fil qui a encore des réponses vivantes (`_SERVI`).
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect

_COLONNES = "id, doc_id, parent_id, quote, quote_prefix, auteur_sub, body, created_at, deleted_at"

# Ce qui se sert : le vivant, et la RACINE retirée d'un fil qui a encore des réponses
# vivantes — servie comme « commentaire supprimé », pour que le fil garde son passage.
_SERVI = ("(c.deleted_at IS NULL OR (c.parent_id IS NULL AND EXISTS (SELECT 1 FROM "
          "doc_comments r WHERE r.parent_id = c.id AND r.deleted_at IS NULL)))")


def _colonnes() -> str:
    return ", ".join(f"c.{x.strip()}" for x in _COLONNES.split(","))


def lister(doc_id: int, *, avant_id: Optional[int] = None, limit: int = 50) -> list[dict]:
    """Les commentaires servis d'une page, le plus récent d'abord (`id` décroissant), au
    plus `limit + 1` : celui de trop dit qu'il reste une page."""
    clauses = ["c.doc_id = %s", _SERVI]
    params: list = [int(doc_id)]
    if avant_id is not None:
        clauses.append("c.id < %s")
        params.append(int(avant_id))
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT {_colonnes()} FROM doc_comments c "
            f"WHERE {' AND '.join(clauses)} ORDER BY c.id DESC LIMIT %s",
            (*params, int(limit) + 1)).fetchall()]


def compter(doc_id: int) -> int:
    """Le nombre de commentaires vivants d'une page (parcours de l'index partiel)."""
    with _connect() as conn:
        r = conn.execute(
            "SELECT count(*) AS n FROM doc_comments WHERE doc_id = %s AND deleted_at IS NULL",
            (int(doc_id),)).fetchone()
    return int(r["n"]) if r else 0


def ajouter(doc_id: int, *, auteur_sub: Optional[str], body: str,
            parent_id: Optional[int] = None, quote: Optional[str] = None,
            quote_prefix: Optional[str] = None) -> dict:
    with _connect() as conn:
        return dict(conn.execute(
            f"INSERT INTO doc_comments (doc_id, parent_id, quote, quote_prefix, auteur_sub, "
            f"body) VALUES (%s, %s, %s, %s, %s, %s) RETURNING {_COLONNES}",
            (int(doc_id), parent_id, quote, quote_prefix, auteur_sub, body)).fetchone())


def lire(doc_id: int, comment_id: int, *, servi: bool = False) -> Optional[dict]:
    """Un commentaire VIVANT de cette page, ou `None`. `servi=True` : aussi la racine
    retirée d'un fil encore servi (on peut toujours y répondre)."""
    garde = _SERVI if servi else "c.deleted_at IS NULL"
    with _connect() as conn:
        r = conn.execute(
            f"SELECT {_colonnes()} FROM doc_comments c WHERE c.id = %s AND c.doc_id = %s "
            f"AND {garde}", (int(comment_id), int(doc_id))).fetchone()
    return dict(r) if r else None


def retirer(comment_id: int) -> bool:
    """Retire CE commentaire, et lui seul. Faux s'il l'était déjà."""
    with _connect() as conn:
        return conn.execute(
            "UPDATE doc_comments SET deleted_at = now() "
            "WHERE id = %s AND deleted_at IS NULL", (int(comment_id),)).rowcount == 1
