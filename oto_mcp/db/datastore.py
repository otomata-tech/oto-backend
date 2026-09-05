"""Datastore spine PG (ADR 0016) : namespaces, lignes JSONB, resource grants (ADR 0030).

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

from ..datastore.schema import LAYER_KEYS, VALUE_LAYER
# Chemins et feuilles : extraits dans `paths` (#325), ré-exportés ici pour que la
# surface plate `db.<fn>` et tous les appelants restent inchangés.
from .paths import (  # noqa: F401
    FIELD_VALUE_PARAM_SQL,
    LAYER_VALUE_PARAM_SQL,
    ROW_VALUES_TEXT_SQL,
    bkey_index_expr,
    field_read_sql,
    field_value_sql,
    leaf_read_sql,
    split_layer,
    split_list_path,
)
from ._conn import _connect, _connect_autocommit
# Construction des requêtes : extraite dans `query` (#325), ré-exportée ici pour que
# la surface plate `db.<fn>` et tous les appelants restent inchangés.
# Le tableau et sa propriété : extraits dans `datastore_ns` (#325), ré-exportés ici
# pour que la surface plate `db.<fn>` et tous les appelants restent inchangés.
from .datastore_ns import (  # noqa: F401
    count_datastore_rows_for_ns,
    create_datastore_namespace,
    delete_datastore_namespace_by_id,
    get_datastore_namespace,
    get_datastore_namespace_by_id,
    get_resource_grant,
    grant_resource,
    list_all_datastore_namespaces,
    list_datastore_namespaces_for_owners,
    list_datastore_namespaces_granted_to,
    list_resource_grants,
    rename_datastore_namespace_by_id,
    reparent_datastore_namespace,
    resolve_datastore_ns,
    revoke_resource_grant,
    set_datastore_schema,
    set_datastore_semantic,
)

# Le bail de ligne : extrait dans `rowlock` (#325), ré-exporté ici pour que la surface
# plate `db.<fn>` et tous les appelants restent inchangés.
from .rowlock import (  # noqa: F401
    datastore_active_lease,
    datastore_active_leases_of,
    datastore_claim_next,
    datastore_claim_row,
    datastore_claimed_rows,
    datastore_release_by_run,
    datastore_release_claim,
    datastore_row_within,
)
from .query import (  # noqa: F401
    _build_aggregate,
    _NUMERIC_RE,
    _ds_filter_clauses,
    _ds_meta_ts_clause,
    _ds_one_field_clause,
    _ds_text,
    _ds_where,
    _DS_FILTER_OPS,
    _DS_MAX_FIELDS_PER_FILTER,
    _DS_MAX_FILTERS,
    group_key,
    order_health_sql,
    typed_order_sql,
)
from .users import upsert_user


def datastore_insert_row(ns_id: int, row_id: str, data: dict,
                         created_at: Optional[str] = None,
                         updated_at: Optional[str] = None) -> dict:
    """Insère une row. `created_at`/`updated_at` optionnels (override pour le
    backfill ; sinon NOW())."""
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO datastore_rows (ns_id, row_id, data, created_at, updated_at, embed_dirty) "
            "VALUES (%s, %s, %s::jsonb, COALESCE(%s::timestamptz, NOW()), COALESCE(%s::timestamptz, NOW()), "
            # dirty ⟺ le namespace est opt-in sémantique (#67 V2.2) — sinon jamais embedé.
            "        (SELECT semantic_search FROM user_datastores WHERE id = %s)) "
            "RETURNING row_id, created_at, updated_at, data",
            (ns_id, row_id, json.dumps(data), created_at, updated_at, ns_id),
        ).fetchone()
        from .search import stamp_rank_vector
        stamp_rank_vector(conn, "datastore_rows", "ns_id = %s AND row_id = %s", (ns_id, row_id))
        return dict(row)


def datastore_upsert_row(ns_id: int, row_id: str, data: dict) -> tuple[dict, bool]:
    """Insère OU met à jour une row par sa clé `(ns_id, row_id)`. Idempotent :
    re-poser le même `row_id` remplace `data` au lieu de dupliquer (sert la
    dédup par clé stable, ex. urn LinkedIn). Renvoie `(row, inserted)` où
    `inserted` est True si la row n'existait pas (ON CONFLICT non déclenché)."""
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO datastore_rows (ns_id, row_id, data, created_at, updated_at, embed_dirty) "
            "VALUES (%s, %s, %s::jsonb, NOW(), NOW(), "
            "        (SELECT semantic_search FROM user_datastores WHERE id = %s)) "
            # data change ⟹ re-dirty ⟺ namespace opt-in sémantique (#67 V2.2).
            "ON CONFLICT (ns_id, row_id) DO UPDATE SET data = EXCLUDED.data, updated_at = NOW(), "
            "  embed_dirty = (SELECT semantic_search FROM user_datastores WHERE id = datastore_rows.ns_id), "
            # Écrire, c'est repartir de zéro (#433) : le compteur de reprises ne mesure
            # que les réservations SANS écriture, et le motif d'abandon tombe avec lui —
            # une ligne réparée à la main revient dans la file.
            "  claims = 0, abandon_reason = NULL "
            "RETURNING row_id, created_at, updated_at, data, (xmax = 0) AS inserted",
            (ns_id, row_id, json.dumps(data), ns_id),
        ).fetchone()
        from .search import stamp_rank_vector
        stamp_rank_vector(conn, "datastore_rows", "ns_id = %s AND row_id = %s", (ns_id, row_id))

        inserted = bool(row.pop("inserted"))
        return dict(row), inserted


def datastore_find_row_id_by_key(ns_id: int, key_field: str, key_value) -> Optional[str]:
    """Trouve le `row_id` d'une row par une CLÉ MÉTIER, pour la dédup d'un batch
    write. Renvoie le plus ancien match (ordre stable) ou None.

    ⚠️ L'expression vient de `bkey_index_expr` — LA MÊME que celle de l'index, à la
    chaîne près. Le planner ne sert un index d'EXPRESSION que si le `WHERE` porte
    exactement la sienne : un écart ne casserait rien de visible (la déduplication
    marcherait) et ferait simplement partir chaque lookup en seq scan. C'est la panne
    qu'on ne voit qu'au moment où le namespace est assez gros pour qu'elle coûte."""
    from psycopg import sql as _sql
    q = _sql.SQL(
        "SELECT row_id FROM datastore_rows WHERE ns_id = %s AND {e} = %s "
        "ORDER BY created_at ASC LIMIT 1"
    ).format(e=bkey_index_expr(key_field))
    with _connect() as conn:
        row = conn.execute(q, (ns_id, str(key_value))).fetchone()
        return row["row_id"] if row else None


# ── Clé métier = contrainte (#109 ch.3) ──────────────────────────────────────
# Quand `schema.key` est déclarée, elle cesse d'être purement applicative : un
# index UNIQUE PARTIEL par namespace (`ds_bkey_<ns_id>`, expression `data->>key`,
# prédicat ns_id + clé non nulle) rend la dédup concurrent-safe (deux writes
# parallèles du même member_id ⇒ le perdant prend une UniqueViolation, convertie
# en update par le store) et le lookup indexé. Cycle de vie : posé/déposé par
# `set_schema` (source unique de schema.key) + migration boot pour l'existant.

def _bkey_index_name(ns_id: int) -> str:
    return f"ds_bkey_{int(ns_id)}"


def datastore_capturer_origine(ns_id: int, champs: list[str]) -> int:
    """Pose `<champ>.origine` = la valeur COURANTE sur les lignes existantes, pour
    les colonnes qui viennent de gagner le format `origine: "system"`.

    Pourquoi à la DÉCLARATION et pas seulement à la première écriture
    (otomata-tech/oto#46) : la capture paresseuse ne garde rien sur une ligne qui
    existait déjà. Une valeur écrasée entre la création de la ligne et la première
    écriture d'après la déclaration était perdue sans filet, et le nom `origine`
    promettait davantage. Capturer ici rend la promesse exacte pour toute ligne,
    au prix d'une écriture unique, à un moment choisi par celui qui déclare.

    Trois règles, chacune fermant une porte :

    - une couche DÉJÀ posée n'est jamais touchée — capturer par-dessus effacerait
      justement ce qu'on cherche à garder ;
    - une colonne ABSENTE de la ligne ne reçoit rien : « pas de valeur » n'est pas
      « valeur vide », et poser `""` inventerait une origine ;
    - une colonne déjà enveloppée (elle porte d'autres couches) reçoit son
      `origine` **à côté** des couches existantes, sans les écraser.

    Une seule requête par champ, dans UNE transaction : la déclaration réussit
    entièrement ou pas du tout. Rend le nombre de lignes touchées.
    """
    from psycopg import sql as _sql

    if not champs:
        return 0
    touchees = 0
    with _connect() as conn:
        with conn.transaction():
            for champ in champs:
                # On remplace la COLONNE entière, pas une sous-clé : `jsonb_set`
                # ne sait pas créer un chemin dans une valeur plate (une chaîne
                # reste une chaîne, et la mise à jour ne fait rien — silencieusement).
                q = _sql.SQL(
                    "UPDATE datastore_rows SET data = jsonb_set(data, {chemin}, "
                    "  CASE WHEN jsonb_typeof(data->{k}) = 'object' "
                    # déjà enveloppée : on AJOUTE l'origine à côté des autres
                    # couches, en reprenant sa `valeur` courante
                    "       THEN (data->{k}) || jsonb_build_object("
                    "              'origine', COALESCE(data->{k}->'valeur', 'null'::jsonb)) "
                    # plate : on l'enveloppe, la valeur devenant aussi l'origine
                    "       ELSE jsonb_build_object('valeur', data->{k}, "
                    "                               'origine', data->{k}) END, true) "
                    "WHERE ns_id = %s "
                    # existe, et n'a pas déjà une origine
                    "  AND data ? {k} "
                    "  AND NOT (jsonb_typeof(data->{k}) = 'object' "
                    "           AND data->{k} ? 'origine')"
                ).format(k=_sql.Literal(str(champ)),
                         chemin=_sql.Literal("{" + str(champ) + "}"))
                touchees += conn.execute(q, (ns_id,)).rowcount
    return touchees


def datastore_key_dup_groups(ns_id: int, key: str, limit: int = 10) -> list[dict]:
    """Valeurs de clé métier en DOUBLON dans les rows existantes — `[{value, n}]`,
    plus gros groupes d'abord. Sert le refus actionnable de `set_schema` (on ne
    pose pas un UNIQUE sur des données sales sans le dire)."""
    from psycopg import sql as _sql
    q = _sql.SQL(
        "SELECT data->>{k} AS value, COUNT(*) AS n FROM datastore_rows "
        "WHERE ns_id = %s AND data->>{k} IS NOT NULL "
        "GROUP BY 1 HAVING COUNT(*) > 1 ORDER BY n DESC, 1 LIMIT %s"
    ).format(k=_sql.Literal(str(key)))
    with _connect() as conn:
        return [dict(r) for r in conn.execute(q, (ns_id, limit)).fetchall()]


def datastore_overlong_fields(ns_id: int, bounds: dict) -> list[dict]:
    """Champs dont des rows DÉJÀ EN BASE dépassent la borne `max_length` posée —
    `[{field, max_length, rows, longest}]`, pire dépassement d'abord.

    Sert l'avertissement (pas le refus) de `set_schema` : borner un champ après
    coup est légitime, mais celui qui pose la borne doit savoir ce que l'historique
    contient — ces lignes-là ne seront refusées qu'au geste qui les réécrit."""
    from psycopg import sql as _sql
    out: list[dict] = []
    with _connect() as conn:
        for field, ml in (bounds or {}).items():
            q = _sql.SQL(
                "SELECT COUNT(*) AS rows, MAX(length({v})) AS longest "
                "FROM datastore_rows WHERE ns_id = %s AND length({v}) > %s"
            ).format(v=field_value_sql(field))
            r = conn.execute(q, (ns_id, int(ml))).fetchone()
            if r and (r["rows"] or 0) > 0:
                out.append({"field": field, "max_length": int(ml),
                            "rows": int(r["rows"]), "longest": int(r["longest"])})
    return sorted(out, key=lambda d: d["longest"] - d["max_length"], reverse=True)



def datastore_offending_enum_values(ns_id: int, options: dict,
                                    per_field: int = 5) -> list[dict]:
    """Valeurs DÉJÀ EN BASE qu'un enum fraîchement déclaré condamne —
    `[{field, values: [{value, rows}], rows}]`, le plus atteint d'abord.

    Un schéma ne vaut que pour l'AVENIR : le poser ne revalide pas l'existant. Une
    colonne peut donc être pleine de valeurs que le format refuse désormais, sans
    que rien ne le dise — et le tableau *a l'air* conforme puisqu'il a un schéma.
    Vécu : 504 lignes en « Oui »/« Non » sur un enum `oui`/`non`/`inconnu`, valeurs
    présentes à l'écran et invisibles au filtrage comme aux facettes.

    On rend les valeurs FAUTIVES avec leur compte, pas un simple total : c'est ce
    qui permet de trancher tout de suite entre corriger la donnée et élargir les
    options. Les vides (`NULL`, chaîne vide) sont écartés — une case non remplie
    n'est pas une valeur hors options, c'est l'affaire de `required`."""
    from psycopg import sql as _sql
    out: list[dict] = []
    with _connect() as conn:
        for field, allowed in (options or {}).items():
            vals = [str(o) for o in (allowed or [])]
            if not vals:
                continue  # enum libre : aucune option déclarée, rien à condamner
            q = _sql.SQL(
                "SELECT {v} AS value, COUNT(*) AS rows "
                "FROM datastore_rows WHERE ns_id = %s "
                "AND {v} IS NOT NULL AND {v} <> '' "
                "AND NOT ({v} = ANY(%s)) "
                "GROUP BY 1 ORDER BY 2 DESC"
            ).format(v=field_value_sql(field))
            rows = conn.execute(q, (ns_id, vals)).fetchall()
            if not rows:
                continue
            out.append({
                "field": field,
                "rows": sum(int(r["rows"]) for r in rows),
                "distinct": len(rows),
                "values": [{"value": r["value"], "rows": int(r["rows"])}
                           for r in rows[:per_field]],
            })
    return sorted(out, key=lambda d: d["rows"], reverse=True)


def datastore_field_values(ns_id: int, fields, cap: int = 20000) -> dict:
    """Valeurs DISTINCTES déjà en base, par champ — `{champ: {values, truncated}}`,
    `values` = `[{value, rows}]`, la plus fréquente d'abord.

    Rend la donnée BRUTE plutôt qu'un verdict, et c'est le point : le contrôle qui
    l'exploite (le motif `pattern` de #387) doit s'exécuter avec le MÊME moteur que
    celui qui refusera les écritures. Poser le prédicat en SQL — l'opérateur `~` de
    PostgreSQL — ferait juger l'existant par un second moteur d'expressions, dont le
    dialecte diverge de Python sur `\\d`, `\\w` et les drapeaux : l'avertissement
    annoncerait alors un nombre de lignes que le refus ne confirmerait pas.

    `truncated` est rendu plutôt que subi : au-delà de `cap` valeurs distinctes, le
    relevé est PARTIEL et le dit — un compte tronqué qui se présente comme un total
    rassure exactement là où il ne faut pas."""
    from psycopg import sql as _sql
    out: dict = {}
    with _connect() as conn:
        for field in fields or []:
            q = _sql.SQL(
                "SELECT {v} AS value, COUNT(*) AS rows "
                "FROM datastore_rows WHERE ns_id = %s "
                "AND {v} IS NOT NULL AND {v} <> '' "
                "GROUP BY 1 ORDER BY 2 DESC LIMIT %s"
            ).format(v=field_value_sql(field))
            rows = conn.execute(q, (ns_id, int(cap) + 1)).fetchall()
            out[field] = {
                "values": [{"value": r["value"], "rows": int(r["rows"])}
                           for r in rows[:cap]],
                "truncated": len(rows) > cap,
            }
    return out


def datastore_drop_column(ns_id: int, key: str) -> int:
    """Retire la clé `key` du blob `data` de TOUTES les rows du namespace. Renvoie le
    nombre de rows modifiées (0 = la colonne n'existait dans aucune).

    L'opérateur JSONB `-` retire la clé, là où l'écrire à `null` la CONSERVE (une
    clé de valeur nulle reste une clé : elle continue de se rendre, et de tromper).
    Le `WHERE data ? key` borne l'UPDATE aux rows concernées — sur un namespace où
    la colonne est rare, on ne réécrit pas les autres pour rien.

    ⚠️ NON sérialisé avec les écritures applicatives : celles-ci font un
    read-merge-write du blob entier (`_merge_into_row`), donc un write dont le
    SELECT précède cette purge et l'UPDATE la suit REMET la clé sur sa ligne.
    Fenêtre étroite, effet re-purgeable — purger hors drainage, ou repasser."""
    from psycopg import sql as _sql
    q = _sql.SQL(
        "UPDATE datastore_rows SET data = data - {k} "
        " WHERE ns_id = %s AND data ? {k}"
    ).format(k=_sql.Literal(str(key)))
    with _connect() as conn:
        return conn.execute(q, (ns_id,)).rowcount or 0


def datastore_has_column(ns_id: int, key: str) -> bool:
    """UNE ligne au moins porte-t-elle cette clé ? (#680)

    Le pendant EXACT de `datastore_row_keys`, qui répond sur un échantillon des 1000
    lignes les plus récentes. L'échantillon suffit à SIGNALER des colonnes orphelines
    (elles sont sur toutes les lignes ou presque) ; il ne suffit pas à fonder une
    phrase qu'un opérateur va lire comme un fait — « `x.comment` est une couche de la
    colonne `x` » exige que `x` existe pour de bon, sans quoi on nomme une
    destination inventée, ce qui est pire que ne rien nommer.

    Même prédicat `data ? key` que la purge, donc même parcours au pire ; il n'est
    payé que sur le chemin où la purge n'a RIEN touché — c'est-à-dire jamais en
    régime normal, et une fois par nom fautif quand quelqu'un se trompe."""
    from psycopg import sql as _sql
    q = _sql.SQL(
        "SELECT 1 FROM datastore_rows WHERE ns_id = %s AND data ? {k} LIMIT 1"
    ).format(k=_sql.Literal(str(key)))
    with _connect() as conn:
        return conn.execute(q, (ns_id,)).fetchone() is not None


def datastore_row_keys(ns_id: int, sample: int = 1000) -> list[str]:
    """Clés présentes dans les DONNÉES d'un namespace, triées.

    Bornée à un ÉCHANTILLON (`sample` rows les plus récentes) : l'usage est de
    signaler des colonnes que le schéma ne déclare plus, et celles-là sont sur
    toutes les lignes ou presque. Scanner un namespace de 500 000 rows pour un
    geste de confort (poser un schéma) coûterait plus que ça ne rapporte — au prix
    assumé qu'une clé présente sur une poignée de lignes anciennes puisse échapper
    au relevé."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT jsonb_object_keys(data) AS k FROM ("
            "  SELECT data FROM datastore_rows WHERE ns_id = %s "
            "   ORDER BY created_at DESC, row_id DESC LIMIT %s) t",
            (ns_id, int(sample)),
        ).fetchall()
    return sorted(r["k"] for r in rows)


def datastore_merge_key_duplicates(ns_id: int, key: str) -> int:
    """Résorbe les doublons de clé métier en reconstituant la sémantique upsert :
    pour chaque valeur en doublon, MERGE les `data` dans l'ordre chronologique dans
    la row la plus ANCIENNE (celle que `find_row_id_by_key` aurait servie à chaque
    write), puis supprime les plus récentes. Renvoie le nombre de rows supprimées.
    Une transaction par groupe (échec isolé, jamais de demi-merge)."""
    from psycopg import sql as _sql
    key = str(key)
    removed = 0
    dup_q = _sql.SQL(
        "SELECT data->>{k} AS value FROM datastore_rows "
        "WHERE ns_id = %s AND data->>{k} IS NOT NULL GROUP BY 1 HAVING COUNT(*) > 1"
    ).format(k=_sql.Literal(key))
    rows_q = _sql.SQL(
        "SELECT row_id, data FROM datastore_rows WHERE ns_id = %s AND data->>{k} = %s "
        "ORDER BY created_at ASC, row_id ASC"
    ).format(k=_sql.Literal(key))
    with _connect() as conn:
        values = [r["value"] for r in conn.execute(dup_q, (ns_id,)).fetchall()]
    for value in values:
        with _connect() as conn:
            group = conn.execute(rows_q, (ns_id, value)).fetchall()
            if len(group) < 2:
                continue  # résorbé entre-temps
            merged: dict = {}
            for r in group:
                d = r["data"]
                merged.update(d if isinstance(d, dict) else json.loads(d))
            keeper = group[0]["row_id"]
            losers = [r["row_id"] for r in group[1:]]
            conn.execute(
                "UPDATE datastore_rows SET data = %s::jsonb, updated_at = NOW() "
                "WHERE ns_id = %s AND row_id = %s",
                (json.dumps(merged), ns_id, keeper))
            conn.execute(
                "DELETE FROM datastore_rows WHERE ns_id = %s AND row_id = ANY(%s)",
                (ns_id, losers))
            removed += len(losers)
    return removed


class KeyIndexUnavailable(RuntimeError):
    """La pose de l'index d'unicité de clé métier n'a pas abouti DANS SA BORNE.

    Ce n'est pas une erreur de schéma : le schéma est écrit, la clé est déclarée, et
    le chemin d'écriture applicatif (lookup-puis-insert de `datastore/core.py`)
    continue de rapprocher les lignes sur cette clé. Ce qui manque est la garantie
    ANTI-COURSE que l'index apporte en plus — et `oto-mcp maintenance key-indexes`
    la repose au tir suivant, puisqu'il balaie justement les namespaces à clé
    déclarée dont l'index est absent.

    Elle existe pour que l'appelant puisse le DIRE plutôt que rendre un 500 opaque
    sur un schéma pourtant posé (incident du 2026-09-01)."""


def datastore_ensure_key_index(ns_id: int, key: str, *, bornee: bool = True) -> None:
    """Pose l'index UNIQUE partiel de clé métier du namespace (dépose l'ancien —
    la clé a pu changer). Nom déterministe `ds_bkey_<ns_id>` (int → sûr) ; la clé
    est un LITTÉRAL composé via psycopg.sql (le DDL ne se paramètre pas).

    ⚠️ **Travail bloquant : jamais depuis le thread de l'event loop.** Les deux
    adaptateurs de capacité y veillent (handler sync → threadpool) ; un appelant qui
    invente son propre chemin doit faire pareil.

    `bornee=True` (défaut, chemin de REQUÊTE) : lève `KeyIndexUnavailable` quand la
    borne coupe — le schéma reste posé, la maintenance reprend l'index. `bornee=False`
    pour le travail de FOND, qui a le droit d'attendre son tour."""
    from psycopg import sql as _sql
    name = _bkey_index_name(ns_id)
    expr = bkey_index_expr(key)
    # CRÉER AVANT DE DÉPOSER, et jamais l'inverse : un DROP suivi d'un CREATE laisse
    # une fenêtre où RIEN n'impose l'unicité, et un batch concurrent y insère des
    # doublons que l'index neuf ne pourra plus se créer par-dessus. Les deux
    # coexistent sans conflit — sur une ligne plate, les deux expressions rendent la
    # même valeur.
    #
    # CONCURRENTLY ne bloque pas les écritures pendant la construction, et REFUSE de
    # tourner dans une transaction (vérifié) — d'où la connexion autocommit. Mesuré :
    # 40 ms sur 50 000 lignes, contre 32 ms pour l'ancienne forme plate.
    #
    # Ce qu'il ne dit PAS de lui-même : avant de construire, il attend la fin de toute
    # transaction ouverte avant lui. Une lecture inoffensive suffit à le retenir aussi
    # longtemps qu'elle dure — d'où la borne posée sur la connexion, et le rattrapage
    # ci-dessous quand elle coupe.
    tmp = _sql.Identifier(name + "_v2")
    with _connect_autocommit(bornee=bornee) as conn:
        try:
            conn.execute(_sql.SQL("DROP INDEX IF EXISTS {t}").format(t=tmp))
            conn.execute(_sql.SQL(
                "CREATE UNIQUE INDEX CONCURRENTLY {t} ON datastore_rows (({e})) "
                "WHERE ns_id = {ns} AND {e} IS NOT NULL"
            ).format(t=tmp, e=expr, ns=_sql.Literal(int(ns_id))))
            conn.execute(_sql.SQL("DROP INDEX IF EXISTS {n}").format(
                n=_sql.Identifier(name)))
            conn.execute(_sql.SQL("ALTER INDEX {t} RENAME TO {n}").format(
                t=tmp, n=_sql.Identifier(name)))
        except (psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled) as e:
            # Un `CREATE INDEX CONCURRENTLY` coupé laisse son index INVALIDE derrière
            # lui — et un unique invalide peut continuer d'imposer sa contrainte aux
            # écritures suivantes. On le retire tout de suite : le laisser ferait
            # refuser des écritures au nom d'un index que personne ne sait nommer.
            # Best-effort ET COURT : ce DROP réclame un verrou exclusif, donc il se
            # heurte au même mur que ce qu'on vient de subir — le tenter au budget
            # plein doublerait le temps d'échec pour rien.
            try:
                conn.execute("SET lock_timeout = '250ms'")
                conn.execute(_sql.SQL("DROP INDEX IF EXISTS {t}").format(t=tmp))
            # noqa: SILENT — nettoyage d'appoint ; la 1re ligne du prochain appel refait le geste
            except Exception:
                logger.warning("ds_bkey ns=%s : index temporaire non nettoyé",
                               ns_id, exc_info=True)
            raise KeyIndexUnavailable(
                f"index d'unicité de `{key}` : la base ne l'a pas laissé se poser dans "
                "sa borne — une transaction ouverte le retenait. Le schéma EST écrit et "
                "la clé reste rapprochée à l'écriture ; c'est la garantie anti-course "
                "qui manque, et la maintenance la repose au tir suivant.") from e


def datastore_drop_key_index(ns_id: int) -> None:
    """Dépose l'index d'unicité — ET le temporaire `_v2` qu'une pose coupée aurait
    pu laisser (sinon un unique orphelin continue de refuser des écritures pour une
    clé qui n'est plus déclarée)."""
    from psycopg import sql as _sql
    name = _bkey_index_name(ns_id)
    with _connect() as conn:
        conn.execute(_sql.SQL("DROP INDEX IF EXISTS {n}").format(
            n=_sql.Identifier(name)))
        conn.execute(_sql.SQL("DROP INDEX IF EXISTS {t}").format(
            t=_sql.Identifier(name + "_v2")))


def datastore_has_key_index(ns_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute("SELECT 1 FROM pg_indexes WHERE indexname = %s",
                           (_bkey_index_name(ns_id),)).fetchone()
        return row is not None


def datastore_namespaces_with_key() -> list[dict]:
    """Namespaces dont le schéma déclare une clé métier — `[{id, key}]` (migration
    boot #109 ch.3 : matérialiser la clé en contrainte sur l'existant)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, schema->>'key' AS key FROM user_datastores "
            "WHERE schema->>'key' IS NOT NULL AND schema->>'key' <> ''"
        ).fetchall()
        return [dict(r) for r in rows]


_DS_MAX_ROWS_BY_IDS = 200


def datastore_rows_by_ids(ns_id: int, row_ids: list) -> dict:
    """Contenu d'un LOT de lignes, en UNE requête : `{row_id: data}`.

    Sert à libeller des références (le journal d'activité cite des `row_id`, l'UI
    veut le champ `role="title"`). Les ids inconnus — ligne supprimée depuis —
    sont simplement absents du résultat, jamais une erreur. Lot borné.
    """
    ids = [str(r) for r in (row_ids or []) if r][:_DS_MAX_ROWS_BY_IDS]
    if not ids:
        return {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT row_id, data FROM datastore_rows WHERE ns_id = %s AND row_id = ANY(%s)",
            (ns_id, ids),
        ).fetchall()
        return {r["row_id"]: (r["data"] or {}) for r in rows}


def datastore_get_row(ns_id: int, row_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT row_id, created_at, updated_at, data, claimed_by, claimed_until, "
            "       claimed_run, claims, abandon_reason, "
            "       (claimed_until IS NOT NULL AND claimed_until > NOW())"
            "           AS claim_active "
            "FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id),
        ).fetchone()
        return dict(row) if row else None


# Filtres par colonne (vue tableau dashboard, oto-dashboard#18). Chaque filtre =
# {field, op, value}. Le champ est TOUJOURS paramétré (`data ->> %s`) et l'op tiré
# d'une whitelist → fragment SQL fixe, zéro interpolation de valeur = pas d'injection.


def datastore_list_rows(ns_id: int, *, offset: int = 0, limit: Optional[int] = None,
                        order_by: Optional[str] = None, order_dir: str = "desc",
                        q: Optional[str] = None, filters: Optional[list] = None,
                        order_type: Optional[str] = None,
                        order_options: Optional[list] = None) -> list[dict]:
    """Page de rows d'un namespace. `order_by` : `_created_at`/`_updated_at`/`_id`
    (colonnes méta) ou un nom de champ user → `data->>field`. `q` : recherche
    plein-texte sur tout le JSON (substring ACCENT-INSENSIBLE, aligné sur oto_search).
    `filters` : filtres par
    colonne (liste `{field, op, value}`, combinés AND — cf. `_ds_filter_clauses`).
    Tri/pagination/recherche/filtres côté SQL (server-side, ADR 0016). `limit=None`
    = toutes les rows (compat `store.list_rows` / MCP `data_rows`).

    `order_type`/`order_options` (#336) : le TYPE DÉCLARÉ du champ trié
    (`number`/`enum`/`date`), résolu PAR LE STORE — cette couche ne connaît pas
    le schéma et ne doit pas le connaître, elle reçoit un type générique et
    construit l'expression (cf. `typed_order_sql`). None = tri textuel historique."""
    direction = "ASC" if str(order_dir).lower() == "asc" else "DESC"
    where, params = _ds_where(ns_id, q, filters)
    if order_by in (None, "", "_created_at"):
        order_sql = f"created_at {direction}, row_id {direction}"
    elif order_by == "_updated_at":
        order_sql = f"updated_at {direction}, row_id {direction}"
    elif order_by == "_id":
        order_sql = f"row_id {direction}"
    elif order_type:
        _v, _vp = field_read_sql(order_by)
        order_sql, _op = typed_order_sql(_v, _vp, order_type, order_options, direction)
        params.extend(_op)
    else:
        _v, _vp = field_read_sql(order_by)
        order_sql = f"{_v} {direction}, row_id {direction}"
        # ⚠️ TOUS les paramètres de l'expression, pas le nom une fois : depuis #318 la
        # lecture d'une colonne est un COALESCE à DEUX emplacements (plate ou à
        # couches), et un chemin de liste en compte quatre. N'en fournir qu'un faisait
        # échouer la requête — tout tri par colonne user, en production. Le banc de tri
        # stubbait le SQL : il vérifiait quel CHEMIN de code est pris, jamais que la
        # requête s'exécute. La sonde qui l'attrape est donc contre un vrai PostgreSQL.
        params.extend(_vp)
    tail = ""
    if limit is not None:
        tail = " LIMIT %s OFFSET %s"
        params.extend([limit, offset])
    with _connect() as conn:
        rows = conn.execute(
            "SELECT row_id, created_at, updated_at, data, claimed_by, claimed_until, "
            "       claimed_run, claims, abandon_reason, "
            "       (claimed_until IS NOT NULL AND claimed_until > NOW())"
            "           AS claim_active "
            f"FROM datastore_rows {where} ORDER BY {order_sql}{tail}",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]


def datastore_list_rows_after(ns_id: int, *, after_row_id: Optional[str] = None,
                              limit: int = 100, q: Optional[str] = None,
                              filters: Optional[list] = None) -> list[dict]:
    """Page **keyset** (curseur stable) triée par `row_id`. `row_id` est un uuid7 —
    monotone dans le temps de création — donc `ORDER BY row_id ASC` = ordre de
    création et `WHERE row_id > after_row_id` (borne EXCLUSIVE) enchaîne les pages
    sans dérive sous écritures concurrentes (contrairement à OFFSET, décalé par toute
    insertion). `after_row_id=None` = première page. `q`/`filters` = même filtrage
    SQL que `datastore_list_rows`. La clé est exacte (pas de troncature de timestamp,
    contrairement à un keyset sur `created_at` rendu à la seconde)."""
    where, params = _ds_where(ns_id, q, filters)
    if after_row_id:
        where += " AND row_id > %s"
        params.append(after_row_id)
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT row_id, created_at, updated_at, data, claimed_by, claimed_until, "
            "       claimed_run, claims, abandon_reason, "
            "       (claimed_until IS NOT NULL AND claimed_until > NOW())"
            "           AS claim_active "
            f"FROM datastore_rows {where} ORDER BY row_id ASC LIMIT %s",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]


def datastore_order_health(ns_id: int, *, order_by: str, order_type: str,
                           order_options: Optional[list] = None,
                           q: Optional[str] = None,
                           filters: Optional[list] = None) -> dict:
    """Les compteurs d'écart d'un tri typé (#336) : `{off_type, empty}` sur le
    même WHERE que la page — décision ① rendue à l'issue : les valeurs qu'on ne
    sait pas ranger vont en queue ET LA RÉPONSE LE DIT, au lieu d'enterrer
    l'écart sous un tri qui a l'air délibéré."""
    where, params = _ds_where(ns_id, q, filters)
    _v, _vp = field_read_sql(order_by)
    proj, pparams = order_health_sql(_v, _vp, order_type, order_options)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {proj} FROM datastore_rows {where}",
            tuple(pparams + params),
        ).fetchone()
    return {"off_type": int(row["off_type"] or 0), "empty": int(row["empty"] or 0)}


def datastore_count_rows(ns_id: int, q: Optional[str] = None,
                         filters: Optional[list] = None) -> int:
    """Nombre total de rows d'un namespace (pour la pagination), filtré par `q` et
    les filtres par colonne — même clause que `datastore_list_rows` → total cohérent
    avec la page affichée."""
    where, params = _ds_where(ns_id, q, filters)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM datastore_rows {where}", tuple(params)
        ).fetchone()
        return int(row["n"]) if row else 0



def datastore_aggregate(ns_id: int, *, group_by: Optional[str] = None,
                        metrics: Optional[list] = None, q: Optional[str] = None,
                        filters: Optional[list] = None, limit: int = 1000) -> list[dict]:
    """Agrégat serveur d'un namespace (feedback #191) : `COUNT/SUM/AVG/MIN/MAX` sur des
    champs JSONB, avec `group_by` optionnel — stats d'un gros vivier sans rapatrier les
    lignes. `group_by` = champ `data->>field` (None = agrégat global, une ligne).
    `metrics` = liste `{op, field?}`, op ∈ count|sum|avg|min|max (défaut `[{op:count}]`) ;
    `count` sans field = COUNT(*). sum/avg/min/max ne comptent que les valeurs
    NUMÉRIQUES (les non-numériques sont ignorées via un garde regex, jamais d'erreur de
    cast). Filtré par `q`/`filters` (même clause que list/count). Trié par la 1re métrique
    décroissante (« top … ») quand `group_by`. Renvoie `[{<group_by>: val, <metric>: n}]`
    (clés lisibles : `count`, `sum_<field>`, `avg_<field>`…)."""
    from decimal import Decimal
    sql, params, names = _build_aggregate(ns_id, group_by, metrics, q, filters, limit)
    with _connect() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    out = []
    cle = group_key(group_by)
    for r in rows:
        d: dict = {}
        if group_by:
            d[cle] = r["grp"]
        for alias, name in names:
            v = r[alias]
            d[name] = float(v) if isinstance(v, Decimal) else v
        out.append(d)
    return out


def datastore_update_row(ns_id: int, row_id: str, data: dict, updated_at: str) -> Optional[dict]:
    """Remplace `data` (le store a déjà fusionné le patch) + `updated_at`."""
    with _connect() as conn:
        row = conn.execute(
            "UPDATE datastore_rows SET data = %s::jsonb, updated_at = %s::timestamptz, "
            # cf. `datastore_upsert_row` : toute écriture de la ligne remet le
            # compteur de reprises à zéro et la rouvre à la file (#433).
            "       claims = 0, abandon_reason = NULL "
            "WHERE ns_id = %s AND row_id = %s "
            "RETURNING row_id, created_at, updated_at, data",
            (json.dumps(data), updated_at, ns_id, row_id),
        ).fetchone()
        from .search import stamp_rank_vector
        stamp_rank_vector(conn, "datastore_rows", "ns_id = %s AND row_id = %s", (ns_id, row_id))
        return dict(row) if row else None


def datastore_merge_row_locked(ns_id: int, row_id: str, apply_fn, updated_at: str,
                               lease_guard=None):
    """MERGE ATOMIQUE d'une row par son `row_id`, sous verrou de ligne (#197).

    Dans UNE transaction : verrouille la row (`SELECT … FOR UPDATE`), applique
    `apply_fn(current_data) -> merged` SOUS le verrou, puis écrit `merged`. Deux
    writes concurrents de la MÊME row (deux upserts de la même clé métier
    résolvent le même row_id via find_row_id_by_key) se **sérialisent** → plus de
    merge perdu : l'ancien `get_row` + merge Python + `update_row` sur deux
    connexions autocommit séparées était last-writer-wins (~30-35 % des merges
    écrasés sous forte concurrence). Renvoie `(row, merged)` ou `None` si la row
    n'existe plus (course de suppression). `apply_fn` peut lever (validation) →
    la transaction rollback, l'exception est propagée.

    `lease_guard` (#317) = contrôle du BAIL, appelé sous le verrou avec la ligne
    verrouillée (`claimed_by`/`claimed_until`/`claimed_run` inclus). Il lève pour
    refuser l'écriture — la transaction rollback, rien n'est écrit. Passé en
    paramètre plutôt que codé ici parce que « qui a le droit d'écrire » est une règle
    du STORE, pas du SQL : ce module ne connaît ni le run courant ni le worker.
    """
    with _connect() as conn:
        with conn.transaction():
            # Le bail est lu DANS le même verrou que la donnée (#317) : le lire
            # avant, sur une autre connexion, laisserait la fenêtre où un claim
            # s'intercale entre le contrôle et l'écriture — le défaut exact que
            # `FOR UPDATE` a été posé pour fermer sur `data` (#197).
            locked = conn.execute(
                "SELECT data, claimed_by, claimed_until, claimed_run "
                "FROM datastore_rows WHERE ns_id = %s AND row_id = %s FOR UPDATE",
                (ns_id, row_id),
            ).fetchone()
            if locked is None:
                return None
            if lease_guard is not None:
                lease_guard(locked)
            current = locked["data"]
            if not isinstance(current, dict):
                current = json.loads(current) if current else {}
            merged = apply_fn(current)
            row = conn.execute(
                "UPDATE datastore_rows SET data = %s::jsonb, updated_at = %s::timestamptz, "
                # cf. `datastore_upsert_row` : toute écriture de la ligne remet le
                # compteur de reprises à zéro et la rouvre à la file (#433).
                "       claims = 0, abandon_reason = NULL "
                "WHERE ns_id = %s AND row_id = %s "
                "RETURNING row_id, created_at, updated_at, data",
                (json.dumps(merged), updated_at, ns_id, row_id),
            ).fetchone()
            return dict(row), merged


def datastore_delete_row(ns_id: int, row_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id),
        )
        return (cur.rowcount or 0) > 0


# ── File de travail (ADR 0046 D) ─────────────────────────────────────────────
# Une row se « claim » avec un BAIL (claimed_by/claimed_until) : pick atomique de
# la prochaine row libre (bail NULL ou expiré) via FOR UPDATE SKIP LOCKED — deux
# workers concurrents ne prennent jamais la même row, sans sérialiser la table.
# Le bail expiré rend la row recyclable (worker mort ≠ row perdue). Libération :
# explicite (release, gardée par worker) ou automatique à l'entrée dans un état
# terminal du cycle de vie (côté store).
