"""Store du graphe de facts — write-model générique (ADR 0008).

Schéma PG dédié `factgraph` :
- `workspace` : une instance de cas d'usage, scopée org (org × kind de harnais).
- `fact`      : les nœuds (un `kind` + un payload JSONB *validé* contre le registre).
- `edge`      : les arêtes dirigées typées (`role`).

Tout le métier (statut, contacts, historique…) se lit en parcourant le graphe ;
le read-model typé (file priorisée, scoring) vit dans `projection.py`.

Branché sur le pool psycopg existant (`db._connect`) ; rows = dicts (`_str_dict_row`).
"""

from __future__ import annotations

from typing import Optional

import psycopg
from psycopg.types.json import Json

from .. import db
from .schemas import validate_edge, validate_fact

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

CREATE TABLE IF NOT EXISTS factgraph.edge (
  src_id   BIGINT NOT NULL REFERENCES factgraph.fact(id) ON DELETE CASCADE,
  dst_id   BIGINT NOT NULL REFERENCES factgraph.fact(id) ON DELETE CASCADE,
  role     TEXT NOT NULL,
  PRIMARY KEY (src_id, dst_id, role),
  CHECK (src_id <> dst_id)
);
CREATE INDEX IF NOT EXISTS edge_dst_idx ON factgraph.edge (dst_id);
CREATE INDEX IF NOT EXISTS edge_role_idx ON factgraph.edge (role);
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


def link(src_id: int, dst_id: int, role: str) -> None:
    with db._connect() as conn:
        src = _get(conn, src_id)
        dst = _get(conn, dst_id)
        if src["workspace_id"] != dst["workspace_id"]:
            raise ValueError("arête inter-workspace interdite")
        validate_edge(role, src["kind"], dst["kind"])   # ← arête typée
        conn.execute(
            "INSERT INTO factgraph.edge (src_id, dst_id, role) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (src_id, dst_id, role),
        )


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


def incoming(dst_id: int, role: Optional[str] = None) -> list[dict]:
    """Facts pointant VERS dst_id (ex : contacts/actions qui concernent une entreprise).
    Chaque dict porte une clé `role` en plus des colonnes du fact."""
    sql = (
        "SELECT f.id, f.workspace_id, f.kind, f.data, f.created_at, e.role "
        "FROM factgraph.edge e JOIN factgraph.fact f ON f.id = e.src_id "
        "WHERE e.dst_id = %s"
    )
    params: list = [dst_id]
    if role:
        sql += " AND e.role = %s"
        params.append(role)
    sql += " ORDER BY f.id"
    with db._connect() as conn:
        return conn.execute(sql, params).fetchall()


def find(workspace_id: int, kind: str) -> list[dict]:
    with db._connect() as conn:
        return conn.execute(
            "SELECT id, workspace_id, kind, data, created_at FROM factgraph.fact "
            "WHERE workspace_id = %s AND kind = %s ORDER BY id",
            (workspace_id, kind),
        ).fetchall()
