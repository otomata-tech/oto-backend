"""Recherche transverse (lot 3, Ship 1) — le SQL par source de PROSE.

Une requête par source, chacune SCOPÉE par son prédicat d'accès (passé par le
caller `oto_mcp/search.py` — jamais calculé ici). Config FTS `french` (stemming
core PG) + repli d'accents `translate()` (réutilise `projects._fold` — `unaccent`
refusé sur la DB managée), appliqué au DOCUMENT et à la REQUÊTE.

**Source unique index ↔ requête** : les expressions indexées (GIN d'expression,
posées par `_init`) sont les constantes ci-dessous — toute requête utilise
EXACTEMENT la même expression, sinon le planner n'utilise pas l'index.

Surlignage : `ts_headline` sur une 2e tsquery construite de la saisie BRUTE
contre le texte ORIGINAL (accents corrects) ; si le match ne venait que du
folding, le fragment n'a pas de <b> — le caller retombe sur la description/le
début du texte (jamais un texte foldé rendu à l'utilisateur).
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect
from .projects import _fold

# Textes indexés par table (mêmes expressions dans le DDL et le WHERE).
DOCS_TEXT = "coalesce(title,'') || ' ' || coalesce(body_md,'')"
PROJECTS_TEXT = "coalesce(name,'') || ' ' || coalesce(brief_md,'')"
INSTR_TEXT = "coalesce(title,'') || ' ' || coalesce(description,'') || ' ' || coalesce(body_md,'')"
GUIDES_TEXT = "coalesce(title,'') || ' ' || coalesce(description,'') || ' ' || coalesce(body_md,'')"

_HL_OPTS = "MaxWords=30,MinWords=12,ShortWord=2,HighlightAll=false"


def _vec(text_expr: str) -> str:
    return f"to_tsvector('french', {_fold(text_expr)})"


def index_ddl() -> list[str]:
    """DDL des index GIN d'expression (idempotents), consommé par `_init.init_db`.
    `CREATE INDEX` simple (pas CONCURRENTLY : init_db est transactionnel) — tables
    petites, verrou bref."""
    return [
        f"CREATE INDEX IF NOT EXISTS idx_docs_fts ON docs USING GIN ({_vec(DOCS_TEXT)})",
        f"CREATE INDEX IF NOT EXISTS idx_projects_fts ON projects USING GIN ({_vec(PROJECTS_TEXT)})",
        f"CREATE INDEX IF NOT EXISTS idx_org_instructions_fts ON org_instructions USING GIN ({_vec(INSTR_TEXT)})",
        f"CREATE INDEX IF NOT EXISTS idx_guides_fts ON guides USING GIN ({_vec(GUIDES_TEXT)}) "
        "WHERE delivery = 'on-demand'",
    ]


def _prose_query(table: str, text_expr: str, select_cols: str, headline_col: str,
                 where_scope: str, scope_params: tuple, q: str, limit: int) -> list[dict]:
    """Requête générique d'une source de prose : match FTS foldé (l'index), rang
    `ts_rank_cd` length-normalized (|32 — une page géante ne domine ni ne disparaît),
    headline sur la saisie brute contre le texte original."""
    vec = _vec(text_expr)
    fold_q = _fold("%s")
    sql = (
        f"SELECT {select_cols}, "
        f"ts_rank_cd({vec}, websearch_to_tsquery('french', {fold_q}), 32) AS rank, "
        f"ts_headline('french', {headline_col}, websearch_to_tsquery('french', %s), '{_HL_OPTS}') AS headline "
        f"FROM {table} "
        f"WHERE {vec} @@ websearch_to_tsquery('french', {fold_q}) AND ({where_scope}) "
        "ORDER BY rank DESC LIMIT %s"
    )
    with _connect() as conn:
        rows = conn.execute(sql, (q, q, q, *scope_params, limit)).fetchall()
        return [dict(r) for r in rows]


def search_docs_fts(q: str, project_ids: list[int], *, limit: int = 20) -> list[dict]:
    """Pages (docs) des projets accessibles — kind=page."""
    if not project_ids:
        return []
    return _prose_query(
        "docs", DOCS_TEXT,
        "id, project_id, title, updated_at",
        "coalesce(body_md,'')",
        "project_id = ANY(%s)", (project_ids,), q, limit)


def search_project_briefs(q: str, project_ids: list[int], *, limit: int = 20) -> list[dict]:
    """Briefs des projets accessibles — kind=brief (un brief ne remonte que s'il matche)."""
    if not project_ids:
        return []
    return _prose_query(
        "projects", PROJECTS_TEXT,
        "id, name, updated_at",
        "coalesce(brief_md,'')",
        "id = ANY(%s) AND archived_at IS NULL", (project_ids,), q, limit)


def search_procedures_fts(q: str, org_id: int, *, limit: int = 20) -> list[dict]:
    """Procédures ORG-owned de l'org active — kind=procedure. Les procédures d'ÉQUIPE
    sont exclues V1 (écart nommé au plan : `can_read_group` par ligne, plus tard).
    `slug <> 'claude_md'` : reliques du readme pré-convergence 0042 (le readme vit
    dans `guides` — 3 lignes mortes constatées en prod le 17/07, purge à part)."""
    return _prose_query(
        "org_instructions", INSTR_TEXT,
        "slug, title, description, updated_at",
        "coalesce(body_md,'')",
        "owner_type = 'org' AND owner_id = %s AND slug <> 'claude_md'",
        (str(org_id),), q, limit)


def search_guides_fts(q: str, org_id: Optional[int], sub: str, *, limit: int = 20) -> list[dict]:
    """Guides ON-DEMAND lisibles par l'acteur : plateforme (tous) + org active + user.
    Scope 'group' exclu V1 (même écart nommé que les procédures d'équipe)."""
    return _prose_query(
        "guides", GUIDES_TEXT,
        "scope, owner_id, slug, title, description, updated_at",
        "coalesce(body_md,'')",
        "delivery = 'on-demand' AND (scope = 'platform' "
        "OR (scope = 'org' AND owner_id = %s) OR (scope = 'user' AND owner_id = %s))",
        (str(org_id or ""), sub), q, limit)


def search_docs_semantic(query_literal: str, project_ids: list[int], *,
                         limit: int = 20) -> list[dict]:
    """kNN sémantique (lot 3) : pages des projets accessibles les plus PROCHES du
    vecteur de requête (distance cosine `<=>` sur l'index HNSW). Scopé accès comme le
    lexical (mêmes `project_ids`). `query_literal` = littéral halfvec `[...]`."""
    if not project_ids:
        return []
    sql = (
        "SELECT d.id, d.project_id, d.title, d.updated_at, "
        "e.embedding <=> %s::halfvec AS distance "
        "FROM doc_embeddings e JOIN docs d ON d.id = e.doc_id "
        "WHERE d.project_id = ANY(%s) "
        "ORDER BY e.embedding <=> %s::halfvec LIMIT %s"
    )
    with _connect() as conn:
        rows = conn.execute(sql, (query_literal, project_ids, query_literal, limit)).fetchall()
        return [dict(r) for r in rows]


def project_names(ids: list[int]) -> dict[int, str]:
    """Noms d'un lot de projets (étiquette des hits) — une requête."""
    if not ids:
        return {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name FROM projects WHERE id = ANY(%s)", (ids,)).fetchall()
        return {int(r["id"]): r["name"] for r in rows}


def search_files_meta(q: str, project_ids: list[int], *, limit: int = 20) -> list[dict]:
    """Fichiers des projets accessibles — kind=fichier, CONTENEUR : match sur
    `filename + title + description` SEULEMENT (jamais `summary`, colonne morte ;
    pas le binaire — extraction texte = V2). Table minuscule → ILIKE foldé à la
    volée, pas d'index."""
    if not project_ids:
        return []
    text = "coalesce(filename,'') || ' ' || coalesce(title,'') || ' ' || coalesce(description,'')"
    sql = (
        "SELECT id, project_id, filename, title, description, created_at "
        "FROM project_files "
        f"WHERE project_id = ANY(%s) AND {_fold(text)} ILIKE '%%' || {_fold('%s')} || '%%' "
        "ORDER BY created_at DESC LIMIT %s"
    )
    with _connect() as conn:
        rows = conn.execute(sql, (project_ids, q, limit)).fetchall()
        return [dict(r) for r in rows]
