"""Commentaires de procédure : lecture et écriture de `procedure_comments`
(`docs/commentaires-de-procedure.md`).

NON aplati dans `db.*` : `lister`/`ajouter`/`retirer` sont trop communs pour la surface
plate. Les appelants écrivent `db.procedure_commentaires.<fn>`.

Un commentaire retiré garde sa ligne (`deleted_at`) et ne se sert plus :
aucune lecture d'ici ne le rend.
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect

_COLONNES = ("id, instruction_id, parent_id, version, quote, quote_prefix, auteur_sub, body, "
             "created_at, deleted_at")

# Ce qui se sert : le vivant, et la RACINE retirée d'un fil qui a encore des réponses
# vivantes — servie comme « commentaire supprimé », pour que le fil garde son passage.
_SERVI = ("(c.deleted_at IS NULL OR (c.parent_id IS NULL AND EXISTS (SELECT 1 FROM "
          "procedure_comments r WHERE r.parent_id = c.id AND r.deleted_at IS NULL)))")


def lister(instruction_id: int, *, avant_id: Optional[int] = None,
           limit: int = 50) -> list[dict]:
    """Les commentaires vivants d'une procédure, le plus récent d'abord (`id`
    décroissant), au plus `limit + 1` : celui de trop dit qu'il reste une page."""
    clauses = ["c.instruction_id = %s", _SERVI]
    params: list = [int(instruction_id)]
    if avant_id is not None:
        clauses.append("c.id < %s")
        params.append(int(avant_id))
    colonnes = ", ".join(f"c.{x.strip()}" for x in _COLONNES.split(","))
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT {colonnes} FROM procedure_comments c "
            f"WHERE {' AND '.join(clauses)} ORDER BY c.id DESC LIMIT %s",
            (*params, int(limit) + 1)).fetchall()]


def compter(instruction_id: int) -> int:
    """Le nombre de commentaires vivants d'une procédure (parcours de l'index partiel)."""
    with _connect() as conn:
        r = conn.execute(
            "SELECT count(*) AS n FROM procedure_comments "
            "WHERE instruction_id = %s AND deleted_at IS NULL",
            (int(instruction_id),)).fetchone()
    return int(r["n"]) if r else 0


def ajouter(instruction_id: int, *, version: Optional[int], auteur_sub: Optional[str],
            body: str, parent_id: Optional[int] = None, quote: Optional[str] = None,
            quote_prefix: Optional[str] = None) -> dict:
    with _connect() as conn:
        return dict(conn.execute(
            f"INSERT INTO procedure_comments (instruction_id, parent_id, version, quote, "
            f"quote_prefix, auteur_sub, body) VALUES (%s, %s, %s, %s, %s, %s, %s) "
            f"RETURNING {_COLONNES}",
            (int(instruction_id), parent_id, version, quote, quote_prefix, auteur_sub,
             body)).fetchone())


def lire(instruction_id: int, comment_id: int, *, servi: bool = False) -> Optional[dict]:
    """Un commentaire VIVANT de cette procédure, ou `None`. `servi=True` : aussi la
    racine retirée d'un fil encore servi (on peut toujours y répondre)."""
    garde = _SERVI if servi else "c.deleted_at IS NULL"
    colonnes = ", ".join(f"c.{x.strip()}" for x in _COLONNES.split(","))
    with _connect() as conn:
        r = conn.execute(
            f"SELECT {colonnes} FROM procedure_comments c WHERE c.id = %s "
            f"AND c.instruction_id = %s AND {garde}",
            (int(comment_id), int(instruction_id))).fetchone()
    return dict(r) if r else None


def retirer(comment_id: int) -> bool:
    """Retire CE commentaire, et lui seul : une racine retirée dont le fil a des réponses
    reste servie comme « supprimé » (`_SERVI`). Faux s'il l'était déjà."""
    with _connect() as conn:
        return conn.execute(
            "UPDATE procedure_comments SET deleted_at = now() "
            "WHERE id = %s AND deleted_at IS NULL", (int(comment_id),)).rowcount == 1
