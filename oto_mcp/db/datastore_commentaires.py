"""Commentaires de tableau : lecture et écriture de `datastore_row_comments`.

Trois cibles (20/11/2026) : la LIGNE (`row_id`), une CELLULE (`row_id` + `column_key`),
une COLONNE (`column_key` seul, `row_id` NULL). Les lectures « d'une ligne » rendent la
ligne ET ses cellules ; celles « d'une colonne » ne rendent que la colonne elle-même.

NON aplati dans `db.*` : `lister`/`ajouter`/`retirer` sont trop communs pour la surface
plate. Les appelants écrivent `db.datastore_commentaires.<fn>`.

Un commentaire retiré garde sa ligne (`deleted_at`) et ne se sert plus :
aucune lecture d'ici ne le rend.
"""
from __future__ import annotations

from typing import Iterable, Optional

from ._conn import _connect

_COLONNES = "id, row_id, column_key, auteur_sub, body, created_at"

# ── La cible élargie, sur une base d'avant (démarrage ET révision
# `20261125_1710_doc_cell_comments`, mêmes ordres : ordre indifférent) ──────────────
COLONNE_CIBLE = "column_key"
DDL_COLONNE_CIBLE = ("ALTER TABLE datastore_row_comments "
                     "ADD COLUMN IF NOT EXISTS column_key TEXT")
# Relâchement à sens unique : l'ancien code écrit toujours `row_id` et ne lit que par
# `row_id = %s`, qu'un commentaire de colonne (NULL) ne satisfait jamais — il lui est
# invisible plutôt que mal servi.
DDL_LIGNE_FACULTATIVE = ("ALTER TABLE datastore_row_comments "
                         "ALTER COLUMN row_id DROP NOT NULL")
CONTRAINTE_CIBLE = "datastore_row_comments_cible"
DDL_CONTRAINTE_CIBLE = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = "
    f"'{CONTRAINTE_CIBLE}') THEN ALTER TABLE datastore_row_comments ADD CONSTRAINT "
    f"{CONTRAINTE_CIBLE} CHECK (row_id IS NOT NULL OR column_key IS NOT NULL); "
    "END IF; END $$")
# Après la colonne, jamais dans le fragment : un index du DDL assemblé sur une colonne
# née d'un ALTER casse le démarrage d'une base d'avant (#450).
INDEX_COLONNE = "idx_datastore_row_comments_colonne"
DDL_INDEX_COLONNE = (
    f"CREATE INDEX IF NOT EXISTS {INDEX_COLONNE} ON datastore_row_comments "
    "(ns_id, column_key, id) WHERE row_id IS NULL AND deleted_at IS NULL")


def lister(ns_id: int, row_id: str, *, colonne: Optional[str] = None,
           avant_id: Optional[int] = None, limit: int = 50,
           sauf_colonnes: Iterable[str] = ()) -> list[dict]:
    """Les commentaires vivants d'une ligne — la ligne ET ses cellules, ou la seule
    cellule `colonne` —, le plus récent d'abord (`id` décroissant), au plus `limit + 1` :
    celui de trop dit à l'appelant qu'il reste une page.

    `sauf_colonnes` écarte, EN SQL, les commentaires des cellules de ces colonnes (les
    colonnes masquées à un agent) : filtrés après coup, ils videraient des pages et
    fausseraient `limit + 1`. Les commentaires de la ligne elle-même restent."""
    clauses = ["ns_id = %s", "row_id = %s", "deleted_at IS NULL"]
    params: list = [int(ns_id), str(row_id)]
    if colonne is not None:
        clauses.append("column_key = %s")
        params.append(str(colonne))
    exclues = sorted({str(c) for c in sauf_colonnes})
    if exclues:
        clauses.append("(column_key IS NULL OR NOT (column_key = ANY(%s)))")
        params.append(exclues)
    if avant_id is not None:
        clauses.append("id < %s")
        params.append(int(avant_id))
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT {_COLONNES} FROM datastore_row_comments "
            f"WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT %s",
            (*params, int(limit) + 1)).fetchall()]


APERCU = 280


def comptes(ns_id: int, row_ids: list[str]) -> list[dict]:
    """Pour chaque `row_id` demandé qui existe encore ET porte des commentaires
    vivants : `n`, leur nombre, et le plus récent (`id`, `auteur_sub`, `created_at`,
    `apercu` = les `APERCU` premiers caractères de son corps, `tronque`). Les autres
    ne sont pas rendus.

    Une sonde PAR LIGNE demandée (`LATERAL`, au plus une page de grille) plutôt
    qu'une agrégation du tableau : chacune est un parcours de l'index partiel
    `(ns_id, row_id, id)`, le plus récent lu en remontant l'index (`LIMIT 1`), sans
    tri. Le coût suit les lignes AFFICHÉES, jamais la taille du tableau."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(SQL_COMPTES, params_comptes(ns_id, row_ids))]


# La requête des comptes, exposée pour que le banc lise son PLAN (`EXPLAIN`) sur le
# texte même qu'exécute `comptes`, pas sur une copie.
SQL_COMPTES = (
    "SELECT x.row_id, cnt.n, d.id, d.auteur_sub, d.created_at, "
    "       left(d.body, %(apercu)s) AS apercu, length(d.body) > %(apercu)s AS tronque "
    "FROM unnest(%(ids)s::text[]) AS x(row_id) "
    "JOIN datastore_rows r ON r.ns_id = %(ns)s AND r.row_id = x.row_id "
    "CROSS JOIN LATERAL (SELECT count(*) AS n FROM datastore_row_comments c "
    "    WHERE c.ns_id = %(ns)s AND c.row_id = x.row_id AND c.deleted_at IS NULL) cnt "
    "JOIN LATERAL (SELECT c.id, c.auteur_sub, c.created_at, c.body "
    "    FROM datastore_row_comments c "
    "    WHERE c.ns_id = %(ns)s AND c.row_id = x.row_id AND c.deleted_at IS NULL "
    "    ORDER BY c.id DESC LIMIT 1) d ON true")


def params_comptes(ns_id: int, row_ids: list[str]) -> dict:
    return {"apercu": APERCU, "ids": list(dict.fromkeys(row_ids)), "ns": int(ns_id)}


def lister_colonne(ns_id: int, colonne: str, *, avant_id: Optional[int] = None,
                   limit: int = 50) -> list[dict]:
    """Les commentaires vivants d'une COLONNE (pas ceux de ses cellules), même ordre et
    même page de trop que `lister`."""
    clauses = ["ns_id = %s", "row_id IS NULL", "column_key = %s", "deleted_at IS NULL"]
    params: list = [int(ns_id), str(colonne)]
    if avant_id is not None:
        clauses.append("id < %s")
        params.append(int(avant_id))
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT {_COLONNES} FROM datastore_row_comments "
            f"WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT %s",
            (*params, int(limit) + 1)).fetchall()]


def comptes_cellules(ns_id: int, row_ids: list[str]) -> list[dict]:
    """`(row_id, column_key, n)` : les cellules commentées des lignes demandées qui
    existent encore. Même borne que `comptes` : le coût suit les lignes affichées."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT c.row_id, c.column_key, count(*) AS n "
            "FROM datastore_row_comments c "
            "JOIN datastore_rows r ON r.ns_id = c.ns_id AND r.row_id = c.row_id "
            "WHERE c.ns_id = %s AND c.row_id = ANY(%s::text[]) "
            "AND c.column_key IS NOT NULL AND c.deleted_at IS NULL "
            "GROUP BY c.row_id, c.column_key",
            (int(ns_id), list(dict.fromkeys(row_ids)))).fetchall()]


def comptes_colonnes(ns_id: int) -> dict[str, int]:
    """`{column_key: n}` : les commentaires vivants de chaque colonne du tableau (ceux
    des colonnes elles-mêmes) — peu de lignes, lues par l'index partiel des colonnes."""
    with _connect() as conn:
        return {r["column_key"]: int(r["n"]) for r in conn.execute(
            "SELECT column_key, count(*) AS n FROM datastore_row_comments "
            "WHERE ns_id = %s AND row_id IS NULL AND deleted_at IS NULL "
            "GROUP BY column_key", (int(ns_id),)).fetchall()}


def ajouter(ns_id: int, row_id: Optional[str], *, auteur_sub: Optional[str], body: str,
            colonne: Optional[str] = None) -> dict:
    """La ligne (`colonne` absente), une cellule (les deux), ou une colonne (`row_id`
    None)."""
    with _connect() as conn:
        return dict(conn.execute(
            f"INSERT INTO datastore_row_comments (ns_id, row_id, column_key, auteur_sub, "
            f"body) VALUES (%s, %s, %s, %s, %s) RETURNING {_COLONNES}",
            (int(ns_id), None if row_id is None else str(row_id), colonne, auteur_sub,
             body)).fetchone())


def lire(ns_id: int, row_id: str, comment_id: int) -> Optional[dict]:
    """Un commentaire VIVANT de cette ligne ou d'une de ses cellules, ou `None`."""
    with _connect() as conn:
        r = conn.execute(
            f"SELECT {_COLONNES} FROM datastore_row_comments WHERE id = %s "
            f"AND ns_id = %s AND row_id = %s AND deleted_at IS NULL",
            (int(comment_id), int(ns_id), str(row_id))).fetchone()
    return dict(r) if r else None


def lire_colonne(ns_id: int, colonne: str, comment_id: int) -> Optional[dict]:
    """Un commentaire VIVANT de cette colonne (elle-même), ou `None`."""
    with _connect() as conn:
        r = conn.execute(
            f"SELECT {_COLONNES} FROM datastore_row_comments WHERE id = %s "
            f"AND ns_id = %s AND row_id IS NULL AND column_key = %s AND deleted_at IS NULL",
            (int(comment_id), int(ns_id), str(colonne))).fetchone()
    return dict(r) if r else None


def retirer(comment_id: int) -> bool:
    """Retire un commentaire vivant ; faux s'il l'était déjà (deux retraits croisés)."""
    with _connect() as conn:
        return conn.execute(
            "UPDATE datastore_row_comments SET deleted_at = now() "
            "WHERE id = %s AND deleted_at IS NULL", (int(comment_id),)).rowcount == 1
