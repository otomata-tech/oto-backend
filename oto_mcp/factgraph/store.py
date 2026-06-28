"""Store des records typés — write-model générique (ADR 0008, amendé ADR 0029).

Schéma PG dédié `factgraph` :
- `workspace` : une instance de cas d'usage, scopée org (org × domaine).
- `fact`      : les records typés (un `kind` + un payload JSONB *validé* contre le registre).

⚠️ ADR 0029 : l'ambition « graphe » est retirée — **pas d'arêtes** (la table `edge`
et les fonctions `link`/`incoming` ont été supprimées : aucune résolution d'entité
en oto, la clé SIREN suffit). Ne reste que le **record typé**, rendu en fiches.

Branché sur le pool psycopg existant (`db._connect`) ; rows = dicts (`_str_dict_row`).
"""

from __future__ import annotations

from typing import Optional

import psycopg
from psycopg.types.json import Json

from .. import db
from .schemas import validate_fact

_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS factgraph;

CREATE TABLE IF NOT EXISTS factgraph.workspace (
  id             BIGSERIAL PRIMARY KEY,
  org_id         BIGINT NOT NULL,
  kind           TEXT NOT NULL,          -- cas d'usage : 'prospection' | 'compta' | ...
  label          TEXT,
  doctrine       TEXT,                   -- oto_get_doctrine() per-workspace (à câbler)
  scoring_config JSONB,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (org_id, kind)
);

CREATE TABLE IF NOT EXISTS factgraph.fact (
  id           BIGSERIAL PRIMARY KEY,
  workspace_id BIGINT NOT NULL REFERENCES factgraph.workspace(id) ON DELETE CASCADE,
  kind         TEXT NOT NULL,
  data         JSONB NOT NULL,           -- payload validé contre le schéma du kind
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_by   TEXT NOT NULL DEFAULT 'system'
);
CREATE INDEX IF NOT EXISTS fact_ws_kind_idx ON factgraph.fact (workspace_id, kind);
CREATE INDEX IF NOT EXISTS fact_data_gin_idx ON factgraph.fact USING gin (data jsonb_path_ops);
CREATE INDEX IF NOT EXISTS fact_siren_idx
  ON factgraph.fact ((data->>'siren')) WHERE data ? 'siren';
"""


def init_schema(conn: psycopg.Connection) -> None:
    """Crée le schéma factgraph (idempotent). Appelé depuis db.init_db."""
    conn.execute(_SCHEMA)


# ── workspaces ───────────────────────────────────────────────────────────────
def get_or_create_workspace(org_id: int, kind: str, label: Optional[str] = None) -> int:
    with db._connect() as conn:
        row = conn.execute(
            """
            INSERT INTO factgraph.workspace (org_id, kind, label)
            VALUES (%s, %s, %s)
            ON CONFLICT (org_id, kind) DO UPDATE SET label = COALESCE(EXCLUDED.label, factgraph.workspace.label)
            RETURNING id
            """,
            (org_id, kind, label),
        ).fetchone()
        return row["id"]


# ── écriture ─────────────────────────────────────────────────────────────────
def add_fact(workspace_id: int, kind: str, data: dict, created_by: str = "system") -> int:
    clean = validate_fact(kind, data)              # ← garde-fou « structuré »
    with db._connect() as conn:
        row = conn.execute(
            "INSERT INTO factgraph.fact (workspace_id, kind, data, created_by) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (workspace_id, kind, Json(clean), created_by),
        ).fetchone()
        return row["id"]


# ── lecture ──────────────────────────────────────────────────────────────────
def _get(conn: psycopg.Connection, fact_id: int) -> dict:
    r = conn.execute(
        "SELECT id, workspace_id, kind, data, created_at FROM factgraph.fact WHERE id = %s",
        (fact_id,),
    ).fetchone()
    if r is None:
        raise KeyError(f"fact {fact_id} introuvable")
    return r


def get_fact(fact_id: int) -> dict:
    with db._connect() as conn:
        return _get(conn, fact_id)


def find(workspace_id: int, kind: str) -> list[dict]:
    with db._connect() as conn:
        return conn.execute(
            "SELECT id, workspace_id, kind, data, created_at FROM factgraph.fact "
            "WHERE workspace_id = %s AND kind = %s ORDER BY id",
            (workspace_id, kind),
        ).fetchall()


# ── accès générique org-scopé (anti-IDOR par JOIN sur workspace.org_id) ──────
def list_facts_for_org(org_id: int, domain: str, kind: str, limit: int = 200) -> list[dict]:
    """Facts d'un `kind` dans le workspace (org × domaine). [] si pas de workspace."""
    with db._connect() as conn:
        return conn.execute(
            "SELECT f.id, f.workspace_id, f.kind, f.data, f.created_at, f.created_by "
            "FROM factgraph.fact f JOIN factgraph.workspace w ON w.id = f.workspace_id "
            "WHERE w.org_id = %s AND w.kind = %s AND f.kind = %s "
            "ORDER BY f.id DESC LIMIT %s",
            (org_id, domain, kind, limit),
        ).fetchall()


def get_fact_for_org(org_id: int, fact_id: int) -> Optional[dict]:
    """Un fact SI son workspace appartient à `org_id`, sinon None (verrou IDOR)."""
    with db._connect() as conn:
        return conn.execute(
            "SELECT f.id, f.workspace_id, f.kind, f.data, f.created_at, f.created_by "
            "FROM factgraph.fact f JOIN factgraph.workspace w ON w.id = f.workspace_id "
            "WHERE f.id = %s AND w.org_id = %s",
            (fact_id, org_id),
        ).fetchone()


def update_fact(fact_id: int, kind: str, data: dict) -> dict:
    """Remplace le payload d'un fact (re-validé contre le schéma du kind).
    Le scope org est garanti par l'appelant (get_fact_for_org en amont)."""
    clean = validate_fact(kind, data)
    with db._connect() as conn:
        conn.execute(
            "UPDATE factgraph.fact SET data = %s WHERE id = %s",
            (Json(clean), fact_id),
        )
    return clean
