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

import logging
import re
from typing import Optional

from ._conn import _connect
from .projects import _fold

logger = logging.getLogger(__name__)

# Textes indexés par table (mêmes expressions dans le DDL et le WHERE).
DOCS_TEXT = "coalesce(title,'') || ' ' || coalesce(body_md,'')"
PROJECTS_TEXT = "coalesce(name,'') || ' ' || coalesce(brief_md,'')"
INSTR_TEXT = "coalesce(title,'') || ' ' || coalesce(description,'') || ' ' || coalesce(body_md,'')"
GUIDES_TEXT = "coalesce(title,'') || ' ' || coalesce(description,'') || ' ' || coalesce(body_md,'')"
# Couches de contexte (surface `oto_guide`) — des NŒUDS depuis le lot M1 (blueprint
# ADR 0054/0063) : `nodes`, la prose dans `props`. MÊME texte indexé qu'au temps de
# la table `guides` (titre + chapô + corps), à la lecture JSONB près — le
# comportement de recherche est identique, seule la table lue change (#282).
# `->>` sur jsonb (`jsonb_object_field_text`) est IMMUTABLE → indexable en
# expression, comme `data::text` juste dessous.
NODES_TEXT = ("coalesce(props->>'title','') || ' ' || coalesce(props->>'description','') "
              "|| ' ' || coalesce(props->>'body_md','')")
# Lignes de datastore (#67 V2.1) : le JSONB entier rendu en texte. `data::text` est
# IMMUTABLE (jsonb_out) → indexable en expression, comme les autres sources ; même
# config `french` + repli d'accents. Grain grossier assumé (matche clés + valeurs),
# raffinable plus tard par colonne dérivée si besoin.
DATASTORE_ROWS_TEXT = "data::text"
# Texte EXTRAIT des fichiers déposés (#298). L'expression ne porte QUE la colonne de
# `project_file_texts` : le nom du fichier vit dans `project_files`, et un index
# d'expression ne peut pas couvrir une jointure — c'est ce qui fait du contenu une
# source distincte du nom, réunie par le RRF plutôt que par un `||`.
FILE_TEXT = "coalesce(extracted_text,'')"


# ── Vecteurs de classement matérialisés (#318) ───────────────────────────────
#
# Le classement par pertinence recalcule `to_tsvector` **par candidat** : mesuré à
# 674 ms sur un mot présent dans 20 % des documents, contre 0,2 ms pour le même filtre
# sans classement. Le coût n'est ni l'index (le plan montre bien le `BitmapOr`), ni le
# surlignage (le borner ne change rien) — c'est le rang, et lui seul.
#
# ⚠️ **Deux remèdes ont été mesurés et ÉCARTÉS**, pour qu'on ne les reprenne pas :
# borner les candidats avant classement ne gagne rien (1 278 ms contre 1 308 — ce que
# l'issue #318 proposait au départ), et `GENERATED ALWAYS AS … STORED` réécrit la
# table sous verrou exclusif — **7,55 s sur `datastore_rows`**, soit une interruption
# de service en pleine production, la base étant partagée.
#
# D'où la forme retenue : une colonne NULLABLE ordinaire (ajout instantané, aucun
# verrou), remplie **hors du chemin de démarrage**, et lue par un `COALESCE` qui rend
# la bascule inutile — une ligne remplie se classe par sa colonne, une ligne pas
# encore remplie recalcule comme avant. Aucun instant de bascule à ne pas rater, et
# **aucun silence possible** : un document non encore rempli reste trouvable et
# correctement classé.
#
# Mesuré sur 1 500 documents (mot fréquent) : **624 ms → 339 ms à mi-remplissage →
# 10,4 ms une fois plein**, avec un top-20 IDENTIQUE aux trois états.
#
# ⚠️ **C'est LA source unique**, au sens de la convention index↔requête : l'écriture,
# le rattrapage et le repli du `COALESCE` lisent tous cette table. Une expression
# recopiée ailleurs divergerait en silence — le classement se ferait alors sur un
# texte qui n'est plus celui qu'on indexe.
RANKED_SOURCES = {
    # table                  → (expression du texte, colonne du vecteur)
    "docs": DOCS_TEXT,
    "projects": PROJECTS_TEXT,
    "org_instructions": INSTR_TEXT,
    "guides": GUIDES_TEXT,
    "nodes": NODES_TEXT,
    "datastore_rows": DATASTORE_ROWS_TEXT,
    "project_file_texts": FILE_TEXT,
}

# Le nom est le même partout : une colonne par table, jamais un nom par source.
RANK_VECTOR_COLUMN = "search_vec"


# MaxFragments=2 : deux extraits courts plutôt qu'un long qui coupe en plein
# tableau markdown (oto-backend#6). Le texte est pré-nettoyé de ses pipes `|`.
_HL_OPTS = "MaxWords=22,MinWords=8,ShortWord=2,HighlightAll=false,MaxFragments=2"


def _vec(text_expr: str) -> str:
    return f"to_tsvector('french', {_fold(text_expr)})"


def _trgm(text_expr: str) -> str:
    """Expression d'index TRIGRAMME (#67) sur le texte foldé — rend `ILIKE '%…%'`
    indexé (substring rapide) en complément de la FTS tokenisée : « syl » retrouve
    « Sylvie », les fragments/préfixes matchent sans seq-scan. Même `_fold` que la FTS."""
    return f"({_fold(text_expr)}) gin_trgm_ops"


def rank_expr(table: str, alias: str = "") -> str:
    """L'expression de CLASSEMENT d'une source — la colonne, avec repli sur le calcul.

    `alias` sert les requêtes qui joignent (`t.search_vec`). Le repli n'est pas une
    précaution transitoire qu'on retirera : il reste après le rattrapage et couvre
    l'écriture qui aurait raté son vecteur — la ligne est simplement classée comme
    avant, jamais perdue, et le tour de rattrapage suivant la remet d'aplomb.
    """
    expr = RANKED_SOURCES.get(table)
    if expr is None:                      # source non matérialisée : l'ancien chemin
        return ""
    p = f"{alias}." if alias else ""
    return f"COALESCE({p}{RANK_VECTOR_COLUMN}, {_vec_of(expr, alias)})"


def _vec_of(expr: str, alias: str = "") -> str:
    """`_vec` appliqué à l'expression, préfixée de l'alias quand la requête joint."""
    return _vec(_prefix(expr, alias))


def _prefix(expr: str, alias: str) -> str:
    """Préfixe les colonnes nues d'une expression par l'alias de table.

    ⚠️ Grossier À DESSEIN : on ne préfixe que les identifiants connus de la source,
    jamais un mot quelconque — une substitution large casserait `coalesce`, `text` ou
    un littéral. Les expressions de ce module sont courtes et fermées ; si l'une
    devenait complexe au point d'exiger un vrai parseur, ce serait le signal qu'elle
    n'a plus sa place en chaîne."""
    if not alias:
        return expr
    for col in ("title", "body_md", "description", "name", "brief_md", "props",
                "data", "extracted_text"):
        expr = re.sub(rf"(?<![\w.]){col}(?![\w])", f"{alias}.{col}", expr)
    return expr


def rank_column_ddl() -> list[str]:
    """Les colonnes de vecteur de classement (#318) — DDL idempotent et INSTANTANÉ.

    `ADD COLUMN <tsvector>` **sans défaut et sans contrainte** ne réécrit pas la
    table (PostgreSQL 11+) : le catalogue est modifié, les lignes ne bougent pas. Le
    verrou est de l'ordre de la milliseconde, ce qui est la raison même de cette
    forme — la variante `GENERATED … STORED`, elle, réécrit tout et tenait
    `datastore_rows` **7,55 s** sous verrou exclusif, en pleine production.

    ⚠️ Aucun index sur ces colonnes : elles servent le CLASSEMENT, jamais le filtre.
    Le `WHERE` continue de passer par les GIN d'expression — les indexer coûterait
    l'écriture sans servir une seule requête."""
    return [f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {RANK_VECTOR_COLUMN} tsvector"
            for t in RANKED_SOURCES]


def rank_backfill_sql(table: str, batch: int) -> str:
    """Une TRANCHE de rattrapage : au plus `batch` lignes dont le vecteur manque.

    Borné par tranche, donc jamais un verrou de table — c'est ce qui permet de le
    jouer pendant que la production écrit. Le prédicat `IS NULL` fait office de file :
    aucune colonne d'avancement à tenir, et une ligne écrite après coup (sans son
    vecteur) revient d'elle-même dans la file au tour suivant. C'est aussi ce qui rend
    le rattrapage utile APRÈS la fin du remplissage : il devient la réconciliation.
    """
    expr = RANKED_SOURCES[table]
    return (f"UPDATE {table} SET {RANK_VECTOR_COLUMN} = {_vec(expr)} "
            f"WHERE ctid IN (SELECT ctid FROM {table} "
            f"WHERE {RANK_VECTOR_COLUMN} IS NULL LIMIT {int(batch)})")


def stamp_rank_vector(conn, table: str, where: str, params: tuple = ()) -> None:
    """Rafraîchit le vecteur de classement des lignes visées — DANS la transaction
    de l'écriture qui vient de les modifier.

    C'est le maintien à l'écriture (#318, barreau 2) : sans lui, une ligne modifiée
    garderait indéfiniment son ancien vecteur : le rattrapage ne traite que les
    NULL. On invalide donc d'abord le cache dans la transaction métier, puis on
    tente son calcul sous savepoint. En cas d'échec, le classement calcule le
    contenu courant et le worker retrouve la ligne NULL ; jamais de rang périmé.

    ⚠️ **Un UPDATE séparé, volontairement, plutôt qu'une colonne de plus dans
    l'écriture** : en SQL, `SET data = <neuf>, vec = f(data)` calcule `f` sur
    l'ANCIENNE valeur — le vecteur naîtrait périmé, silencieusement, et le test le
    plus évident (« après écriture, le vecteur est frais ») passerait au vert sur du
    faux si on comparait à la mauvaise chose. Le faire APRÈS, sur la ligne écrite,
    n'a pas ce piège.

    Seul le RECALCUL est best-effort, avec warning. L'invalidation est requise :
    son échec refuse l'écriture métier plutôt que de conserver un rang périmé.
    Une source non matérialisée n'a aucun cache à maintenir.

    ⚠️ **Le SAVEPOINT est ce qui rend le best-effort VRAI** (#333) : sans lui,
    l'erreur avalée laisse la transaction PARTAGÉE avortée — le COMMIT de
    l'écriture métier devient un ROLLBACK silencieux pendant que l'appelant
    reçoit l'écho du RETURNING. `conn.transaction()` sous une transaction déjà
    ouverte (le cas ici, toujours) crée un savepoint : l'échec du stamp roule
    au savepoint, l'écriture métier commit.
    """
    expr = RANKED_SOURCES.get(table)
    if not expr:
        return
    conn.execute(f"UPDATE {table} SET {RANK_VECTOR_COLUMN} = NULL WHERE {where}", params)
    try:
        with conn.transaction():
            conn.execute(
                f"UPDATE {table} SET {RANK_VECTOR_COLUMN} = {_vec(expr)} WHERE {where}",
                params)
    except Exception:
        logger.warning("Rank vector refresh failed for %s; cache invalidated", table, exc_info=True)


def rank_pending_counts() -> dict:
    """Combien de lignes attendent encore leur vecteur, par source — l'état du
    rattrapage, lisible sans deviner."""
    out = {}
    with _connect() as conn:
        for t in RANKED_SOURCES:
            row = conn.execute(
                f"SELECT count(*) AS n FROM {t} WHERE {RANK_VECTOR_COLUMN} IS NULL"
            ).fetchone()
            if row and row["n"]:
                out[t] = int(row["n"])
    return out


def index_ddl() -> list[str]:
    """DDL des index GIN d'expression (idempotents), consommé par `_init.init_db`.
    `CREATE INDEX` simple (pas CONCURRENTLY : init_db est transactionnel) — tables
    petites, verrou bref."""
    return [
        f"CREATE INDEX IF NOT EXISTS idx_docs_fts ON docs USING GIN ({_vec(DOCS_TEXT)})",
        f"CREATE INDEX IF NOT EXISTS idx_projects_fts ON projects USING GIN ({_vec(PROJECTS_TEXT)})",
        f"CREATE INDEX IF NOT EXISTS idx_org_instructions_fts ON org_instructions USING GIN ({_vec(INSTR_TEXT)})",
        # ⚠️ Les deux index `idx_guides_*` ne servent plus AUCUNE requête d'ici (la
        # recherche lit `nodes` depuis #282) — ils restent posés parce que la PROD
        # tourne encore l'ancien code sur CETTE MÊME base et s'en sert. Leur DROP
        # (et celui de la table) appartient au lot qui suivra le tag prod, pas ici :
        # les retirer maintenant casserait la recherche de guides en production
        # instantanément (docs/live-migrations.md, « la danse en N lots »).
        f"CREATE INDEX IF NOT EXISTS idx_guides_fts ON guides USING GIN ({_vec(GUIDES_TEXT)}) "
        "WHERE delivery = 'on-demand'",
        # Couches de contexte converties en NŒUDS (#282). Index sur `nodes` ENTIÈRE,
        # sans prédicat partiel : la table porte des dizaines de lignes (seule la
        # façade des guides y écrit), et le prédicat se décidera quand les lignes de
        # tableau y entreront — le calibrer sur une population qui n'existe pas est
        # exactement l'erreur qui a produit cette régression.
        f"CREATE INDEX IF NOT EXISTS idx_nodes_fts ON nodes USING GIN ({_vec(NODES_TEXT)})",
        # Lignes de datastore (#67 V2.1) — rend les rows trouvables via oto_search.
        f"CREATE INDEX IF NOT EXISTS idx_datastore_rows_fts ON datastore_rows USING GIN ({_vec(DATASTORE_ROWS_TEXT)})",
        # Index TRIGRAMME (#67) : substring indexé (« syl »→« Sylvie », fragments/préfixes)
        # en complément de la FTS tokenisée — même repli d'accents. Un par source de prose + rows.
        f"CREATE INDEX IF NOT EXISTS idx_docs_trgm ON docs USING GIN ({_trgm(DOCS_TEXT)})",
        f"CREATE INDEX IF NOT EXISTS idx_projects_trgm ON projects USING GIN ({_trgm(PROJECTS_TEXT)})",
        f"CREATE INDEX IF NOT EXISTS idx_org_instructions_trgm ON org_instructions USING GIN ({_trgm(INSTR_TEXT)})",
        # Idem : gardé pour la PROD (cf. le commentaire du FTS ci-dessus), plus lu ici.
        f"CREATE INDEX IF NOT EXISTS idx_guides_trgm ON guides USING GIN ({_trgm(GUIDES_TEXT)}) "
        "WHERE delivery = 'on-demand'",
        f"CREATE INDEX IF NOT EXISTS idx_nodes_trgm ON nodes USING GIN ({_trgm(NODES_TEXT)})",
        f"CREATE INDEX IF NOT EXISTS idx_datastore_rows_trgm ON datastore_rows USING GIN ({_trgm(DATASTORE_ROWS_TEXT)})",
        # Texte extrait des fichiers (#298). Les DEUX index, et c'est mesuré, pas
        # symétrique : sans eux l'`ILIKE` structurel de `_prose_query` coûte **2,5 s**
        # sur 2 000 fichiers (contre 0,6 ms en FTS indexée) ; et la FTS SEULE ne rend
        # rien sur un fragment interne — « ylvestr » : 0 résultat en FTS, 1 en
        # trigramme. Poids relevé : 344 kB + 496 kB pour 2 000 fichiers de ~7 300
        # caractères, soit ~0,4 Mo par millier.
        # Partiels : seules les extractions ABOUTIES portent du texte, et les lignes
        # de refus (`unsupported`, `encrypted`…) n'ont rien à indexer. Le prédicat est
        # celui de la requête (`t.status = 'ok'`), sans quoi l'index serait inerte.
        f"CREATE INDEX IF NOT EXISTS idx_file_texts_fts ON project_file_texts "
        f"USING GIN ({_vec(FILE_TEXT)}) WHERE status = 'ok'",
        f"CREATE INDEX IF NOT EXISTS idx_file_texts_trgm ON project_file_texts "
        f"USING GIN ({_trgm(FILE_TEXT)}) WHERE status = 'ok'",
    ]


def _prose_query(table: str, text_expr: str, select_cols: str, headline_col: str,
                 where_scope: str, scope_params: tuple, q: str, limit: int,
                 rank_vec: str = "") -> list[dict]:
    """Requête générique d'une source de prose : match FTS foldé (l'index), rang
    `ts_rank_cd` length-normalized (|32 — une page géante ne domine ni ne disparaît),
    headline sur la saisie brute contre le texte original.

    Robustesses (oto-backend#6, #67) via un CTE qui calcule la tsquery UNE fois :
    - **substring toujours actif** (#67) : FTS tokenisée `OR` `ILIKE '%q%'` sur le texte
      foldé (index TRIGRAMME) → les FRAGMENTS/PRÉFIXES matchent (« syl »→« Sylvie ») en
      plus des mots/stems, et ça couvre aussi les symboles/stopwords (« € », « avoir » :
      tsq vide ne matche rien, le substring oui). Rang : ts_rank classe les hits FTS
      devant, les hits substring-seul (rank 0) en fin ;
    - **fragments** de headline pré-nettoyés des pipes markdown (`_HL_OPTS`) ;
    - **fallback OR** : si l'AND de tous les termes ne matche rien, on re-tente en OR.

    `rank_vec` (#318) = l'expression de CLASSEMENT, quand la source a sa colonne de
    vecteur matérialisée (`rank_expr(table)`). Elle ne remplace QUE le `ts_rank_cd` :
    le `WHERE` continue de porter l'expression indexée, sans quoi le planner cesserait
    d'utiliser les GIN — le filtre et le rang répondent à deux questions différentes,
    et une seule des deux coûtait cher. Vide = l'ancien chemin, à l'identique."""
    vec = _vec(text_expr)
    rank_on = rank_vec or vec
    fold_q = _fold("%s")            # translate(lower(%s), accents…)
    folded_doc = _fold(text_expr)   # le texte du document, foldé (repli ILIKE)
    hl_text = f"replace({headline_col}, '|', ' ')"  # pas de coupe en plein tableau

    def _run(or_mode: bool) -> list[dict]:
        ws = f"websearch_to_tsquery('french', {fold_q})"
        # OR = passer les `&` de la tsquery en `|` (garde le stemming de websearch).
        tsq = f"replace({ws}::text, '&', '|')::tsquery" if or_mode else ws
        sql = (
            f"WITH qq AS (SELECT {tsq} AS tsq, {fold_q} AS raw) "
            f"SELECT {select_cols}, ts_rank_cd({rank_on}, qq.tsq, 32) AS rank, "
            f"ts_headline('french', {hl_text}, qq.tsq, '{_HL_OPTS}') AS headline "
            f"FROM {table}, qq "
            # FTS tokenisée (mots/stems, rangés par ts_rank) OR substring trigramme
            # (fragments/préfixes « syl »→« Sylvie », rang 0 → en fin). Le substring
            # couvre AUSSI les symboles/stopwords (numnode=0, tsq vide ne matche rien).
            f"WHERE ({vec} @@ qq.tsq "
            f"       OR {folded_doc} ILIKE '%%' || qq.raw || '%%') "
            f"  AND ({where_scope}) "
            "ORDER BY rank DESC LIMIT %s"
        )
        # Params : tsq (1×q) + raw (1×q) dans le CTE, puis scope, puis limit.
        with _connect() as conn:
            rows = conn.execute(sql, (q, q, *scope_params, limit)).fetchall()
            return [dict(r) for r in rows]

    rows = _run(or_mode=False)
    if not rows and len(q.split()) > 1:
        rows = _run(or_mode=True)  # aucun résultat en AND multi-termes → OR
    return rows


def search_docs_fts(q: str, project_ids: list[int], *, limit: int = 20) -> list[dict]:
    """Pages (docs) des projets accessibles — kind=page."""
    if not project_ids:
        return []
    return _prose_query(
        "docs", DOCS_TEXT,
        "id, project_id, title, description, updated_at",
        "coalesce(body_md,'')",
        "project_id = ANY(%s)", (project_ids,), q, limit,
        rank_vec=rank_expr("docs"))


def search_project_briefs(q: str, project_ids: list[int], *, limit: int = 20) -> list[dict]:
    """Briefs des projets accessibles — kind=brief (un brief ne remonte que s'il matche)."""
    if not project_ids:
        return []
    return _prose_query(
        "projects", PROJECTS_TEXT,
        "id, name, updated_at",
        "coalesce(brief_md,'')",
        "id = ANY(%s) AND archived_at IS NULL", (project_ids,), q, limit,
        rank_vec=rank_expr("projects"))


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
        (str(org_id),), q, limit, rank_vec=rank_expr("org_instructions"))


def search_guides_fts(q: str, org_id: Optional[int], sub: str, *, limit: int = 20) -> list[dict]:
    """Guides ON-DEMAND lisibles par l'acteur : plateforme (tous) + org active + user.
    Scope 'group' exclu V1 (même écart nommé que les procédures d'équipe).

    Lit `nodes` depuis #282 (les couches de contexte y ont été converties au lot M1).
    Le `scope` de la surface EST l'`owner_type` du nœud et la livraison une clé de
    `props` — même vocabulaire, même prédicat, mêmes clés de retour qu'au temps de la
    table `guides` : rien ne change pour l'appelant."""
    return _prose_query(
        "nodes", NODES_TEXT,
        "owner_type AS scope, owner_id, props->>'slug' AS slug, "
        "coalesce(props->>'title','') AS title, "
        "coalesce(props->>'description','') AS description, updated_at",
        "coalesce(props->>'body_md','')",
        "props->>'delivery' = 'on-demand' AND (owner_type = 'platform' "
        "OR (owner_type = 'org' AND owner_id = %s) OR (owner_type = 'user' AND owner_id = %s))",
        (str(org_id or ""), sub), q, limit, rank_vec=rank_expr("nodes"))


def search_docs_semantic(query_literal: str, project_ids: list[int], *,
                         limit: int = 20, max_distance: float = 0.6) -> list[dict]:
    """kNN sémantique (lot 3) : pages des projets accessibles les plus PROCHES du
    vecteur de requête (distance cosine `<=>` sur l'index HNSW). Scopé accès comme le
    lexical (mêmes `project_ids`). `query_literal` = littéral halfvec `[...]`.

    `max_distance` = cut-off de distance cosine (~0.6) : sans lui, le kNN renvoie
    TOUJOURS `limit` pages, même sans rapport avec la requête → à 500 pages, un
    repli sémantique noierait le lexical sous du bruit (oto-backend#6). `body_excerpt`
    (début du corps) sert de passage de repli pour un hit sémantique pur (sans
    surlignage lexical)."""
    if not project_ids:
        return []
    # Une page matche si son embedding PRINCIPAL (doc_embeddings) OU un chunk de
    # DÉBORDEMENT (doc_chunk_embeddings, #6 C — pages longues) est proche. Deux kNN
    # INDEXÉS (chacun ORDER BY <=> LIMIT → HNSW), unis, dédupliqués par doc (distance min).
    k = max(limit * 3, limit)   # sur-échantillonne : la dédup par doc réduit le lot
    sql = (
        "WITH cand AS ("
        "  (SELECT e.doc_id, e.embedding <=> %s::halfvec AS distance "
        "     FROM doc_embeddings e JOIN docs d ON d.id = e.doc_id "
        "     WHERE d.project_id = ANY(%s) ORDER BY e.embedding <=> %s::halfvec LIMIT %s) "
        "  UNION ALL "
        "  (SELECT c.doc_id, c.embedding <=> %s::halfvec AS distance "
        "     FROM doc_chunk_embeddings c JOIN docs d ON d.id = c.doc_id "
        "     WHERE d.project_id = ANY(%s) ORDER BY c.embedding <=> %s::halfvec LIMIT %s)"
        "), best AS ("
        "  SELECT doc_id, min(distance) AS distance FROM cand WHERE distance < %s GROUP BY doc_id"
        ") "
        "SELECT d.id, d.project_id, d.title, d.description, d.updated_at, "
        "left(d.body_md, 400) AS body_excerpt, b.distance "
        "FROM best b JOIN docs d ON d.id = b.doc_id "
        "ORDER BY b.distance LIMIT %s"
    )
    with _connect() as conn:
        rows = conn.execute(
            sql, (query_literal, project_ids, query_literal, k,
                  query_literal, project_ids, query_literal, k,
                  max_distance, limit)).fetchall()
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
    """Fichiers des projets accessibles — kind=fichier, sur son NOM : match sur
    `filename + title + description` SEULEMENT (jamais `summary`, colonne morte).
    Table étroite → ILIKE foldé à la volée, pas d'index.

    ⚠️ Le CONTENU est une source SÉPARÉE (`search_file_contents`, #298), et pas par
    goût de la symétrie : le texte extrait vit dans une autre table
    (`project_file_texts`), et **un index d'expression ne peut pas couvrir une
    jointure**. Les fusionner ici obligerait donc à balayer — mesuré à 2,5 s sur
    2 000 fichiers, contre 0,6 ms indexé. Les deux sources rendent le même
    `kind`/`ref`, si bien que le RRF les réunit de lui-même : un fichier trouvé par
    son nom ET par son contenu cumule ses rangs et remonte."""
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


def search_file_contents(q: str, project_ids: list[int], *, limit: int = 20) -> list[dict]:
    """Le CONTENU des fichiers déposés (#298) — kind=fichier, `matched_by='content'`.

    Même régime que les autres sources de prose (`_prose_query` : FTS foldée `OR`
    substring indexé trigramme, fallback OR, headline sur le texte). Chercher « syl »
    trouve donc « Sylvestre » dans un PDF comme dans une page — la mesure a tranché :
    la FTS seule ne rend RIEN sur un fragment interne (0 résultat), le trigramme le
    trouve en 2 ms, et sans lui l'`ILIKE` structurel de ce patron coûterait 2,5 s.

    ⚠️ **`project_files` est jointe pour le SCOPE**, jamais pour le texte : l'index
    d'expression porte sur `project_file_texts` seule, comme tout index d'expression.
    Le nom du fichier reste servi par `search_files_meta` — deux sources, un seul
    `kind`, que le RRF réunit.

    Invariant « cherchable ⇔ lisible » : `project_ids` est résolu par l'appelant
    (`ownership.accessible_project_ids`), jamais ici — un fichier hérite de l'accès
    de son projet, et son contenu ne peut pas être plus visible que lui.

    ## Coût mesuré, et sa limite connue

    Sur 1 500 fichiers de ~7 000 caractères : **8 ms** pour un mot rare, **10 ms**
    pour un fragment interne, **~1,3 s pour un mot présent dans presque tous les
    documents**. Le cas dégénéré vient du classement par pertinence, qui recalcule le
    vecteur de CHAQUE candidat (l'index ne le rend pas) — il est donc inhérent au
    patron de prose partagé, et non propre aux fichiers ; il ne se voyait pas sur des
    pages, dix fois plus courtes.

    ⚠️ Deux fausses pistes écartées par la mesure, pour qu'on ne les reprenne pas :
    ce n'est ni le SURLIGNAGE (le borner à 20 000 caractères ne change rien : 1 383 ms
    contre 1 255), ni un défaut d'index (le plan montre bien le `BitmapOr` des deux
    GIN). Le traiter demanderait de borner les candidats avant le classement, ce qui
    touche `_prose_query` et donc TOUTES les sources — un lot à part."""
    if not project_ids:
        return []
    return _prose_query(
        "project_file_texts t JOIN project_files f ON f.id = t.file_id",
        FILE_TEXT,
        "t.file_id AS id, f.project_id, f.filename, f.title, f.created_at",
        "t.extracted_text",
        # Seuls les fichiers dont l'extraction a ABOUTI portent du texte : filtrer sur
        # le statut évite de balayer les lignes de refus, qui ont un texte vide.
        "f.project_id = ANY(%s) AND t.status = 'ok'",
        (project_ids,), q, limit,
        # La source joint `project_files` pour le SCOPE : le classement doit donc
        # viser la colonne de `project_file_texts`, alias compris.
        rank_vec=rank_expr("project_file_texts", "t"))


def search_datastore_rows_fts(q: str, ns_ids: list[int], *, limit: int = 20) -> list[dict]:
    """Lignes de datastore des namespaces ACCESSIBLES (résolus par le caller, jamais
    ici — invariant « cherchable ⇔ lisible » : une ligne hérite de l'accès de son
    namespace) — kind=ligne (#67 V2.1). Même régime FTS que la prose (index GIN
    d'expression `french` + repli d'accents + fallback OR + repli ILIKE symboles),
    headline sur le JSON rendu (pipes dépipés). Retour : `ns_id, row_id` (deep-link)
    + `headline`."""
    if not ns_ids:
        return []
    return _prose_query(
        "datastore_rows", DATASTORE_ROWS_TEXT,
        "ns_id, row_id, updated_at, left(data::text, 200) AS excerpt",
        "data::text",
        "ns_id = ANY(%s)", (ns_ids,), q, limit,
        rank_vec=rank_expr("datastore_rows"))
