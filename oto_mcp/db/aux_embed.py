"""Embeddings des sources NON-page (oto/#6 C) : briefs de projet + guides on-demand.

Miroir de `doc_embeddings` pour des sources keyées `(kind, ref)` dans `aux_embeddings`.
Le worker `embed_worker` draine `list_dirty_aux` après les docs ; la recherche ajoute
`search_briefs_semantic`/`search_guides_semantic` quand un vecteur de requête est fourni.
Scope guides IDENTIQUE au lexical (`search_guides_fts`) : platform ∪ org active ∪ user.
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect


def list_dirty_aux(limit: int = 16) -> list[dict]:
    """Briefs + guides on-demand à (ré)indexer (`embed_dirty`), forme uniforme
    `{kind, ref, text}`. Ne prend que les sources à TEXTE non trivial (un brief vide
    n'a rien à indexer)."""
    with _connect() as conn:
        briefs = conn.execute(
            "SELECT 'brief' AS kind, id AS ref, "
            "coalesce(name,'') || E'\n' || coalesce(brief_md,'') AS text "
            "FROM projects WHERE embed_dirty AND archived_at IS NULL "
            "AND length(coalesce(brief_md,'')) > 0 ORDER BY id LIMIT %s",
            (limit,)).fetchall()
        guides = conn.execute(
            "SELECT 'guide' AS kind, id AS ref, "
            "coalesce(title,'') || E'\n' || coalesce(description,'') || E'\n' || coalesce(body_md,'') AS text "
            "FROM guides WHERE embed_dirty AND delivery = 'on-demand' "
            "AND length(coalesce(body_md,'')) > 0 ORDER BY id LIMIT %s",
            (limit,)).fetchall()
        return [dict(r) for r in list(briefs) + list(guides)]


def _clear_aux_dirty(conn, kind: str, ref: int) -> None:
    table = "projects" if kind == "brief" else "guides"
    conn.execute(f"UPDATE {table} SET embed_dirty = FALSE WHERE id = %s", (ref,))


def upsert_aux_embedding(kind: str, ref: int, content_sha: str,
                         embedding_literal: str, model: str) -> None:
    """Pose/rafraîchit l'embedding d'un brief/guide ET baisse son dirty, en UNE
    transaction (idempotent, ne perd pas une écriture concurrente)."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO aux_embeddings (kind, ref, content_sha, embedding, model) "
            "VALUES (%s, %s, %s, %s::halfvec, %s) "
            "ON CONFLICT (kind, ref) DO UPDATE SET content_sha = EXCLUDED.content_sha, "
            "embedding = EXCLUDED.embedding, model = EXCLUDED.model, updated_at = NOW()",
            (kind, ref, content_sha, embedding_literal, model))
        _clear_aux_dirty(conn, kind, ref)


def get_aux_embedding_sha(kind: str, ref: int) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT content_sha FROM aux_embeddings WHERE kind = %s AND ref = %s",
            (kind, ref)).fetchone()
        return row["content_sha"] if row else None


def clear_aux_dirty(kind: str, ref: int) -> None:
    with _connect() as conn:
        _clear_aux_dirty(conn, kind, ref)


def search_briefs_semantic(query_literal: str, project_ids: list[int], *,
                           limit: int = 20, max_distance: float = 0.6) -> list[dict]:
    """Briefs proches du sens de la requête, scopés aux projets accessibles (mêmes
    `project_ids` que le lexical `search_project_briefs`)."""
    if not project_ids:
        return []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT p.id, p.name, p.updated_at, "
            "left(p.brief_md, 400) AS body_excerpt, e.embedding <=> %s::halfvec AS distance "
            "FROM aux_embeddings e JOIN projects p ON p.id = e.ref "
            "WHERE e.kind = 'brief' AND p.id = ANY(%s) AND (e.embedding <=> %s::halfvec) < %s "
            "ORDER BY e.embedding <=> %s::halfvec LIMIT %s",
            (query_literal, project_ids, query_literal, max_distance, query_literal, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def search_guides_semantic(query_literal: str, org_id: Optional[int], sub: str, *,
                           limit: int = 20, max_distance: float = 0.6) -> list[dict]:
    """Guides on-demand proches du sens, MÊME scope que le lexical `search_guides_fts` :
    plateforme ∪ org active ∪ user."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT g.scope, g.owner_id, g.slug, g.title, g.description, g.updated_at, "
            "left(g.body_md, 400) AS body_excerpt, e.embedding <=> %s::halfvec AS distance "
            "FROM aux_embeddings e JOIN guides g ON g.id = e.ref "
            "WHERE e.kind = 'guide' AND g.delivery = 'on-demand' "
            "AND (g.scope = 'platform' OR (g.scope = 'org' AND g.owner_id = %s) "
            "     OR (g.scope = 'user' AND g.owner_id = %s)) "
            "AND (e.embedding <=> %s::halfvec) < %s "
            "ORDER BY e.embedding <=> %s::halfvec LIMIT %s",
            (query_literal, str(org_id or ""), sub, query_literal, max_distance,
             query_literal, limit)
        ).fetchall()
        return [dict(r) for r in rows]
