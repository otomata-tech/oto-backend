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
                 "is_template, archived_at, created_at, updated_at")


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
                             include_archived: bool = False,
                             templates_only: bool = False) -> list[dict]:
    """Projets possédés par l'un des `(owner_type, owner_id)` (perso + orgs/groupes).
    `templates_only` = ne garder que les modèles publiés (`is_template`, ADR 0032 §7 B5a)."""
    if not owners:
        return []
    otypes = [o[0] for o in owners]
    oids = [o[1] for o in owners]
    sql = (f"SELECT {_PROJECT_COLS} FROM projects p "
           "JOIN unnest(%s::text[], %s::text[]) AS o(t, i) "
           "  ON p.owner_type = o.t AND p.owner_id = o.i "
           "WHERE TRUE ")
    if not include_archived:
        sql += "AND p.archived_at IS NULL "
    if templates_only:
        sql += "AND p.is_template "
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
                   brief_md: Optional[str] = None,
                   is_template: Optional[bool] = None) -> None:
    sets: list[str] = []
    params: list = []
    if name is not None:
        sets.append("name = %s")
        params.append(name)
    if brief_md is not None:
        sets.append("brief_md = %s")
        params.append(brief_md)
    if is_template is not None:
        sets.append("is_template = %s")
        params.append(is_template)
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
               body_md: Optional[str] = None, kind: Optional[str] = None,
               edited_by: Optional[str] = None) -> None:
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
        # Snapshot de l'état ANTÉRIEUR avant d'écrire (chaîne de versions, ADR 0032 §3 B4c).
        prior = conn.execute("SELECT title, body_md FROM docs WHERE id = %s",
                             (doc_id,)).fetchone()
        if prior is not None:
            conn.execute(
                "INSERT INTO doc_revisions (doc_id, title, body_md, edited_by) "
                "VALUES (%s, %s, %s, %s)",
                (doc_id, prior["title"], prior["body_md"], edited_by),
            )
        conn.execute(f"UPDATE docs SET {', '.join(sets)} WHERE id = %s", tuple(params))


def list_doc_revisions(doc_id: int, limit: int = 50) -> list[dict]:
    """Versions antérieures d'un doc, plus récentes d'abord (ADR 0032 §3, B4c)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, title, body_md, edited_by, created_at FROM doc_revisions "
            "WHERE doc_id = %s ORDER BY created_at DESC LIMIT %s",
            (doc_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# --- Demandes de modification (gap #4b, lecture seule → propose, owner tranche) --
_DCR_COLS = ("id, doc_id, requested_by, proposed_title, proposed_body_md, message, "
             "status, resolved_by, resolved_at, created_at")


def add_doc_change_request(doc_id: int, requested_by: Optional[str], *,
                           proposed_title: Optional[str], proposed_body_md: str,
                           message: Optional[str] = None) -> dict:
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO doc_change_requests (doc_id, requested_by, proposed_title, "
            "proposed_body_md, message) VALUES (%s, %s, %s, %s, %s) "
            f"RETURNING {_DCR_COLS}",
            (doc_id, requested_by, proposed_title, proposed_body_md, message),
        ).fetchone()
        return dict(row)


def list_doc_change_requests(doc_id: int, *, only_pending: bool = True) -> list[dict]:
    sql = f"SELECT {_DCR_COLS} FROM doc_change_requests WHERE doc_id = %s "
    if only_pending:
        sql += "AND status = 'pending' "
    sql += "ORDER BY created_at DESC"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, (doc_id,)).fetchall()]


def get_doc_change_request(request_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_DCR_COLS} FROM doc_change_requests WHERE id = %s", (request_id,),
        ).fetchone()
        return dict(row) if row else None


def resolve_doc_change_request(request_id: int, status: str, resolved_by: Optional[str]) -> None:
    """Marque une demande accepted|rejected. L'APPLICATION du contenu (si accepted)
    est faite par l'appelant via `update_doc` (qui snapshotte la version courante)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE doc_change_requests SET status = %s, resolved_by = %s, "
            "resolved_at = NOW() WHERE id = %s AND status = 'pending'",
            (status, resolved_by, request_id),
        )


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


# --- Copie profonde d'un projet (« modèle », ADR 0032 §7 B5a) -----------------
def duplicate_project(src_id: int, new_name: str, owner_type: str, owner_id: str,
                      copied_by: Optional[str] = None) -> int:
    """Copie un projet en un NOUVEAU projet possédé par `(owner_type, owner_id)` :
    brief + arbre des docs (hiérarchie préservée) + liens (label/role/config) +
    fichiers bruts (copie S3, repartis PRIVÉS). La donnée datastore n'est PAS
    dupliquée — les liens `tableau` restent des pointeurs (réutilisation par
    référence, ADR §6) ; l'agent arbitre doublon vs réutilisation. La copie n'est
    jamais un modèle elle-même (`is_template=false` par défaut). Retourne le nouvel id."""
    from .. import media_store

    src = get_project_by_id(src_id)
    if src is None:
        raise ValueError(f"projet source #{src_id} introuvable")

    new_id = create_project(owner_type, owner_id, new_name,
                            brief_md=src.get("brief_md", ""), created_by=copied_by)

    # Arbre des docs : copie niveau par niveau, en remappant parent_id src→cible.
    docs = list_docs_for_project(src_id)
    id_map: dict[int, int] = {}
    remaining = list(docs)
    # Itère jusqu'à ce que chaque doc ait son parent déjà copié (les racines d'abord).
    while remaining:
        progressed = False
        still: list[dict] = []
        for d in remaining:
            parent = d.get("parent_id")
            if parent is None or parent in id_map:
                new_parent = id_map.get(parent) if parent is not None else None
                id_map[d["id"]] = create_doc(
                    new_id, d["title"], parent_id=new_parent,
                    body_md=d.get("body_md", ""), kind=d.get("kind", "doc"),
                    created_by=copied_by)
                progressed = True
            else:
                still.append(d)
        if not progressed:   # cycle/parent orphelin (ne devrait pas arriver) : rattache à la racine
            for d in still:
                id_map[d["id"]] = create_doc(
                    new_id, d["title"], parent_id=None,
                    body_md=d.get("body_md", ""), kind=d.get("kind", "doc"),
                    created_by=copied_by)
            break
        remaining = still

    # Liens typés : label + role + config préservés (le « pourquoi » suit l'entité).
    for link in list_project_links(src_id):
        add_project_link(new_id, link["target_type"], link["target_ref"],
                         label=link.get("label"), role=link.get("role"),
                         config=link.get("config") or None)

    # Fichiers bruts : copie S3 server-side, la copie repart privée (public=false).
    for f in list_project_files(src_id):
        try:
            new_key = media_store.copy_object(f["s3_key"], "project-files", str(new_id))
        except Exception:
            logger.warning("duplicate_project: copie S3 échouée (file=%s)", f.get("id"))
            continue
        add_project_file(new_id, new_key, f["filename"], mime=f.get("mime"),
                         size_bytes=f.get("size_bytes"), title=f.get("title"),
                         description=f.get("description"), created_by=copied_by)

    log_project_activity(new_id, copied_by, "project.copy", f"from #{src_id}")
    return new_id
