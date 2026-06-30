"""Projets (ADR 0030/0032) : conteneur, liens typés, docs arborescents, fichiers bruts, activité.

Extrait de l'ex-monolithe `db.py` (barreau final). Fonctions de domaine — la
plomberie est dans `_conn`. Ré-exporté par `db/__init__`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
from datetime import date, datetime, timezone
from typing import Any, Iterator, Optional

import psycopg

logger = logging.getLogger(__name__)

from ._conn import _connect
from .users import upsert_user


# --- Projets (couche d'organisation, owned resource ADR 0030) ----------------
_PROJECT_COLS = ("id, owner_type, owner_id, name, brief_md, created_by, "
                 "archived_at, created_at, updated_at")


def create_project(owner_type: str, owner_id: str, name: str,
                   brief_md: str = "", created_by: Optional[str] = None) -> int:
    """Crée un projet possédé par `(owner_type, owner_id)` (ADR 0030). owner_id = sub
    (perso) | org.id::text | group.id::text."""
    if owner_type == "user":
        upsert_user(owner_id)
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO projects (owner_type, owner_id, name, brief_md, created_by) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (owner_type, owner_id, name, brief_md, created_by),
        ).fetchone()
        return int(row["id"])


def get_project_by_id(project_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_PROJECT_COLS} FROM projects WHERE id = %s", (project_id,),
        ).fetchone()
        return dict(row) if row else None


def list_projects_for_owners(owners: list[tuple[str, str]], *,
                             include_archived: bool = False) -> list[dict]:
    """Projets possédés par l'un des `(owner_type, owner_id)` (perso + orgs/groupes)."""
    if not owners:
        return []
    otypes = [o[0] for o in owners]
    oids = [o[1] for o in owners]
    sql = (f"SELECT {_PROJECT_COLS} FROM projects p "
           "JOIN unnest(%s::text[], %s::text[]) AS o(t, i) "
           "  ON p.owner_type = o.t AND p.owner_id = o.i ")
    if not include_archived:
        sql += "WHERE p.archived_at IS NULL "
    sql += "ORDER BY p.updated_at DESC"
    with _connect() as conn:
        rows = conn.execute(sql, (otypes, oids)).fetchall()
        return [dict(r) for r in rows]


def list_all_projects(*, include_archived: bool = False) -> list[dict]:
    """Tous les projets (vue opérateur plateforme — gouvernance, pas de contenu)."""
    sql = f"SELECT {_PROJECT_COLS} FROM projects "
    if not include_archived:
        sql += "WHERE archived_at IS NULL "
    sql += "ORDER BY updated_at DESC"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def update_project(project_id: int, *, name: Optional[str] = None,
                   brief_md: Optional[str] = None) -> None:
    sets: list[str] = []
    params: list = []
    if name is not None:
        sets.append("name = %s")
        params.append(name)
    if brief_md is not None:
        sets.append("brief_md = %s")
        params.append(brief_md)
    if not sets:
        return
    sets.append("updated_at = NOW()")
    params.append(project_id)
    with _connect() as conn:
        conn.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id = %s", tuple(params))


def archive_project(project_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE projects SET archived_at = NOW(), updated_at = NOW() "
                     "WHERE id = %s", (project_id,))


def reparent_project(project_id: int, new_owner_type: str, new_owner_id: str) -> None:
    if new_owner_type == "user":
        upsert_user(new_owner_id)
    with _connect() as conn:
        conn.execute("UPDATE projects SET owner_type = %s, owner_id = %s, updated_at = NOW() "
                     "WHERE id = %s", (new_owner_type, new_owner_id, project_id))


def add_project_link(project_id: int, target_type: str, target_ref: str,
                     label: Optional[str] = None, role: Optional[str] = None,
                     config: Optional[dict] = None) -> None:
    """Lie une entité (tableau/procédure/connecteur/base) au projet. Idempotent :
    re-lier met à jour le label ; le `role` et le `config` (surcharge contextuelle
    préfaite, ADR 0032 §4) ne sont écrasés que s'ils sont fournis (un re-link pour
    changer le seul label ne perd ni la description de rôle ni la config déjà posées).
    `config` absent à la création → `{}`."""
    cfg = json.dumps(config) if config is not None else None
    with _connect() as conn:
        conn.execute(
            "INSERT INTO project_links (project_id, target_type, target_ref, label, role, config) "
            "VALUES (%s, %s, %s, %s, %s, COALESCE(%s::jsonb, '{}'::jsonb)) "
            "ON CONFLICT (project_id, target_type, target_ref) DO UPDATE SET "
            "label = EXCLUDED.label, role = COALESCE(EXCLUDED.role, project_links.role), "
            "config = COALESCE(%s::jsonb, project_links.config)",
            (project_id, target_type, target_ref, label, role, cfg, cfg),
        )
        conn.execute("UPDATE projects SET updated_at = NOW() WHERE id = %s", (project_id,))


def remove_project_link(project_id: int, target_type: str, target_ref: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM project_links WHERE project_id = %s AND target_type = %s AND target_ref = %s",
            (project_id, target_type, target_ref),
        )
        return cur.rowcount


def list_project_links(project_id: int) -> list[dict]:
    """Liens du projet, avec `role` et `cross_project` DÉRIVÉ (ADR 0032 §2) : True si
    le même (target_type, target_ref) est lié par un AUTRE projet → l'agent sait qu'une
    modif de l'entité retombe ailleurs (s'abstenir d'un changement brutal / demander)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT pl.target_type, pl.target_ref, pl.label, pl.role, pl.config, pl.created_at, "
            "       EXISTS(SELECT 1 FROM project_links o "
            "              WHERE o.target_type = pl.target_type "
            "                AND o.target_ref = pl.target_ref "
            "                AND o.project_id <> pl.project_id) AS cross_project "
            "FROM project_links pl WHERE pl.project_id = %s "
            "ORDER BY pl.target_type, pl.label NULLS LAST, pl.target_ref",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# --- Docs (pages markdown arborescentes d'un projet, incrément 3) -------------
_DOC_COLS = ("id, project_id, parent_id, title, body_md, kind, created_by, "
             "created_at, updated_at")


def create_doc(project_id: int, title: str, *, parent_id: Optional[int] = None,
               body_md: str = "", kind: str = "doc", created_by: Optional[str] = None) -> int:
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO docs (project_id, parent_id, title, body_md, kind, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (project_id, parent_id, title, body_md, kind, created_by),
        ).fetchone()
        conn.execute("UPDATE projects SET updated_at = NOW() WHERE id = %s", (project_id,))
        return int(row["id"])


def get_doc_by_id(doc_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(f"SELECT {_DOC_COLS} FROM docs WHERE id = %s", (doc_id,)).fetchone()
        return dict(row) if row else None


def list_docs_for_project(project_id: int) -> list[dict]:
    """Toutes les pages du projet (l'UI/agent reconstruit l'arbre via parent_id)."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_DOC_COLS} FROM docs WHERE project_id = %s "
            "ORDER BY parent_id NULLS FIRST, title", (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def update_doc(doc_id: int, *, title: Optional[str] = None,
               body_md: Optional[str] = None, kind: Optional[str] = None) -> None:
    sets: list[str] = []
    params: list = []
    if title is not None:
        sets.append("title = %s")
        params.append(title)
    if body_md is not None:
        sets.append("body_md = %s")
        params.append(body_md)
    if kind is not None:
        sets.append("kind = %s")
        params.append(kind)
    if not sets:
        return
    sets.append("updated_at = NOW()")
    params.append(doc_id)
    with _connect() as conn:
        conn.execute(f"UPDATE docs SET {', '.join(sets)} WHERE id = %s", tuple(params))


def delete_doc(doc_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM docs WHERE id = %s", (doc_id,))


def move_doc(doc_id: int, new_parent_id: Optional[int]) -> None:
    with _connect() as conn:
        conn.execute("UPDATE docs SET parent_id = %s, updated_at = NOW() WHERE id = %s",
                     (new_parent_id, doc_id))


# --- Journal d'activité du projet (incrément 5) ------------------------------
def log_project_activity(project_id: int, sub: Optional[str], action: str,
                         detail: Optional[str] = None) -> None:
    """Best-effort : ne jamais faire échouer la mutation principale sur un log raté."""
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO project_activity (project_id, sub, action, detail) "
                "VALUES (%s, %s, %s, %s)", (project_id, sub, action, detail),
            )
    except Exception:
        logger.warning("log_project_activity échoué (project=%s action=%s)", project_id, action)


def list_project_activity(project_id: int, limit: int = 50) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT sub, action, detail, created_at FROM project_activity "
            "WHERE project_id = %s ORDER BY created_at DESC LIMIT %s",
            (project_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# --- Fichiers bruts d'un projet (carte « Autre document », ADR 0032 §3) -------
_PFILE_COLS = ("id, project_id, s3_key, filename, mime, size_bytes, title, "
               "description, summary, public, public_url, created_by, created_at")


def add_project_file(project_id: int, s3_key: str, filename: str, *,
                     mime: Optional[str] = None, size_bytes: Optional[int] = None,
                     title: Optional[str] = None, description: Optional[str] = None,
                     created_by: Optional[str] = None) -> dict:
    """Enregistre un fichier brut (déjà uploadé en S3) attaché au projet. Le blob
    durable vit dans Object Storage (`s3_key`) ; cette ligne porte sa métadonnée."""
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO project_files (project_id, s3_key, filename, mime, "
            "size_bytes, title, description, created_by) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_PFILE_COLS}",
            (project_id, s3_key, filename, mime, size_bytes, title, description, created_by),
        ).fetchone()
        conn.execute("UPDATE projects SET updated_at = NOW() WHERE id = %s", (project_id,))
        return dict(row)


def list_project_files(project_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_PFILE_COLS} FROM project_files WHERE project_id = %s "
            "ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_project_file(file_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_PFILE_COLS} FROM project_files WHERE id = %s", (file_id,),
        ).fetchone()
        return dict(row) if row else None


def delete_project_file(file_id: int) -> Optional[dict]:
    """Supprime la ligne et renvoie la `s3_key` à purger (ou None si inconnu)."""
    with _connect() as conn:
        row = conn.execute(
            "DELETE FROM project_files WHERE id = %s RETURNING project_id, s3_key",
            (file_id,),
        ).fetchone()
        return dict(row) if row else None


def set_project_file_public(file_id: int, public: bool,
                            public_url: Optional[str]) -> Optional[dict]:
    """Bascule l'état public d'un fichier (ADR 0032 §3, B4b) ; renvoie la ligne à jour."""
    with _connect() as conn:
        row = conn.execute(
            f"UPDATE project_files SET public = %s, public_url = %s WHERE id = %s "
            f"RETURNING {_PFILE_COLS}",
            (public, public_url, file_id),
        ).fetchone()
        return dict(row) if row else None
