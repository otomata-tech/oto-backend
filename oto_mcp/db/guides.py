"""Guides DB (ADR 0042) : prose par (scope, owner), UNE table pour deux livraisons.

- **`delivery='on-demand'`** : how-to chargé à la demande via `oto_guide`
  (scope `org`|`user` ; platform on-demand = fichiers `guides/*.md`).
- **`delivery='init'`** : readme injecté au handshake (bloc A/C) — le MÊME primitif,
  juste livré d'office. Slug canonique `readme` (org/group/user) ou `secret_sauce`
  (platform). Migré des ex-tables `platform_instructions` / `*_instructions[claude_md]`
  / `user_agent_readme`.

Distinct des PROCÉDURES (`org_instructions`, slots/versioning) — cf. ADR 0042. Les
lectures on-demand filtrent `delivery='on-demand'` pour ne pas exposer les readmes
init dans le catalogue. Ré-exporté par `db/__init__`.
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect

_COLS = ("id, scope, owner_id, slug, title, description, body_md, "
         "delivery, created_at, updated_at")


# --- On-demand (catalogue `oto_guide`) : delivery='on-demand' UNIQUEMENT ------

def list_guides_db(scope: str, owner_id: str) -> list[dict]:
    """Guides ON-DEMAND d'un (scope, owner), triés par slug — métadonnées + corps.
    Exclut les readmes init (delivery='init')."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_COLS} FROM guides "
            "WHERE scope = %s AND owner_id = %s AND delivery = 'on-demand' ORDER BY slug",
            (scope, str(owner_id)),
        ).fetchall()
        return [dict(r) for r in rows]


def get_guide_db(scope: str, owner_id: str, slug: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_COLS} FROM guides "
            "WHERE scope = %s AND owner_id = %s AND slug = %s AND delivery = 'on-demand'",
            (scope, str(owner_id), slug),
        ).fetchone()
        return dict(row) if row else None


def set_guide_db(scope: str, owner_id: str, slug: str, body_md: str,
                 title: str = "", description: str = "") -> dict:
    """Crée ou met à jour (upsert par `(scope, owner_id, slug)`) un guide ON-DEMAND."""
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO guides (scope, owner_id, slug, title, description, body_md, delivery) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'on-demand') "
            "ON CONFLICT (scope, owner_id, slug) DO UPDATE SET "
            "  title = EXCLUDED.title, description = EXCLUDED.description, "
            "  body_md = EXCLUDED.body_md, updated_at = NOW() "
            f"RETURNING {_COLS}",
            (scope, str(owner_id), slug, title, description, body_md),
        ).fetchone()
        return dict(row)


def seed_guide_db(scope: str, owner_id: str, slug: str, body_md: str,
                  title: str = "", description: str = "") -> None:
    """Pose le défaut d'un guide ON-DEMAND s'il n'existe pas (boot, idempotent).
    Ne touche JAMAIS une ligne déjà posée/éditée (les fichiers `guides/*.md` sont
    des seeds, la DB est la source de vérité éditable)."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO guides (scope, owner_id, slug, title, description, body_md, delivery) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'on-demand') "
            "ON CONFLICT (scope, owner_id, slug) DO NOTHING",
            (scope, str(owner_id), slug, title, description, body_md),
        )


def delete_guide_db(scope: str, owner_id: str, slug: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM guides WHERE scope = %s AND owner_id = %s AND slug = %s "
            "AND delivery = 'on-demand'",
            (scope, str(owner_id), slug),
        )
        return (cur.rowcount or 0) > 0


# --- Init (readme injecté au handshake) : delivery='init' UNIQUEMENT ----------

def get_init_guide_db(scope: str, owner_id: str, slug: str) -> Optional[dict]:
    """Le readme INIT d'un (scope, owner, slug), ou None. `{body_md, updated_at, …}`."""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_COLS} FROM guides "
            "WHERE scope = %s AND owner_id = %s AND slug = %s AND delivery = 'init'",
            (scope, str(owner_id), slug),
        ).fetchone()
        return dict(row) if row else None


def set_init_guide_db(scope: str, owner_id: str, slug: str, body_md: str) -> dict:
    """Upsert d'un readme INIT (édition admin/org/user). Corps vide = readme effacé,
    la ligne reste (comme les ex-tables)."""
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO guides (scope, owner_id, slug, body_md, delivery) "
            "VALUES (%s, %s, %s, %s, 'init') "
            "ON CONFLICT (scope, owner_id, slug) DO UPDATE SET "
            "  body_md = EXCLUDED.body_md, updated_at = NOW() "
            f"RETURNING {_COLS}",
            (scope, str(owner_id), slug, body_md or ""),
        ).fetchone()
        return dict(row)


def seed_init_guide_db(scope: str, owner_id: str, slug: str, body_md: str) -> None:
    """Pose le défaut d'un readme INIT s'il n'existe pas (boot, idempotent). Ne touche
    JAMAIS une ligne déjà éditée."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO guides (scope, owner_id, slug, body_md, delivery) "
            "VALUES (%s, %s, %s, %s, 'init') ON CONFLICT (scope, owner_id, slug) DO NOTHING",
            (scope, str(owner_id), slug, body_md or ""),
        )
