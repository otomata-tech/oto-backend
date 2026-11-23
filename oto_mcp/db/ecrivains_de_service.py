"""Les écrivains de service d'un tableau (table `datastore_service_writers`).

Un service machine (`auth/service_identity`) n'a ni compte ni org : il n'écrit un
tableau que si une personne qui le GOUVERNE le lui a ouvert, pour un rôle de service
nommé. Ce module ne juge rien — la garde vit dans la capacité
(`capabilities/datastore/ecrivains_de_service.py`) ; il lit et écrit les lignes.

NON aplati dans `db/__init__` : `poser`/`retirer`/`lister` sont trop communs pour la
surface plate. Les appelants écrivent `db.ecrivains_de_service.<fn>`.
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect


def poser(ns_id: int, role: str, granted_by: str) -> dict:
    """Ouvre le tableau au rôle. Idempotent : une ouverture déjà posée garde sa date et
    son auteur d'origine (le second geste ne réécrit pas l'histoire)."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO datastore_service_writers (ns_id, role, granted_by) "
            "VALUES (%s, %s, %s) ON CONFLICT (ns_id, role) DO NOTHING",
            (int(ns_id), role, granted_by))
        return conn.execute(
            "SELECT ns_id, role, granted_by, granted_at FROM datastore_service_writers "
            "WHERE ns_id = %s AND role = %s", (int(ns_id), role)).fetchone()


def retirer(ns_id: int, role: str) -> bool:
    """Ferme le tableau au rôle ; False s'il ne lui était pas ouvert."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM datastore_service_writers WHERE ns_id = %s AND role = %s",
            (int(ns_id), role))
        return cur.rowcount > 0


def lister(ns_id: int) -> list[dict]:
    """Les rôles auxquels le tableau est ouvert, du plus ancien au plus récent."""
    with _connect() as conn:
        return list(conn.execute(
            "SELECT ns_id, role, granted_by, granted_at FROM datastore_service_writers "
            "WHERE ns_id = %s ORDER BY granted_at, role", (int(ns_id),)).fetchall())


def ouvert(ns_id: int, role: str) -> Optional[dict]:
    """L'ouverture du tableau à ce rôle, ou None — ce que lit l'écriture du service."""
    with _connect() as conn:
        return conn.execute(
            "SELECT ns_id, role, granted_by, granted_at FROM datastore_service_writers "
            "WHERE ns_id = %s AND role = %s", (int(ns_id), role)).fetchone()
