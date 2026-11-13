"""Les favoris d'une org (`org_favorites`) — le stockage seul, sans aucun droit.

Une ligne = une page (`doc`) ou un tableau (`table`) épinglé dans une org, avec qui l'a
épinglé et quand. `owner_sub` None = favori PARTAGÉ (portée `org`) ; posé = favori
PERSONNEL de ce membre (portée `me`). Qui peut LIRE quoi se juge dans la capacité
(`capabilities/orgs/favoris.py`) : ce module ne filtre rien.
"""
from __future__ import annotations

from typing import Any, Optional

from ._conn import _connect

KINDS = ("doc", "table")

_COLS = "org_id, owner_sub, kind, ref_id, added_by, added_at"


def lister(org_id: int, sub: str) -> list[dict[str, Any]]:
    """Les favoris PARTAGÉS de l'org et les favoris PERSONNELS de `sub` dans l'org —
    jamais ceux d'un autre membre —, dans l'ordre de pose (le plus ancien d'abord)."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_COLS} FROM org_favorites WHERE org_id = %s "
            "AND (owner_sub IS NULL OR owner_sub = %s) "
            "ORDER BY added_at, kind, ref_id, owner_sub NULLS FIRST",
            (int(org_id), sub)).fetchall()
    return [dict(r) for r in rows]


def poser(org_id: int, owner_sub: Optional[str], kind: str, ref_id: int,
          added_by: Optional[str]) -> None:
    """Épingle — idempotent : un favori déjà posé garde son auteur et sa date."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO org_favorites (org_id, owner_sub, kind, ref_id, added_by) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT ON CONSTRAINT org_favorites_une_ligne "
            "DO NOTHING",
            (int(org_id), owner_sub, kind, int(ref_id), added_by))


_LA_LIGNE = ("WHERE org_id = %s AND owner_sub IS NOT DISTINCT FROM %s "
             "AND kind = %s AND ref_id = %s")


def existe(org_id: int, owner_sub: Optional[str], kind: str, ref_id: int) -> bool:
    with _connect() as conn:
        return conn.execute(f"SELECT 1 FROM org_favorites {_LA_LIGNE}",
                            (int(org_id), owner_sub, kind, int(ref_id))).fetchone() is not None


def retirer(org_id: int, owner_sub: Optional[str], kind: str, ref_id: int, *,
            added_by: Optional[str] = None) -> bool:
    """Désépingle — idempotent. `owner_sub` None ne touche que le favori partagé ;
    posé, que le favori personnel de ce compte. `added_by` posé : seulement si ce compte
    l'a épinglé. Rend True si une ligne est partie."""
    sql, args = f"DELETE FROM org_favorites {_LA_LIGNE}", [int(org_id), owner_sub, kind,
                                                            int(ref_id)]
    if added_by is not None:
        sql, args = sql + " AND added_by = %s", args + [added_by]
    with _connect() as conn:
        return conn.execute(sql, args).rowcount > 0


def purger(conn, kind: str, ref_ids: list[int]) -> None:
    """Retire, dans TOUTES les orgs et pour tous leurs membres, les favoris d'objets qui
    n'existent plus — sur la connexion de l'appelant, pour tenir dans la transaction qui
    supprime l'objet (`delete_doc`, `delete_datastore_by_id`)."""
    if ref_ids:
        conn.execute("DELETE FROM org_favorites WHERE kind = %s AND ref_id = ANY(%s)",
                     (kind, [int(r) for r in ref_ids]))


def retirer_morts(kind: str, ref_ids: list[int]) -> None:
    """Le filet de la lecture : une ligne morte qui aurait échappé aux suppressions."""
    if not ref_ids:
        return
    with _connect() as conn:
        purger(conn, kind, ref_ids)
