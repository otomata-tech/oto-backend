"""Guides on-demand DB (ADR 0042) : prose how-to par (scope, owner), chargée à la
demande via `oto_guide`. Scope `org`|`user` (platform = fichiers, cf. `guide_store`).
Distinct des procédures (`org_instructions`, slots) et des readmes init. Ré-exporté
par `db/__init__`."""
from __future__ import annotations

from typing import Optional

from ._conn import _connect


def list_guides_db(scope: str, owner_id: str) -> list[dict]:
    """Guides d'un (scope, owner), triés par slug — métadonnées + corps."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, scope, owner_id, slug, title, description, body_md, "
            "       created_at, updated_at "
            "FROM guides WHERE scope = %s AND owner_id = %s ORDER BY slug",
            (scope, str(owner_id)),
        ).fetchall()
        return [dict(r) for r in rows]


def get_guide_db(scope: str, owner_id: str, slug: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, scope, owner_id, slug, title, description, body_md, "
            "       created_at, updated_at "
            "FROM guides WHERE scope = %s AND owner_id = %s AND slug = %s",
            (scope, str(owner_id), slug),
        ).fetchone()
        return dict(row) if row else None


def set_guide_db(scope: str, owner_id: str, slug: str, body_md: str,
                 title: str = "", description: str = "") -> dict:
    """Crée ou met à jour (upsert par `(scope, owner_id, slug)`) un guide on-demand."""
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO guides (scope, owner_id, slug, title, description, body_md) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (scope, owner_id, slug) DO UPDATE SET "
            "  title = EXCLUDED.title, description = EXCLUDED.description, "
            "  body_md = EXCLUDED.body_md, updated_at = NOW() "
            "RETURNING id, scope, owner_id, slug, title, description, body_md, "
            "          created_at, updated_at",
            (scope, str(owner_id), slug, title, description, body_md),
        ).fetchone()
        return dict(row)


def delete_guide_db(scope: str, owner_id: str, slug: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM guides WHERE scope = %s AND owner_id = %s AND slug = %s",
            (scope, str(owner_id), slug),
        )
        return (cur.rowcount or 0) > 0
