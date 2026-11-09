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
from typing import TYPE_CHECKING, Any, Iterator, Optional

import psycopg

if TYPE_CHECKING:
    from ..datastore.recherche import Recherche

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
from ._conn import HorsPoolSature, _connect, _connect_autocommit
from .estampille import ecriture_de_lignes
# Construction des requêtes : extraite dans `query` (#325), ré-exportée ici pour que
# la surface plate `db.<fn>` et tous les appelants restent inchangés.
# Le tableau et sa propriété : extraits dans `datastore_ns` (#325), ré-exportés ici
# pour que la surface plate `db.<fn>` et tous les appelants restent inchangés.
from .datastore_ns import (  # noqa: F401
    AdresseAmbigue,
    best_resource_grants,
    count_datastore_rows_for_ns,
    create_datastore,
    datastore_name_taken,
    delete_datastore_by_id,
    get_datastore,
    get_datastore_by_id,
    get_datastore_owner,
    get_resource_grant,
    grant_resource,
    list_all_datastores,
    list_datastores_for_owners,
    list_datastores_granted_to,
    list_datastores_shared_to_user,
    list_resource_grants,
    manager_resource_grants,
    principals_with_live_grant,
    ranger_tableau,
    rename_datastore_by_id,
    reparent_datastore,
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
    datastore_compter_reservables,
    datastore_release_by_run,
    datastore_release_claim,
    datastore_row_within,
)
from .query import (  # noqa: F401
    COUNT_BY_VALUES_DEFAUT,
    COUNT_BY_VALUES_MAX,
    _build_aggregate,
    _build_count_by,
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
    thin_read_cte_sql,
    typed_order_sql,
)
from .users import upsert_user


def datastore_insert_row(ns_id: int, row_id: str, data: dict,
                         created_at: Optional[str] = None,
                         updated_at: Optional[str] = None) -> dict:
    """Insère une row. `created_at`/`updated_at` optionnels (override pour le
    backfill ; sinon NOW())."""
    with ecriture_de_lignes() as conn:
        row = conn.execute(
            "INSERT INTO datastore_rows (ns_id, row_id, data, created_at, updated_at, embed_dirty) "
            "VALUES (%s, %s, %s::jsonb, COALESCE(%s::timestamptz, NOW()), COALESCE(%s::timestamptz, NOW()), "
            # dirty ⟺ le namespace est opt-in sémantique (#67 V2.2) — sinon jamais embedé.
            "        (SELECT semantic_search FROM user_datastores WHERE id = %s)) "
            "RETURNING row_id, created_at, updated_at, data, rev",
            (ns_id, row_id, json.dumps(data), created_at, updated_at, ns_id),
        ).fetchone()
        from .search import stamp_rank_vector
        stamp_rank_vector(conn, "datastore_rows", "ns_id = %s AND row_id = %s", (ns_id, row_id))
        return dict(row)


def datastore_find_row_id_by_key(ns_id: int, key_field: str, key_value) -> Optional[str]:
    """Trouve le `row_id` d'une row par une CLÉ MÉTIER, pour la dédup d'un batch
    write. Renvoie le plus ancien match (ordre stable) ou None.

    ⚠️ L'expression vient de `bkey_index_expr` — LA MÊME que celle de l'index, à la
    chaîne près. Le planner ne sert un index d'EXPRESSION que si le `WHERE` porte
    exactement la sienne : un écart ne casserait rien de visible (la déduplication
    marcherait) et ferait simplement partir chaque lookup en seq scan. C'est la panne
    qu'on ne voit qu'au moment où le namespace est assez gros pour qu'elle coûte.

    `key_value` = la valeur DÉBALLÉE (`unwrap`). Son texte est rendu par la BASE
    (`#>> '{}'` sur son JSON), comme `data->>k` rend celui de la case : `true` et non
    `True`, `5.0` et non une conversion Python (oto#223). Une chaîne rend son texte
    nu, un nombre et `"5"` se retrouvent, comme dans l'index.

    ⚠️ **`ns_id` est un LITTÉRAL, pas un paramètre** (oto#225), comme dans le prédicat
    de l'index partiel (`WHERE ns_id = <littéral> …`). psycopg prépare côté serveur une
    requête répétée sur une connexion ; PostgreSQL peut alors retenir un plan GÉNÉRIQUE,
    où un `ns_id` paramétré ne prouve plus le prédicat : l'index n'est plus empruntable
    et le lookup lit tout le tableau. Mesuré sur PostgreSQL 17, 50 000 lignes : 0,01 ms
    et 3 blocs par l'index, 29 ms et 962 blocs sans. Le basculement n'est pas théorique :
    la requête préparée est partagée, sur une connexion, par tous les tableaux dont la
    clé porte le même nom, et cinq lookups sur un grand tableau SANS index (clé `key=`
    non déclarée, index manquant) l'ont provoqué pour les suivants (mesuré). Le littéral
    rend le plan générique aussi bon que le personnalisé. `int()` : jamais une saisie."""
    from psycopg import sql as _sql
    q = _sql.SQL(
        "SELECT row_id FROM datastore_rows WHERE ns_id = {ns} AND {e} = (%s::jsonb #>> '{{}}') "
        "ORDER BY created_at ASC LIMIT 1"
    ).format(ns=_sql.Literal(int(ns_id)), e=bkey_index_expr(key_field))
    with _connect() as conn:
        row = conn.execute(q, (json.dumps(key_value),)).fetchone()
        return row["row_id"] if row else None


def datastore_textes_de_cle(valeurs: list) -> list[str]:
    """Le texte de chaque valeur de clé métier DÉBALLÉE, rendu par la BASE — pour juger
    si deux lignes d'un même lot portent « la même clé » (oto#141) sans tenir en Python
    une seconde règle de comparaison. C'est la conversion que le lookup applique à la
    valeur cherchée (oto#223 : `%s::jsonb #>> '{}'`, `true` et non `True`), en une
    requête pour tout le lot."""
    if not valeurs:
        return []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT e.v #>> '{}' AS t FROM jsonb_array_elements(%s::jsonb) "
            "WITH ORDINALITY AS e(v, i) ORDER BY e.i",
            (json.dumps(list(valeurs)),)).fetchall()
    return [r["t"] for r in rows]


# ── Clé métier = contrainte (#109 ch.3) ──────────────────────────────────────
# Quand `schema.key` est déclarée, elle cesse d'être purement applicative : un
# index UNIQUE PARTIEL par namespace (`ds_bkey_<ns_id>`, expression `bkey_index_expr`
# — la règle de lecture depuis oto#223 —, prédicat ns_id littéral + clé non nulle)
# rend la dédup concurrent-safe (deux writes parallèles du même member_id ⇒ le
# perdant prend une UniqueViolation, convertie en update par le store) et le lookup
# indexé, en plan personnalisé comme générique (ns_id littéral aussi, oto#225). Cycle de vie : posé/déposé par
# `set_schema` (source unique de schema.key) + migration boot pour l'existant.

def _bkey_index_name(ns_id: int) -> str:
    return f"ds_bkey_{int(ns_id)}"


def datastore_key_dup_groups(ns_id: int, key: str, limit: int = 10) -> list[dict]:
    """Valeurs de clé métier en DOUBLON dans les rows existantes — `[{value, n}]`,
    plus gros groupes d'abord. Sert le refus actionnable de `set_schema` (on ne
    pose pas un UNIQUE sur des données sales sans le dire).

    Comparées sur `bkey_index_expr`, l'expression MÊME de l'index (oto#223) : un
    doublon vu ici est exactement ce qui ferait échouer sa pose, et rien d'autre."""
    from psycopg import sql as _sql
    q = _sql.SQL(
        "SELECT {e} AS value, COUNT(*) AS n FROM datastore_rows "
        "WHERE ns_id = %s AND {e} IS NOT NULL "
        "GROUP BY 1 HAVING COUNT(*) > 1 ORDER BY n DESC, 1 LIMIT %s"
    ).format(e=bkey_index_expr(key))
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



def datastore_rows_missing_required(ns_id: int, champs: list) -> list[dict]:
    """Lignes DÉJÀ EN BASE qui ne portent pas un champ fraîchement déclaré
    `required` — `[{field, rows}]`, le plus large d'abord (oto-backend#284).

    ⚠️ **Ce comptage n'a pas la même portée que ses voisins.** Une borne ou un motif
    posés après coup ne gèlent rien : ils ne jugent que les clés qu'un geste ÉCRIT,
    donc un patch d'un autre champ passe. Un champ REQUIS, lui, est vérifié sur la
    ligne entière : tant qu'il manque, **plus aucune écriture n'aboutit sur cette
    ligne**, quelle que soit la colonne visée. C'est ce que compte cette fonction —
    non pas des lignes non conformes, mais des lignes devenues inécrivables.

    Une valeur vide (`''`) compte comme absente : c'est ce que le contrôle de
    `required` refuse aussi, et compter autrement annoncerait moins de lignes
    bloquées qu'il n'y en a.
    """
    from psycopg import sql as _sql
    out: list[dict] = []
    with _connect() as conn:
        for champ in champs or []:
            # oto#204 : une cellule au vide ASSUMÉ satisfait `required` — elle n'est pas
            # manquante, et la compter annoncerait des lignes bloquées qui ne le sont pas.
            from ..datastore.couches import VIDE_ASSUME
            q = _sql.SQL(
                "SELECT COUNT(*) AS rows FROM datastore_rows "
                "WHERE ns_id = %s AND ({v} IS NULL OR {v} = '') "
                "AND (data -> %s ->> %s) IS DISTINCT FROM 'true'"
            ).format(v=field_value_sql(champ))
            r = conn.execute(q, (ns_id, champ, VIDE_ASSUME)).fetchone()
            if r and (r["rows"] or 0) > 0:
                out.append({"field": champ, "rows": int(r["rows"])})
    return sorted(out, key=lambda d: d["rows"], reverse=True)


def datastore_offending_enum_values(ns_id: int, options: dict,
                                    per_field: int = 5) -> list[dict]:
    """Valeurs DÉJÀ EN BASE qu'un enum fraîchement déclaré condamne —
    `[{field, values: [{value, rows}], rows}]`, le plus atteint d'abord.

    Un schéma ne vaut que pour l'AVENIR : le poser ne revalide pas l'existant. Une
    colonne peut donc être pleine de valeurs que le format refuse désormais, sans
    que rien ne le dise — et le tableau *a l'air* conforme puisqu'il a un schéma.
    Vécu : 504 lignes en « Oui »/« Non » sur un enum `oui`/`non`/`inconnu`. Les
    lectures, elles, les voient telles quelles (filtres, regroupement, comptes :
    aucune ne consulte les `options`, oto#218).

    On rend les valeurs FAUTIVES avec leur compte, pas un simple total : c'est ce
    qui permet de trancher tout de suite entre corriger la donnée et élargir les
    options. Les vides (`NULL`, chaîne vide) sont écartés — une case non remplie
    n'est pas une valeur hors options, c'est l'affaire de `required`."""
    from psycopg import sql as _sql
    from ..datastore.options_declarees import valeur_comparee
    out: list[dict] = []
    with _connect() as conn:
        for field, allowed in (options or {}).items():
            # La forme comparée, celle du refus d'écriture (`options_declarees.parmi`).
            vals = [v for v in map(valeur_comparee, allowed or []) if v is not None]
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
    with ecriture_de_lignes() as conn:
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


def datastore_rows_data_after(ns_id: int, *, after_row_id: Optional[str] = None,
                              limit: int = 500) -> list[dict]:
    """Page **keyset** `[{row_id, data}]` triée par `row_id`, sans rien d'autre.

    Sert le relevé des lignes existantes à la pose d'un schéma (oto-backend#479), qui
    juge chaque ligne avec le moteur de validation : il ne lit que la donnée, donc la
    page ne porte ni réservation ni horodatage. Même curseur que
    `datastore_list_rows_after` (borne EXCLUSIVE sur l'uuid7)."""
    params: list = [ns_id]
    where = "ns_id = %s"
    if after_row_id:
        where += " AND row_id > %s"
        params.append(after_row_id)
    params.append(int(limit))
    with _connect() as conn:
        return [{"row_id": str(r["row_id"]), "data": r["data"] or {}}
                for r in conn.execute(
                    f"SELECT row_id, data FROM datastore_rows WHERE {where} "
                    "ORDER BY row_id ASC LIMIT %s", tuple(params)).fetchall()]


def datastore_merge_key_duplicates(ns_id: int, key: str) -> int:
    """Résorbe les doublons de clé métier en reconstituant la sémantique upsert :
    pour chaque valeur en doublon, MERGE les `data` dans l'ordre chronologique dans
    la row la plus ANCIENNE (celle que `find_row_id_by_key` aurait servie à chaque
    write), puis supprime les plus récentes. Renvoie le nombre de rows supprimées.
    Une transaction par groupe (échec isolé, jamais de demi-merge).

    Groupées sur `bkey_index_expr`, l'expression de l'index (oto#223) : une case qui
    ne sert aucune valeur ne fusionne avec rien, et une clé nue fusionne avec la même
    clé enveloppée — ce que l'index, posé juste après, refuserait sinon."""
    from psycopg import sql as _sql
    removed = 0
    expr = bkey_index_expr(key)
    dup_q = _sql.SQL(
        "SELECT {e} AS value FROM datastore_rows "
        "WHERE ns_id = %s AND {e} IS NOT NULL GROUP BY 1 HAVING COUNT(*) > 1"
    ).format(e=expr)
    rows_q = _sql.SQL(
        "SELECT row_id, data FROM datastore_rows WHERE ns_id = %s AND {e} = %s "
        "ORDER BY created_at ASC, row_id ASC"
    ).format(e=expr)
    with _connect() as conn:
        values = [r["value"] for r in conn.execute(dup_q, (ns_id,)).fetchall()]
    for value in values:
        with ecriture_de_lignes() as conn:
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


class KeyIndexStillEnforced(RuntimeError):
    """Le RETRAIT de l'index d'unicité n'a pas abouti dans sa borne — il est encore là.

    Sœur exacte de `KeyIndexUnavailable`, et son contraire : là, une contrainte
    manquait ; ici une contrainte SURVIT à la clé qui la déclarait. La conséquence
    n'est pas symétrique — un index qu'aucun schéma ne déclare refuse des écritures
    au nom d'une clé que `data_get_schema` ne montre plus. Personne ne peut relier
    le refus à sa cause.

    Elle existe pour la même raison que sa sœur : le schéma est déjà écrit quand le
    retrait coupe, donc l'appelant doit pouvoir le DIRE."""


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
    try:
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
            except Exception as e:
                # ⚠️ **On rattrape TOUTE interruption, pas une liste de causes.** Un
                # `CREATE INDEX CONCURRENTLY` coupé laisse son index INVALIDE derrière lui
                # — et un unique invalide peut continuer d'imposer sa contrainte aux
                # écritures suivantes. On le retire tout de suite : le laisser ferait
                # refuser des écritures au nom d'un index que personne ne sait nommer.
                #
                # ⚠️ **Ce bloc ne listait que deux causes, et il en manquait une vivante.**
                # Constaté en production le 07/09/2026 à 23:40 UTC : un `DeadlockDetected`
                # sur cette route. Il n'est PAS une sous-classe de `LockNotAvailable` ni de
                # `QueryCanceled` — ce sont trois sœurs sous `OperationalError` — donc il
                # traversait : l'appelant recevait une erreur interne, **et surtout le
                # nettoyage ne tournait pas**. L'index invalide restait.
                #
                # La leçon est dans la FORME, pas dans la cause manquante : l'invariant
                # est « toute interruption laisse un index à retirer », et une liste
                # d'exceptions ne peut pas exprimer un invariant — elle ne peut
                # qu'énumérer ce qu'on a rencontré jusqu'ici. Le nettoyage est donc
                # inconditionnel ; seul le MESSAGE distingue les causes.
                #
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

                if not isinstance(e, _POSE_INTERROMPUE):
                    # Pas une contention : une vraie panne. Le nettoyage a eu lieu, et
                    # l'erreur remonte telle quelle — la traduire en « réessaie plus tard »
                    # ferait attendre un tir suivant qui échouera pareil.
                    raise

                # La cause tient en un mot, et il change ce que l'appelant doit en penser :
                # une transaction qui retenait le geste se résorbe seule, un interblocage
                # dit que deux poses se sont croisées.
                pourquoi = ("deux poses de schéma se sont croisées"
                            if isinstance(e, psycopg.errors.DeadlockDetected)
                            else "une transaction ouverte le retenait")
                raise KeyIndexUnavailable(
                    f"index d'unicité de `{key}` : la base ne l'a pas laissé se poser dans "
                    f"sa borne — {pourquoi}. Le schéma EST écrit et "
                    "la clé reste rapprochée à l'écriture ; c'est la garantie anti-course "
                    "qui manque, et la maintenance la repose au tir suivant.") from e
    except HorsPoolSature as e:
        # Plus de place hors pool (#1141) : même issue qu'une borne qui coupe —
        # le schéma est écrit, la maintenance reprend le geste.
        raise KeyIndexUnavailable(
            f"index d'unicité de `{key}` : aucune connexion de DDL n'était libre "
            "pour le poser. Le schéma EST écrit et la clé reste rapprochée à "
            "l'écriture ; la maintenance repose l'index au tir suivant.") from e


#: Les interruptions qui disent « la base n'a pas pu poser l'index MAINTENANT »,
#: par opposition à une vraie panne. Les trois sont sœurs sous `OperationalError` —
#: aucune n'attrape les autres, et c'est ce qui a laissé passer l'interblocage.
_POSE_INTERROMPUE = (
    psycopg.errors.LockNotAvailable,
    psycopg.errors.QueryCanceled,
    psycopg.errors.DeadlockDetected,
)


def datastore_drop_key_index(ns_id: int, *, bornee: bool = True) -> None:
    """Dépose l'index d'unicité — ET le temporaire `_v2` qu'une pose coupée aurait
    pu laisser (sinon un unique orphelin continue de refuser des écritures pour une
    clé qui n'est plus déclarée).

    ⚠️ **Borné comme la pose, et pour la même raison** (oto#82). Ce geste tournait sur
    le pool de requête, où `lock_timeout` n'existe pas et `statement_timeout` vaut 0 :
    mesuré **5,72 s d'attente derrière une simple lecture de 6 s**, borne réglée à
    300 ms et ignorée. Et l'attente ne retient pas que l'appelant — `DROP INDEX`
    réclame un `AccessExclusiveLock` sur `datastore_rows`, table COMMUNE à tous les
    tableaux, et une demande de verrou exclusif se met en file DEVANT les lecteurs et
    écrivains suivants. Un tableau qui perd sa clé arrêtait donc le voisinage.

    Lève `KeyIndexStillEnforced` quand la borne coupe : l'index est encore là, et le
    schéma qui ne le déclare plus est déjà écrit. `bornee=False` pour le travail de
    FOND, qui a le droit d'attendre son tour."""
    from psycopg import sql as _sql
    name = _bkey_index_name(ns_id)
    try:
        with _connect_autocommit(bornee=bornee) as conn:
            try:
                conn.execute(_sql.SQL("DROP INDEX IF EXISTS {n}").format(
                    n=_sql.Identifier(name)))
                conn.execute(_sql.SQL("DROP INDEX IF EXISTS {t}").format(
                    t=_sql.Identifier(name + "_v2")))
            except _POSE_INTERROMPUE as e:
                # Même forme que la pose : on ne traduit QUE la contention. Une vraie panne
                # remonte telle quelle — annoncer « réessaie » ferait attendre un tir
                # suivant qui échouera pareil.
                pourquoi = ("pris dans un interblocage"
                            if isinstance(e, psycopg.errors.DeadlockDetected)
                            else "retenu par une transaction ouverte")
                raise KeyIndexStillEnforced(
                    f"l'ancienne contrainte d'unicité du tableau est TOUJOURS en place "
                    f"(son retrait a été {pourquoi}) : une écriture que le nouveau schéma "
                    f"autorise peut encore être refusée au nom de la clé retirée. Repose "
                    f"le même schéma pour réessayer le retrait — la maintenance, elle, ne "
                    f"balaie que les index MANQUANTS.") from e
    except HorsPoolSature as e:
        # Plus de place hors pool (#1141) : même issue qu'une borne qui coupe — et
        # la maintenance ne reprend pas un retrait, c'est l'appelant qui repose.
        raise KeyIndexStillEnforced(
            "l'ancienne contrainte d'unicité du tableau est TOUJOURS en place : "
            "aucune connexion de DDL n'était libre pour la retirer. Repose le même "
            "schéma pour réessayer le retrait.") from e


def datastore_has_key_index(ns_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute("SELECT 1 FROM pg_indexes WHERE indexname = %s",
                           (_bkey_index_name(ns_id),)).fetchone()
        return row is not None


def datastores_with_key() -> list[dict]:
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

    Chaque contenu porte `_revision` (12/09/2026), la révision de la ligne en chaîne,
    comme toute ligne servie : `_revision` est une colonne de plateforme
    (`_META_COLS`), aucun champ utilisateur ne peut porter ce nom.
    """
    ids = [str(r) for r in (row_ids or []) if r][:_DS_MAX_ROWS_BY_IDS]
    if not ids:
        return {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT row_id, data, rev FROM datastore_rows "
            "WHERE ns_id = %s AND row_id = ANY(%s)",
            (ns_id, ids),
        ).fetchall()
        return {r["row_id"]: {**(r["data"] or {}), "_revision": str(r["rev"])}
                for r in rows}


def datastore_get_row(ns_id: int, row_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT row_id, created_at, updated_at, data, rev, claimed_by, claimed_until, "
            "       claimed_run, claims, abandon_reason, abandon_run, "
            "       (claimed_until IS NOT NULL AND claimed_until > NOW())"
            "           AS claim_active "
            "FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id),
        ).fetchone()
        return dict(row) if row else None


# Filtres par colonne (vue tableau dashboard, oto-dashboard#18). Chaque filtre =
# {field, op, value}. Le champ est TOUJOURS paramétré (`data ->> %s`) et l'op tiré
# d'une whitelist → fragment SQL fixe, zéro interpolation de valeur = pas d'injection.


# Une page de lignes voyage en UN SEUL message PostgreSQL (oto-backend#980, suite).
#
# Ligne à ligne, libpq lit le résultat par tranches de ~10-16 Ko (son tampon ne grandit
# que pour un message plus gros que lui) et psycopg rend le GIL à CHAQUE tranche pour
# attendre la socket : ~260 attentes pour une page de 500 lignes de ~4 Ko. Seul, c'est
# gratuit ; dès qu'un autre thread du processus calcule, chaque reprise du GIL attend
# jusqu'à `sys.getswitchinterval()` — le convoi de `docs/event-loop-perf.md`. Mesuré en
# local, un ramassage complet (8 900 lignes × 90 colonnes) passait de ~2 s à ~11 s à
# côté d'un seul thread de calcul, intervalle à 1 ms compris.
#
# Agrégée côté base en un seul `json`, la page est un message long : libpq agrandit son
# tampon et enchaîne ses lectures sans rendre la main — tant que le transport livre le
# message d'un bloc ; par rafales, une attente par rafale (cf. le banc, qui vérifie le
# protocole et non ce compte). Même forme de ligne que `_str_dict_row` : horodatages
# en `AAAA-MM-JJ HH:MM:SS` dans le fuseau de la session, secondes tronquées, `data`
# décodé à l'identique.
#
# ⚠️ Pour une PAGE seulement (`limit` posé) : un ramassage sans borne en un message
# ferait décoder tout le tableau d'un bloc, GIL tenu d'un bout à l'autre.
_HORODATAGE = "'YYYY-MM-DD HH24:MI:SS'"
_LIGNE_JSON = (
    "json_build_object("
    "'row_id', t.row_id, "
    f"'created_at', to_char(t.created_at, {_HORODATAGE}), "
    f"'updated_at', to_char(t.updated_at, {_HORODATAGE}), "
    "'data', t.data, 'rev', t.rev, 'claimed_by', t.claimed_by, "
    f"'claimed_until', to_char(t.claimed_until, {_HORODATAGE}), "
    "'claimed_run', t.claimed_run, 'claims', t.claims, "
    "'abandon_reason', t.abandon_reason, 'abandon_run', t.abandon_run, "
    "'claim_active', t.claim_active)"
)


def _page_en_un_message(conn, sql: str, params: tuple, *, stats: str = "") -> dict:
    """Les lignes de `sql` — qui porte les colonnes de `_LIGNE_JSON` et leur rang
    `rn` — lues en UN message, dans l'ordre de `rn`, sous `lignes`. Un rang nul
    (page vide sous une ligne de statistiques) n'est pas une ligne. `stats` : des
    agrégats de plus sur les mêmes lignes, rendus à côté."""
    return conn.execute(
        f"SELECT {stats}coalesce(json_agg({_LIGNE_JSON} ORDER BY t.rn) "
        f"FILTER (WHERE t.rn IS NOT NULL), '[]') AS lignes FROM ({sql}) t",
        params,
    ).fetchone()


def datastore_list_rows(ns_id: int, *, offset: int = 0, limit: Optional[int] = None,
                        order_by: Optional[str] = None, order_dir: str = "desc",
                        q: Optional["Recherche"] = None, filters: Optional[list] = None,
                        order_type: Optional[str] = None,
                        order_options: Optional[list] = None) -> list[dict]:
    """Page de rows d'un namespace. `order_by` : `_created_at`/`_updated_at`/`_id`
    (colonnes méta) ou un nom de champ user → `data->>field`. `q` : la recherche
    validée (`datastore.recherche.Recherche`) — chaque mot par le sens OU par
    fragment (#307). SANS `order_by`, elle CLASSE : rang lexical d'abord, les lignes
    trouvées seulement par fragment ensuite, puis l'ordre de création (`order_dir`) ;
    un `order_by` explicite, `_created_at` compris, garde la main.
    `filters` : filtres par
    colonne (liste `{field, op, value}`, combinés AND — cf. `_ds_filter_clauses`).
    Tri/pagination/recherche/filtres côté SQL (server-side, ADR 0016). `limit=None`
    = toutes les rows (compat `store.list_rows` / MCP `data_rows`).

    `order_type`/`order_options` (#336) : le TYPE DÉCLARÉ du champ trié
    (`number`/`enum`/`date`), résolu PAR LE STORE — cette couche ne connaît pas
    le schéma et ne doit pas le connaître, elle reçoit un type générique et
    construit l'expression (cf. `typed_order_sql`). None = tri textuel historique.

    ⚠️ Un tri TYPÉ sur une colonne SIMPLE (et/ou des `filters` sur des colonnes
    SIMPLES) passe par `thin_read_cte_sql` (oto-backend#980) : `typed_order_sql`
    relit `data` 56 fois par ligne et un filtre autant de fois qu'il est évalué,
    et PostgreSQL le détoaste à chaque référence — mesuré à 924 000 blocs lus
    pour une page de 50 lignes triées sur un vivier réel, et 237 000 pour une
    page filtrée sans tri. La CTE mince ramène chacun à une lecture par ligne."""
    direction = "ASC" if str(order_dir).lower() == "asc" else "DESC"
    order_simple = order_by not in (None, "", "_created_at", "_updated_at", "_id")
    thin = thin_read_cte_sql(ns_id, q, filters,
                             order_by if (order_type and order_simple) else None)
    if thin is not None and (not order_simple or order_type):
        cte_sql, cte_params, where_sql, where_params = thin
        tail = ""
        tail_params: list = []
        if limit is not None:
            tail = " LIMIT %s OFFSET %s"
            tail_params = [limit, offset]
        if order_type and order_simple:
            _v, _vp = field_read_sql(order_by)
            order_sql, _op = typed_order_sql(_v, _vp, order_type, order_options, direction)
        else:
            # Amincissement porté par les SEULS `filters` (`order_by` est une
            # colonne MÉTA ici, jamais dans `data`, ou absent) : même ordre que
            # le chemin historique. `created_at`/`updated_at` sont portées par
            # `s` elle-même (cf. `thin_read_cte_sql`) — jamais `dr` à ce stade,
            # sous peine d'un `data` ambigu (`s.data` mince vs `dr.data` entier
            # dans la même portée).
            if order_by == "_updated_at":
                order_sql, _op = f"updated_at {direction}, row_id {direction}", []
            elif order_by == "_id":
                order_sql, _op = f"row_id {direction}", []
            else:
                order_sql, _op = f"created_at {direction}, row_id {direction}", []
        p_params = list(_op) + list(where_params)
        with _connect() as conn:
            sql = (
                f"WITH {cte_sql}, "
                f"p AS (SELECT row_id, row_number() OVER (ORDER BY {order_sql}) AS rn "
                f"FROM s {where_sql} ORDER BY rn{tail}) "
                "SELECT p.rn, dr.row_id, dr.created_at, dr.updated_at, dr.data, dr.rev, "
                "       dr.claimed_by, dr.claimed_until, dr.claimed_run, dr.claims, "
                "       dr.abandon_reason, dr.abandon_run, "
                "       (dr.claimed_until IS NOT NULL AND dr.claimed_until > NOW())"
                "           AS claim_active "
                # ⚠️ `row_id` n'est PAS globalement unique (TEXT scopé par
                # `ns_id`, jamais une clé primaire à lui seul) : un JOIN qui ne
                # filtrerait QUE sur `row_id` fusionnerait des lignes HOMONYMES
                # d'AUTRES tableaux (vu en test : `datastore_rows` porte le même
                # `row_id` littéral dans N namespaces). `ns_id` DOIT faire partie
                # de la condition de jointure, pas seulement de la CTE `s`.
                "FROM p JOIN datastore_rows dr "
                "  ON dr.row_id = p.row_id AND dr.ns_id = %s "
                "ORDER BY p.rn")
            params_ = tuple(cte_params + p_params + tail_params + [ns_id])
            if limit is not None:
                return _page_en_un_message(conn, sql, params_)["lignes"]
            return [{k: v for k, v in r.items() if k != "rn"}
                    for r in conn.execute(sql, params_).fetchall()]
    where, params = _ds_where(ns_id, q, filters)
    if order_by in (None, "") and q:
        from .search import rang_des_lignes_sql  # lazy : search → projects → datastore
        rang, rparams = rang_des_lignes_sql(q)
        order_sql = f"{rang} DESC, created_at {direction}, row_id {direction}"
        params.extend(rparams)
    elif order_by in (None, "", "_created_at"):
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
    cols = ("row_id, created_at, updated_at, data, rev, claimed_by, claimed_until, "
            "claimed_run, claims, abandon_reason, abandon_run, "
            "(claimed_until IS NOT NULL AND claimed_until > NOW()) AS claim_active")
    with _connect() as conn:
        if limit is not None:
            # Le rang suit `order_sql` via une clause `WINDOW`, placée APRÈS le
            # `WHERE` : les paramètres positionnels gardent leur ordre (filtres, tri,
            # bornes), et l'expression de tri n'est évaluée qu'une fois.
            return _page_en_un_message(
                conn,
                f"SELECT {cols}, row_number() OVER w AS rn "
                f"FROM datastore_rows {where} WINDOW w AS (ORDER BY {order_sql}) "
                f"ORDER BY rn{tail}",
                tuple(params))["lignes"]
        rows = conn.execute(
            f"SELECT {cols} FROM datastore_rows {where} ORDER BY {order_sql}",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]


def datastore_list_rows_after(ns_id: int, *, after_row_id: Optional[str] = None,
                              limit: int = 100,
                              filters: Optional[list] = None) -> list[dict]:
    """Page **keyset** (curseur stable) triée par `row_id`. `row_id` est un uuid7 —
    monotone dans le temps de création — donc `ORDER BY row_id ASC` = ordre de
    création et `WHERE row_id > after_row_id` (borne EXCLUSIVE) enchaîne les pages
    sans dérive sous écritures concurrentes (contrairement à OFFSET, décalé par toute
    insertion). `after_row_id=None` = première page. `filters` = même filtrage
    SQL que `datastore_list_rows`. Pas de `q` : une recherche CLASSE (#307), elle
    passe par `datastore_list_rows` et son offset — un rang n'est pas une clé keyset. La clé est exacte (pas de troncature de timestamp,
    contrairement à un keyset sur `created_at` rendu à la seconde)."""
    where, params = _ds_where(ns_id, None, filters)
    if after_row_id:
        where += " AND row_id > %s"
        params.append(after_row_id)
    params.append(limit)
    with _connect() as conn:
        return _page_en_un_message(
            conn,
            "SELECT row_id, created_at, updated_at, data, rev, claimed_by, claimed_until, "
            "       claimed_run, claims, abandon_reason, abandon_run, "
            "       (claimed_until IS NOT NULL AND claimed_until > NOW())"
            "           AS claim_active, "
            "       row_number() OVER (ORDER BY row_id ASC) AS rn "
            f"FROM datastore_rows {where} ORDER BY row_id ASC LIMIT %s",
            tuple(params))["lignes"]


def datastore_page_mince(ns_id: int, *, offset: int = 0, limit: int,
                         order_by: Optional[str] = None, order_dir: str = "desc",
                         filters: Optional[list] = None) -> list[dict]:
    """Une page triée sur une colonne MÉTA (`_created_at` par défaut, `_updated_at`,
    `_id`), sans recherche : `LIMIT`/`OFFSET` sur les seules clés, sans fonction de
    fenêtre. Mêmes lignes, même ordre, même forme que `datastore_list_rows`.

    La forme historique classe TOUT le jeu (`row_number() OVER … ORDER BY rn`) avant
    de couper : un tri complet de lignes entières, quel que soit `limit`. Ici la
    coupe se fait sur `(clé de tri, row_id)` — tri borné à `offset + limit`, et
    arrêt au fil de la clé primaire pour `_id` —, puis seules les lignes retenues
    sont relues. Le rang que `_page_en_un_message` attend vient de `WITH ORDINALITY`
    sur le tableau ordonné des clés, pas d'une fenêtre.

    `filters` sont posés tels quels (`_ds_where`) : des filtres AMINCISSABLES passent
    par `datastore_page_with_stats`, qui ne relit `data` qu'une fois par ligne."""
    direction = "ASC" if str(order_dir).lower() == "asc" else "DESC"
    if order_by in (None, "", "_created_at"):
        order_sql = f"created_at {direction}, row_id {direction}"
    elif order_by == "_updated_at":
        order_sql = f"updated_at {direction}, row_id {direction}"
    elif order_by == "_id":
        order_sql = f"row_id {direction}"
    else:
        raise ValueError(
            f"datastore_page_mince: order_by={order_by!r} is not a meta column "
            "(`_created_at`, `_updated_at`, `_id`) — use `datastore_list_rows`")
    where, params = _ds_where(ns_id, None, filters)
    sql = (
        "SELECT p.rn, dr.row_id, dr.created_at, dr.updated_at, dr.data, dr.rev, "
        "dr.claimed_by, dr.claimed_until, dr.claimed_run, dr.claims, dr.abandon_reason, "
        "dr.abandon_run, "
        "(dr.claimed_until IS NOT NULL AND dr.claimed_until > NOW()) AS claim_active "
        f"FROM unnest(ARRAY(SELECT row_id FROM datastore_rows {where} "
        f"ORDER BY {order_sql} LIMIT %s OFFSET %s)) WITH ORDINALITY AS p(row_id, rn) "
        # ⚠️ `ns_id` dans la jointure : `row_id` n'est unique que par tableau.
        "JOIN datastore_rows dr ON dr.row_id = p.row_id AND dr.ns_id = %s")
    with _connect() as conn:
        return _page_en_un_message(
            conn, sql, tuple(params + [limit, offset, ns_id]))["lignes"]


def datastore_mark_formula_dirty(ns_id: int) -> int:
    """Marque TOUTES les rows d'un namespace `formula_dirty` (oto-backend#1008 v2) —
    déclenché quand `set_schema` pose ou modifie une formule. Un seul `UPDATE` de
    masse (rapide, aucun verrou par ligne) plutôt que le recalcul synchrone : le
    worker (`formula_backfill_worker.py`) drainera hors du chemin d'appel. Renvoie
    le nombre de rows marquées."""
    with _connect() as conn:
        return conn.execute(
            "UPDATE datastore_rows SET formula_dirty = TRUE WHERE ns_id = %s",
            (ns_id,)).rowcount or 0


def datastore_formula_dirty_count(ns_id: int) -> int:
    """Combien de rows restent à recalculer — le statut consultable (oto-backend#1008
    v2) : lu via l'index partiel `idx_datastore_rows_formula_dirty`, un `COUNT`
    borné aux seules rows dirty, jamais un balayage de la table entière."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM datastore_rows WHERE ns_id = %s AND formula_dirty",
            (ns_id,)).fetchone()
        return int(row["n"] or 0)


def datastore_namespaces_avec_formule_dirty(limit: int = 20) -> list[int]:
    """Les `ns_id` distincts qui ont au moins une row `formula_dirty` — l'outbox
    lue par le worker, un tour à la fois (patron `rank_backfill_worker.py`)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ns_id FROM datastore_rows WHERE formula_dirty LIMIT %s",
            (limit,)).fetchall()
        return [int(r["ns_id"]) for r in rows]


def datastore_list_formula_dirty(ns_id: int, limit: int) -> list[dict]:
    """Une tranche de rows `formula_dirty` d'UN namespace — pagée par `row_id`
    (déterministe, mais PAS un curseur keyset entre tours : chaque tour repart du
    début, puisque les rows traitées sortent du lot en clairant leur drapeau)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT row_id, data FROM datastore_rows "
            "WHERE ns_id = %s AND formula_dirty ORDER BY row_id ASC LIMIT %s",
            (ns_id, limit)).fetchall()
        return [dict(r) for r in rows]


def datastore_clear_formula_dirty(ns_id: int, row_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE datastore_rows SET formula_dirty = FALSE "
            "WHERE ns_id = %s AND row_id = %s", (ns_id, row_id))


def datastore_clear_formula_dirty_ns(ns_id: int) -> None:
    """Namespace sans schéma (retiré entre le marquage et le tour du worker) ou
    sans plus aucune colonne formule : rien à calculer, on baisse le drapeau sans
    y toucher ligne par ligne."""
    with _connect() as conn:
        conn.execute(
            "UPDATE datastore_rows SET formula_dirty = FALSE WHERE ns_id = %s",
            (ns_id,))


def datastore_order_health(ns_id: int, *, order_by: str, order_type: str,
                           order_options: Optional[list] = None,
                           q: Optional["Recherche"] = None,
                           filters: Optional[list] = None) -> dict:
    """Les compteurs d'écart d'un tri typé (#336) : `{off_type, empty}` sur le
    même WHERE que la page — décision ① rendue à l'issue : les valeurs qu'on ne
    sait pas ranger vont en queue ET LA RÉPONSE LE DIT, au lieu d'enterrer
    l'écart sous un tri qui a l'air délibéré.

    ⚠️ Même détoastage répété que `datastore_list_rows` (oto-backend#980) :
    `order_health_sql` relit `data` 35 fois par ligne (et chaque filtre, à
    chaque référence). Passe par la même CTE mince quand `order_by`/`filters`
    s'y prêtent (`thin_read_cte_sql`)."""
    thin = thin_read_cte_sql(ns_id, q, filters, order_by)
    if thin is not None:
        cte_sql, cte_params, where_sql, where_params = thin
        _v, _vp = field_read_sql(order_by)
        proj, pparams = order_health_sql(_v, _vp, order_type, order_options)
        with _connect() as conn:
            row = conn.execute(
                f"WITH {cte_sql} SELECT {proj} FROM s {where_sql}",
                tuple(cte_params + pparams + where_params),
            ).fetchone()
        return {"off_type": int(row["off_type"] or 0), "empty": int(row["empty"] or 0)}
    where, params = _ds_where(ns_id, q, filters)
    _v, _vp = field_read_sql(order_by)
    proj, pparams = order_health_sql(_v, _vp, order_type, order_options)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {proj} FROM datastore_rows {where}",
            tuple(pparams + params),
        ).fetchone()
    return {"off_type": int(row["off_type"] or 0), "empty": int(row["empty"] or 0)}


def datastore_count_rows(ns_id: int, q: Optional["Recherche"] = None,
                         filters: Optional[list] = None,
                         after_row_id: Optional[str] = None,
                         jusqua: Optional[int] = None) -> int:
    """Nombre total de rows d'un namespace (pour la pagination), filtré par `q` et
    les filtres par colonne — même clause que `datastore_list_rows` → total cohérent
    avec la page affichée.

    ⚠️ Même détoastage répété que `datastore_list_rows` (oto-backend#980) — un
    `COUNT` scanne TOUTES les lignes filtrées, donc paie la relecture de `data`
    par filtre sur le jeu ENTIER, jamais juste la page. CTE mince quand `filters`
    s'y prête (`thin_read_cte_sql`).

    `after_row_id` = ne compter qu'APRÈS cette ligne (borne exclusive, keyset de
    `datastore_list_rows_after`) : le reste d'un geste repris par curseur.

    `jusqua` = ne compter qu'AU PLUS ce nombre de lignes (le compte s'arrête là).
    ⚠️ Avec `q`, chaque ligne trouvée coûte : l'index trigramme de la portée
    `values` exige de revérifier chaque candidat, donc de recalculer ses valeurs
    servies (`datastore_valeurs_texte_v1`) — un compte exact paie autant d'appels que
    de lignes trouvées (19-22 s mesurés en prod pour 1 243 lignes d'un tableau
    large). Borné, il en paie au plus `jusqua`."""
    borne = " LIMIT %s" if jusqua is not None else ""
    bparams = [jusqua] if jusqua is not None else []
    thin = None if after_row_id else thin_read_cte_sql(ns_id, q, filters)
    if thin is not None:
        cte_sql, cte_params, where_sql, where_params = thin
        with _connect() as conn:
            row = conn.execute(
                f"WITH {cte_sql} SELECT COUNT(*) AS n FROM "
                f"(SELECT 1 FROM s {where_sql}{borne}) c",
                tuple(cte_params + where_params + bparams),
            ).fetchone()
            return int(row["n"]) if row else 0
    where, params = _ds_where(ns_id, q, filters)
    if after_row_id:
        where += " AND row_id > %s"
        params.append(after_row_id)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM (SELECT 1 FROM datastore_rows {where}{borne}) c",
            tuple(params + bparams)
        ).fetchone()
        return int(row["n"]) if row else 0


def datastore_page_with_stats(ns_id: int, *, offset: int = 0, limit: Optional[int] = None,
                              order_by: Optional[str] = None, order_dir: str = "desc",
                              q: Optional["Recherche"] = None, filters: Optional[list] = None,
                              order_type: Optional[str] = None,
                              order_options: Optional[list] = None
                              ) -> Optional[tuple[list[dict], int, int, int]]:
    """Page + total + compteurs d'écart de tri (`off_type`/`empty`), en UNE SEULE
    requête — `None` quand la lecture n'est pas amincissable (`thin_read_cte_sql`),
    l'appelant retombe alors sur `datastore_list_rows`/`datastore_count_rows`/
    `datastore_order_health` séparés, inchangés (oto-backend#980, suite).

    Avant ce lot : page, total et compteurs reconstruisaient chacun leur PROPRE
    CTE mince et re-scannaient le jeu filtré entier — trois passages de
    détoastage sur les mêmes lignes (mesuré en prod, ~400-500 ms chacun sur le
    vivier réel). Une seule CTE `f` (le jeu mince ET filtré, calculé UNE fois)
    porte les trois : `agg` (total + compteurs, un `COUNT(*) FILTER` par
    compteur) et `p` (la page, `row_number()` + `LIMIT`/`OFFSET`) lisent TOUTES
    LES DEUX `f`, jamais `s` ni `datastore_rows` directement.

    ⚠️ **`offset` au-delà du total** : `p` rend alors ZÉRO ligne — un
    `JOIN agg, p` perdrait le total avec elle. `agg` porte TOUJOURS exactement
    UNE ligne (un `COUNT(*)` sans `GROUP BY` n'est jamais vide) : le combiné
    part donc de `agg LEFT JOIN p ON TRUE`, jamais l'inverse — la ligne de
    stats survit même quand la page est vide (`p.rn IS NULL` dans ce cas, geste
    de lecture explicite ci-dessous plutôt que silencieux)."""
    direction = "ASC" if str(order_dir).lower() == "asc" else "DESC"
    order_simple = order_by not in (None, "", "_created_at", "_updated_at", "_id")
    typed = bool(order_type and order_simple)
    thin = thin_read_cte_sql(ns_id, q, filters, order_by if typed else None)
    if thin is None:
        return None
    if order_simple and not typed:
        # Tri d'une colonne UTILISATEUR sans type de tri résolu (texte, non déclarée,
        # `type: "formula"`… — `order_spec` ne résout que number/enum/date) : il lit
        # `data`, donc sa clé doit être dans la CTE mince, comme celle d'un tri typé.
        # Pas amincissable (couche, chemin de liste) → `None` : l'appelant retombe sur
        # `datastore_list_rows`, qui sait lire ces formes.
        thin = thin_read_cte_sql(ns_id, q, filters, order_by)
        if thin is None:
            return None
    cte_sql, cte_params, where_sql, where_params = thin

    if typed:
        _v, _vp = field_read_sql(order_by)
        order_sql, order_params = typed_order_sql(_v, _vp, order_type, order_options, direction)
        health_proj, health_params = order_health_sql(_v, _vp, order_type, order_options)
    else:
        if order_simple:
            # ⚠️ Cette branche manquait : `order_by=<colonne sans type résolu>` retombait
            # en SILENCE sur `created_at` ci-dessous — l'ordre de création, ni croissant
            # ni décroissant sur la colonne demandée — dès que des filtres posaient la CTE
            # mince (sans filtre, on ne passait pas par ici). Même tri textuel que
            # `datastore_list_rows` (la face MCP), sur la MÊME lecture de valeur.
            _v, _vp = field_read_sql(order_by)
            order_sql, order_params = f"{_v} {direction}, row_id {direction}", list(_vp)
        elif order_by == "_updated_at":
            order_sql, order_params = f"updated_at {direction}, row_id {direction}", []
        elif order_by == "_id":
            order_sql, order_params = f"row_id {direction}", []
        else:
            order_sql, order_params = f"created_at {direction}, row_id {direction}", []
        # Pas de tri typé → pas de compteur d'écart à calculer (`_order_health`
        # ne l'appelle pas non plus dans ce cas) : littéraux, aucun paramètre.
        health_proj, health_params = "0 AS off_type, 0 AS empty", []

    tail = ""
    tail_params: list = []
    if limit is not None:
        tail = " LIMIT %s OFFSET %s"
        tail_params = [limit, offset]

    sql = (
        f"WITH {cte_sql}, "
        f"f AS (SELECT * FROM s {where_sql}), "
        f"agg AS (SELECT COUNT(*) AS total, {health_proj} FROM f), "
        f"p AS (SELECT row_id, row_number() OVER (ORDER BY {order_sql}) AS rn "
        f"FROM f ORDER BY rn{tail}) "
        "SELECT agg.total, agg.off_type, agg.empty, p.rn, "
        "dr.row_id, dr.created_at, dr.updated_at, dr.data, dr.rev, "
        "dr.claimed_by, dr.claimed_until, dr.claimed_run, dr.claims, dr.abandon_reason, "
        "dr.abandon_run, "
        "(dr.claimed_until IS NOT NULL AND dr.claimed_until > NOW()) AS claim_active "
        "FROM agg LEFT JOIN p ON TRUE "
        "LEFT JOIN datastore_rows dr ON dr.row_id = p.row_id AND dr.ns_id = %s "
        "ORDER BY p.rn"
    )
    params = (cte_params + where_params + health_params
              + order_params + tail_params + [ns_id])
    if limit is not None:
        # Une page : en un message (cf. `_page_en_un_message`), statistiques à côté —
        # identiques sur chaque ligne de `agg LEFT JOIN p`, donc lues par `max`.
        with _connect() as conn:
            r = _page_en_un_message(
                conn, sql, tuple(params),
                stats="max(t.total) AS total, max(t.off_type) AS off_type, "
                      "max(t.empty) AS empty, ")
        return (r["lignes"], int(r["total"] or 0), int(r["off_type"] or 0),
                int(r["empty"] or 0))
    with _connect() as conn:
        result = conn.execute(sql, tuple(params)).fetchall()
    if not result:
        # `agg` porte toujours une ligne — un résultat vide signale un `ns_id`
        # sans AUCUNE ligne (jeu filtré vide dès la base, jamais une page hors
        # bornes d'un jeu non-vide, couvert par `p.rn IS NULL` ci-dessous).
        return [], 0, 0, 0
    total = int(result[0]["total"] or 0)
    off_type = int(result[0]["off_type"] or 0)
    empty = int(result[0]["empty"] or 0)
    # `agg`/`p` (total, off_type, empty, rn) ne font pas partie de la FORME d'une
    # ligne — seules les colonnes de `dr` la composent, même dict que rendrait
    # `datastore_list_rows`.
    rows = [{"row_id": r["row_id"], "created_at": r["created_at"],
             "updated_at": r["updated_at"], "data": r["data"], "rev": r["rev"],
             "claimed_by": r["claimed_by"], "claimed_until": r["claimed_until"],
             "claimed_run": r["claimed_run"], "claims": r["claims"],
             "abandon_reason": r["abandon_reason"], "abandon_run": r["abandon_run"],
             "claim_active": r["claim_active"]}
            for r in result if r["rn"] is not None]
    return rows, total, off_type, empty


def datastore_aggregate(ns_id: int, *, group_by: Optional[str] = None,
                        metrics: Optional[list] = None, q: Optional["Recherche"] = None,
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


def datastore_count_by(ns_id: int, fields: list, *, q: Optional["Recherche"] = None,
                       filters: Optional[list] = None,
                       values_limit: int = COUNT_BY_VALUES_DEFAUT) -> dict:
    """Compteurs de N colonnes en UNE requête (`_build_count_by`) : par colonne,
    `{values: [{value, count}], empty, distinct, total, truncated}` — `values` trié
    du plus fréquent au moins fréquent, `truncated` vrai quand des valeurs distinctes
    restent au-delà de `values_limit` (la liste n'est alors pas complète, et le dit)."""
    if not isinstance(values_limit, int) or not 1 <= values_limit <= COUNT_BY_VALUES_MAX:
        raise ValueError(f"`values_limit`: integer from 1 to {COUNT_BY_VALUES_MAX} "
                         f"(got {values_limit!r})")
    sql, params, champs = _build_count_by(ns_id, fields, q, filters, values_limit)
    with _connect() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    out = {k: {"values": [], "empty": 0, "distinct": 0, "total": 0, "truncated": False}
           for k in champs}
    for r in rows:
        c = out[champs[r["i"]]]
        c["distinct"], c["total"] = int(r["distinct_n"]), int(r["total_n"])
        if r["v"] is None:
            c["empty"] = int(r["n"])
        else:
            c["values"].append({"value": r["v"], "count": int(r["n"])})
    for c in out.values():
        c["truncated"] = c["distinct"] > len(c["values"])
    return out


def datastore_merge_row_locked(ns_id: int, row_id: str, apply_fn, updated_at: str,
                               lease_guard=None, expected_revision: Optional[int] = None,
                               rafraichir_rang: bool = False):
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

    ⚠️ **Le patch par `id` y passe aussi depuis le 12/09/2026**, et c'est la seule
    écriture de `data` par `row_id` qui reste : il lisait la ligne dans une connexion
    et réécrivait le JSON entier dans une autre (feu `datastore_update_row`). Mesuré
    sur l'arbre servi : deux patchs simultanés sur deux colonnes DIFFÉRENTES perdaient
    l'un des deux dans 100 % des cas.

    `lease_guard` (#317) = contrôle du BAIL, appelé sous le verrou avec la ligne
    verrouillée (`claimed_by`/`claimed_until`/`claimed_run` inclus). Il lève pour
    refuser l'écriture — la transaction rollback, rien n'est écrit. Passé en
    paramètre plutôt que codé ici parce que « qui a le droit d'écrire » est une règle
    du STORE, pas du SQL : ce module ne connaît ni le run courant ni le worker.

    `expected_revision` (12/09/2026) = la précondition d'un écrivain qui a CALCULÉ ce
    qu'il écrit à partir d'une lecture. Comparée à `rev` sous le MÊME verrou, dans cet
    ordre : le bail, puis la révision, puis `apply_fn` et l'UPDATE. Différente ⇒
    `RevisionConflict` avant l'UPDATE — ni `data`, ni `rev`, ni `updated_at`, ni
    `claims` ne bougent. Aucune relance ici : recalculer est le travail de qui a lu.
    `rev` n'est jamais écrite par ce code : c'est le déclencheur qui l'avance
    (`db/revision.py`).

    `rafraichir_rang` = rafraîchir le vecteur de classement dans la transaction. Le
    patch par `id` le faisait (feu `datastore_update_row`) ; la fusion par clé ne l'a
    jamais fait. Divergence PRÉEXISTANTE, conservée telle quelle et nommée ici plutôt
    que corrigée en passant : l'étendre au lot doublerait les UPDATE d'un import.
    """
    with ecriture_de_lignes() as conn:
        # Le bail est lu DANS le même verrou que la donnée (#317) : le lire
        # avant, sur une autre connexion, laisserait la fenêtre où un claim
        # s'intercale entre le contrôle et l'écriture — le défaut exact que
        # `FOR UPDATE` a été posé pour fermer sur `data` (#197). La révision, pour
        # la même raison.
        locked = conn.execute(
            "SELECT data, rev, claimed_by, claimed_until, claimed_run "
            "FROM datastore_rows WHERE ns_id = %s AND row_id = %s FOR UPDATE",
            (ns_id, row_id),
        ).fetchone()
        if locked is None:
            return None
        if lease_guard is not None:
            lease_guard(locked)
        if expected_revision is not None \
                and int(locked["rev"]) != int(expected_revision):
            from ..datastore.errors import RevisionConflict
            raise RevisionConflict(row_id, expected_revision, locked["rev"])
        current = locked["data"]
        if not isinstance(current, dict):
            current = json.loads(current) if current else {}
        merged = apply_fn(current)
        row = conn.execute(
            "UPDATE datastore_rows SET data = %s::jsonb, updated_at = %s::timestamptz, "
            # Toute écriture de la ligne remet le compteur de reprises à zéro et
            # la rouvre à la file (#433) : une ligne réparée à la main y revient.
            "       claims = 0, abandon_reason = NULL, abandon_run = NULL "
            "WHERE ns_id = %s AND row_id = %s "
            "RETURNING row_id, created_at, updated_at, data, rev",
            (json.dumps(merged), updated_at, ns_id, row_id),
        ).fetchone()
        if rafraichir_rang:
            from .search import stamp_rank_vector
            stamp_rank_vector(conn, "datastore_rows", "ns_id = %s AND row_id = %s",
                              (ns_id, row_id))
        return dict(row), merged


def datastore_delete_row(ns_id: int, row_id: str, *, lease_guard=None,
                         expected_revision: Optional[int] = None) -> Optional[dict]:
    """SUPPRIME une row par son `row_id`, sous verrou de ligne — la JUMELLE de
    `datastore_merge_row_locked`, et dans le MÊME ordre : le bail, puis la révision,
    puis le geste (oto#217).

    Elle était une `DELETE` nue. Deux conséquences, mesurées à la lecture puis rejouées
    sur base jetable : la garde de bail reposait sur une lecture SÉPARÉE, donc une
    réservation qui s'intercalait entre le contrôle et la suppression passait ; et rien
    ne permettait de dire « supprime seulement si la ligne est encore celle que j'ai
    lue » — la ligne partait avec la modification qu'un autre venait d'y poser.

    `lease_guard` = le contrôle du BAIL, appelé sous le verrou avec la ligne verrouillée,
    exactement comme à l'écriture : il lève pour refuser, la transaction rollback.
    `expected_revision` = la précondition de qui a décidé d'après une lecture. Différente
    de `rev` ⇒ `RevisionConflict` AVANT la suppression : rien n'est supprimé.

    Rend la DONNÉE de la ligne supprimée (relevé du journal : l'état d'avant, lu sous le
    verrou, donc vrai), ou `None` si la ligne n'existait pas. ⚠️ `{}` est une ligne vide
    bel et bien supprimée : les appelants testent `is None`, jamais la vérité du dict.

    Par le point de passage de l'estampille (oto#273) : la suppression laisse une
    révision au journal, qui dit qui l'a faite.
    """
    with ecriture_de_lignes() as conn:
        locked = conn.execute(
            "SELECT data, rev, claimed_by, claimed_until, claimed_run "
            "FROM datastore_rows WHERE ns_id = %s AND row_id = %s FOR UPDATE",
            (ns_id, row_id),
        ).fetchone()
        if locked is None:
            return None
        if lease_guard is not None:
            lease_guard(locked)
        if expected_revision is not None \
                and int(locked["rev"]) != int(expected_revision):
            from ..datastore.errors import RevisionConflict
            raise RevisionConflict(row_id, expected_revision, locked["rev"])
        conn.execute(
            "DELETE FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id),
        )
        data = locked["data"]
        if not isinstance(data, dict):
            data = json.loads(data) if data else {}
        return data


# ── File de travail (ADR 0046 D) ─────────────────────────────────────────────
# Une row se « claim » avec un BAIL (claimed_by/claimed_until) : pick atomique de
# la prochaine row libre (bail NULL ou expiré) via FOR UPDATE SKIP LOCKED — deux
# workers concurrents ne prennent jamais la même row, sans sérialiser la table.
# Le bail expiré rend la row recyclable (worker mort ≠ row perdue). Libération :
# explicite (release, gardée par worker) ou automatique à l'entrée dans un état
# terminal du cycle de vie (côté store).
