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
from .datastore import (
    create_datastore_namespace,
    datastore_insert_row,
    datastore_list_rows,
    get_datastore_namespace_by_id,
    set_datastore_schema,
)
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


def list_projects_granted_to(principals: list[tuple[str, str]]) -> list[dict]:
    """Projets PARTAGÉS aux principals donnés (`resource_grants`, ADR 0030) — la
    lentille « livré à mon org / à moi » (#52). Chaque row porte en plus la
    `permission` du meilleur grant. Exclut les archivés."""
    if not principals:
        return []
    ptypes = [p[0] for p in principals]
    pids = [p[1] for p in principals]
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {', '.join('p.' + c.strip() for c in _PROJECT_COLS.split(','))}, "
            "       MAX(g.permission) AS permission "
            "FROM resource_grants g "
            "JOIN projects p ON p.id = g.resource_id::bigint "
            "JOIN unnest(%s::text[], %s::text[]) AS pr(t, i) "
            "  ON g.principal_type = pr.t AND g.principal_id = pr.i "
            "WHERE g.resource_type = 'project' AND p.archived_at IS NULL "
            f"GROUP BY {', '.join('p.' + c.strip() for c in _PROJECT_COLS.split(','))} "
            "ORDER BY p.updated_at DESC",
            (ptypes, pids),
        ).fetchall()
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
                     config: Optional[dict] = None, identity_ref: Optional[str] = None) -> None:
    """Lie une entité (tableau/procédure/connecteur/base) au projet. `identity_ref`
    (ADR 0032 §4 amendé, #57) = un BINDING distinct par identité — NULL = binding par
    défaut (un connecteur peut être lié N fois, une identité par binding). Idempotent
    par binding : re-lier met à jour le label ; `role`/`config` (surcharge préfaite)
    ne sont écrasés que s'ils sont fournis. `config` absent à la création → `{}`."""
    cfg = json.dumps(config) if config is not None else None
    with _connect() as conn:
        conn.execute(
            "INSERT INTO project_links (project_id, target_type, target_ref, identity_ref, label, role, config) "
            "VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s::jsonb, '{}'::jsonb)) "
            "ON CONFLICT (project_id, target_type, target_ref, identity_ref) DO UPDATE SET "
            "label = EXCLUDED.label, role = COALESCE(EXCLUDED.role, project_links.role), "
            "config = COALESCE(%s::jsonb, project_links.config)",
            (project_id, target_type, target_ref, identity_ref, label, role, cfg, cfg),
        )
        conn.execute("UPDATE projects SET updated_at = NOW() WHERE id = %s", (project_id,))


def update_project_link_ref(project_id: int, target_type: str,
                            old_ref: str, new_ref: str) -> int:
    """Re-pointe un lien vers une autre entité (même type). Sert la cascade de
    livraison (#52) : une procédure COPIÉE dans l'org cible re-pointe le lien sur
    la copie. Renvoie le nb de bindings re-pointés."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE project_links SET target_ref = %s "
            "WHERE project_id = %s AND target_type = %s AND target_ref = %s",
            (new_ref, project_id, target_type, old_ref),
        )
        return cur.rowcount


def remove_project_link(project_id: int, target_type: str, target_ref: str,
                        identity_ref: Optional[str] = None) -> int:
    """Délie (ADR 0032 §4 amendé, #57). `identity_ref` fourni → CE binding précis ;
    `identity_ref` None → **tous** les bindings de l'entité (délier « le connecteur »
    entièrement, quel que soit le nombre d'identités)."""
    where = "project_id = %s AND target_type = %s AND target_ref = %s"
    params: list = [project_id, target_type, target_ref]
    if identity_ref is not None:
        where += " AND identity_ref IS NOT DISTINCT FROM %s"
        params.append(identity_ref)
    with _connect() as conn:
        cur = conn.execute(f"DELETE FROM project_links WHERE {where}", params)
        return cur.rowcount


def _apply_tableau_names(links: list[dict], name_by_id: dict[int, str]) -> None:
    """Attache le NOM du namespace à chaque lien `tableau` (résolu depuis l'id porté par
    `target_ref`). Pur (mutation en place), testable sans DB. Un ref non numérique / un
    namespace disparu → pas de clé `namespace` (le lien reste, best-effort)."""
    for l in links:
        if l.get("target_type") == "tableau" and str(l.get("target_ref", "")).isdigit():
            nm = name_by_id.get(int(l["target_ref"]))
            if nm is not None:
                l["namespace"] = nm


def list_project_links(project_id: int) -> list[dict]:
    """Liens du projet, avec `role` et `cross_project` DÉRIVÉ (ADR 0032 §2) : True si
    le même (target_type, target_ref) est lié par un AUTRE projet → l'agent sait qu'une
    modif de l'entité retombe ailleurs (s'abstenir d'un changement brutal / demander).
    Les liens `tableau` sont enrichis du **nom** de leur namespace (`namespace`) : l'agent
    adresse « le tableau de ce projet » (par rôle/label) → nom réel pour `data_*`, sans
    nom en dur (ADR 0032 §6, adressage par rôle après provisioning template→instance)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT pl.target_type, pl.target_ref, pl.identity_ref, pl.label, pl.role, pl.config, pl.created_at, "
            "       EXISTS(SELECT 1 FROM project_links o "
            "              WHERE o.target_type = pl.target_type "
            "                AND o.target_ref = pl.target_ref "
            "                AND o.project_id <> pl.project_id) AS cross_project "
            "FROM project_links pl WHERE pl.project_id = %s "
            "ORDER BY pl.target_type, pl.label NULLS LAST, pl.target_ref, pl.identity_ref NULLS FIRST",
            (project_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # Mirror back-compat (B3) : l'identité vit dans `identity_ref` (source de vérité),
            # mais les lecteurs legacy (front actuel) lisent encore config.identity_id → on le
            # re-dérive. La résolution (project_pinned_identity) lit identity_ref directement.
            if d.get("target_type") == "connecteur" and d.get("identity_ref"):
                d["config"] = {**(d.get("config") or {}), "identity_id": d["identity_ref"]}
            out.append(d)
        # Résolution des noms de namespace des tableaux, en UNE requête (même connexion).
        ids = [int(l["target_ref"]) for l in out
               if l.get("target_type") == "tableau" and str(l.get("target_ref", "")).isdigit()]
        if ids:
            nrows = conn.execute(
                "SELECT id, namespace FROM user_datastores WHERE id = ANY(%s)", (ids,),
            ).fetchall()
            _apply_tableau_names(out, {r["id"]: r["namespace"] for r in nrows})
        return out


# --- Docs (pages markdown arborescentes d'un projet, incrément 3) -------------
_DOC_COLS = ("id, project_id, parent_id, title, body_md, kind, public_token, "
             "created_by, created_at, updated_at")


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


def set_doc_public(doc_id: int, public: bool) -> Optional[str]:
    """Active/retire le partage public d'un doc (gap #4a). Renvoie le `public_token`
    (généré à l'activation, conservé si déjà public ; None si retiré)."""
    import secrets as _secrets
    with _connect() as conn:
        if not public:
            conn.execute("UPDATE docs SET public_token = NULL WHERE id = %s", (doc_id,))
            return None
        cur = conn.execute("SELECT public_token FROM docs WHERE id = %s", (doc_id,)).fetchone()
        token = (cur or {}).get("public_token") or _secrets.token_urlsafe(16)
        conn.execute("UPDATE docs SET public_token = %s WHERE id = %s", (token, doc_id))
        return token


def get_doc_by_public_token(token: str) -> Optional[dict]:
    """Lecture publique d'un doc par son token (gap #4a) — title/body_md/updated_at."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT title, body_md, updated_at FROM docs WHERE public_token = %s",
            (token,),
        ).fetchone()
        return dict(row) if row else None


# --- Partage PUBLIC CHIFFRÉ d'un projet (zero-knowledge, ADR 0032 §3) ---------
# Le serveur ne voit QUE le ciphertext (snapshot brief+pages chiffré côté navigateur
# en AES-256-GCM) ; la clé vit dans le fragment de l'URL et n'atteint jamais le
# backend → « encrypted » au sens fort. Une part par projet (upsert sur project_id).

def set_project_public_share(project_id: int, ciphertext: str,
                             created_by: Optional[str] = None) -> str:
    """Publie (ou re-publie) le snapshot chiffré d'un projet. Renvoie le `token`
    public. Upsert par projet : re-publier remplace le ciphertext ET fait TOURNER
    le token (l'ancien lien devient caduc — cohérent avec la rotation de clé côté
    navigateur à chaque publication zero-knowledge)."""
    token = secrets.token_urlsafe(16)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO project_public_shares (token, project_id, ciphertext, created_by) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (project_id) DO UPDATE SET token = EXCLUDED.token, "
            "ciphertext = EXCLUDED.ciphertext, created_by = EXCLUDED.created_by, "
            "updated_at = NOW()",
            (token, project_id, ciphertext, created_by),
        )
    return token


def clear_project_public_share(project_id: int) -> None:
    """Retire le partage public d'un projet (le lien devient introuvable)."""
    with _connect() as conn:
        conn.execute("DELETE FROM project_public_shares WHERE project_id = %s", (project_id,))


def get_project_public_share(project_id: int) -> Optional[dict]:
    """Méta de la part publique d'un projet pour le propriétaire (token + horodatage,
    JAMAIS la clé — elle n'est pas côté serveur). None si non partagé."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT token, updated_at FROM project_public_shares WHERE project_id = %s",
            (project_id,),
        ).fetchone()
        return dict(row) if row else None


def get_project_share_by_token(token: str) -> Optional[dict]:
    """Lecture PUBLIQUE (sans auth) du ciphertext par token. Le déchiffrement se fait
    côté navigateur avec la clé du fragment — le serveur ne peut pas lire le contenu."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT ciphertext, updated_at FROM project_public_shares WHERE token = %s",
            (token,),
        ).fetchone()
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
def _provision_tableau(owner_type: str, owner_id: str, src_ref: str, *,
                       seed: bool) -> Optional[str]:
    """Matérialise un namespace datastore FRAIS pour l'instance de projet (ADR 0032 §6,
    amendement 2026-07-01) : nouveau namespace possédé par `(owner_type, owner_id)`, même
    **schéma** que la source (le vivier repart isolé), nom dérivé du nom source rendu unique.
    `seed=True` copie aussi les **rows** d'amorce (mode `seeded`) ; sinon vivier vide (`empty`).
    Retourne le `target_ref` du nouveau namespace (son id en str), ou `None` si la source est
    introuvable / le ref malformé → l'appelant garde le pointeur d'origine (dégradation sûre)."""
    try:
        src_ns_id = int(src_ref)
    except (TypeError, ValueError):
        return None
    src_ns = get_datastore_namespace_by_id(src_ns_id)
    if src_ns is None:
        return None
    base = src_ns["namespace"]
    new_id: Optional[int] = None
    candidate = base
    for i in range(1, 100):   # dérive un nom unique chez le nouveau propriétaire
        try:
            new_id = create_datastore_namespace(owner_type, owner_id, candidate)
            break
        except ValueError:
            candidate = f"{base}-{i}"
    if new_id is None:
        return None
    if src_ns.get("schema"):
        set_datastore_schema(new_id, src_ns["schema"])
    if seed:
        for r in datastore_list_rows(src_ns_id, limit=None):
            datastore_insert_row(new_id, r["row_id"], r.get("data") or {})
    return str(new_id)


def duplicate_project(src_id: int, new_name: str, owner_type: str, owner_id: str,
                      copied_by: Optional[str] = None) -> int:
    """Copie un projet en un NOUVEAU projet possédé par `(owner_type, owner_id)` :
    brief + arbre des docs (hiérarchie préservée) + liens (label/role/config) +
    fichiers bruts (copie S3, repartis PRIVÉS). Un lien `tableau` est par défaut un
    **pointeur** vers le même namespace (réutilisation par référence, `config.provision`
    absent/`shared`) ; en mode **`empty`/`seeded`** (ADR §6) il est **provisionné** — un
    namespace FRAIS (même schéma, rows optionnelles) pour que chaque instance ait son
    vivier isolé. La copie n'est jamais un modèle elle-même (`is_template=false` par
    défaut). Retourne le nouvel id."""
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
    # Un lien `tableau` en mode « provisionné » (config.provision ∈ {empty, seeded}, ADR
    # 0032 §6) NE recopie PAS le pointeur : il matérialise un namespace FRAIS (même schéma,
    # rows si seeded) possédé par la copie, et le lien pointe dessus → vivier isolé par
    # instance. `config.provision` reste sur le lien copié : re-copier re-provisionne.
    for link in list_project_links(src_id):
        target_ref = link["target_ref"]
        if link["target_type"] == "tableau":
            mode = (link.get("config") or {}).get("provision")
            if mode in ("empty", "seeded"):
                target_ref = _provision_tableau(
                    owner_type, owner_id, target_ref, seed=(mode == "seeded")
                ) or target_ref
        add_project_link(new_id, link["target_type"], target_ref,
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
