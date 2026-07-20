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

from ._conn import _connect
from .users import upsert_user


def create_datastore_namespace(owner_type: str, owner_id: str, namespace: str) -> int:
    """Crée un namespace possédé par `(owner_type, owner_id)` (ADR 0030). `owner_type`
    ∈ {user, org, group} ; `owner_id` = sub | org.id::text | group.id::text. Lève si
    le même propriétaire a déjà ce nom."""
    if owner_type == "user":
        upsert_user(owner_id)
    with _connect() as conn:
        try:
            row = conn.execute(
                "INSERT INTO user_datastores (owner_type, owner_id, namespace) "
                "VALUES (%s, %s, %s) RETURNING id",
                (owner_type, owner_id, namespace),
            ).fetchone()
        except psycopg.errors.UniqueViolation as e:
            raise ValueError(f"namespace `{namespace}` existe déjà") from e
        return int(row["id"])


def get_datastore_namespace(owner_type: str, owner_id: str, namespace: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, owner_type, owner_id, namespace, created_at FROM user_datastores "
            "WHERE owner_type = %s AND owner_id = %s AND namespace = %s",
            (owner_type, owner_id, namespace),
        ).fetchone()
        return dict(row) if row else None


def get_datastore_namespace_by_id(ns_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, owner_type, owner_id, namespace, schema, created_at "
            "FROM user_datastores WHERE id = %s",
            (ns_id,),
        ).fetchone()
        return dict(row) if row else None


def set_datastore_schema(ns_id: int, schema: Optional[dict]) -> None:
    """Pose (ou retire si None) le schéma typé d'un namespace (ADR 0032 §6 / 0029, B6).
    Soft : aucune validation des rows existantes — c'est un schéma de rendu, pas une
    contrainte d'écriture."""
    cfg = json.dumps(schema) if schema is not None else None
    with _connect() as conn:
        conn.execute("UPDATE user_datastores SET schema = %s::jsonb WHERE id = %s",
                     (cfg, ns_id))


def list_datastore_namespaces_for_owners(owners: list[tuple[str, str]]) -> list[dict]:
    """Namespaces possédés par l'un des `(owner_type, owner_id)` fournis."""
    if not owners:
        return []
    otypes = [o[0] for o in owners]
    oids = [o[1] for o in owners]
    with _connect() as conn:
        rows = conn.execute(
            "SELECT d.id, d.owner_type, d.owner_id, d.namespace, d.schema, d.created_at "
            "FROM user_datastores d "
            "JOIN unnest(%s::text[], %s::text[]) AS o(t, i) "
            "  ON d.owner_type = o.t AND d.owner_id = o.i "
            "ORDER BY d.namespace",
            (otypes, oids),
        ).fetchall()
        return [dict(r) for r in rows]


def resolve_datastore_ns(
    namespace: str, *, sub: str, org_ids: list[int], group_ids: list[int],
) -> Optional[dict]:
    """Résout un namespace VISIBLE par l'acteur, par NOM **ou par ID** numérique, parmi :
    possédé en perso, possédé par une de ses orgs, ou accordé (grant user/org/group).
    Priorité perso > org > grant. Retourne la ligne `user_datastores` (avec `id`) ou None.
    La décision read/write fine est ensuite faite par `ownership.can_access` sur l'id.

    ⚠️ **id OU nom** : un lien de projet stocke souvent le `target_ref` = **id numérique**
    (le picker dashboard, `EntityPickerDialog`) alors que l'agent lie par **nom** — les deux
    doivent résoudre (sinon l'aperçu tableau tombait en 404 → « Aperçu indisponible »). Le
    prédicat de VISIBILITÉ est identique quelle que soit la clé (aucun IDOR : un id hors de
    la portée de l'acteur ne résout pas). Collision improbable (un namespace nommé « 109 »
    vs un id 109) → le match par NOM est préféré."""
    org_txt = [str(o) for o in org_ids]
    grp_txt = [str(g) for g in group_ids]
    ns_id = int(namespace) if str(namespace).isdigit() else None
    with _connect() as conn:
        row = conn.execute(
            "SELECT d.id, d.owner_type, d.owner_id, d.namespace, d.schema, d.created_at "
            "FROM user_datastores d "
            "WHERE (d.namespace = %(ns)s OR d.id = %(nsid)s) AND ("
            "     (d.owner_type = 'user' AND d.owner_id = %(sub)s)"
            "  OR (d.owner_type = 'org'  AND d.owner_id = ANY(%(org)s))"
            # ADR 0049 (cadrage 10/07) : team-owned = visible dans le contexte de l'org
            # parente (le caller passe mes équipes — ou toutes celles de l'org si admin).
            "  OR (d.owner_type = 'group' AND d.owner_id = ANY(%(grp)s))"
            "  OR EXISTS ("
            "       SELECT 1 FROM resource_grants g"
            "        WHERE g.resource_type = 'datastore_namespace' AND g.resource_id = d.id::text"
            "          AND ( (g.principal_type = 'user'  AND g.principal_id = %(sub)s)"
            "             OR (g.principal_type = 'org'   AND g.principal_id = ANY(%(org)s))"
            "             OR (g.principal_type = 'group' AND g.principal_id = ANY(%(grp)s)) ))"
            ") "
            "ORDER BY CASE WHEN d.namespace = %(ns)s THEN 0 ELSE 1 END, "
            "         CASE WHEN d.owner_type='user' AND d.owner_id=%(sub)s THEN 0 "
            "              WHEN d.owner_type='org' THEN 1 ELSE 2 END "
            "LIMIT 1",
            {"ns": namespace, "nsid": ns_id, "sub": sub, "org": org_txt, "grp": grp_txt},
        ).fetchone()
        return dict(row) if row else None


def list_datastore_namespaces_granted_to(
    sub: str, org_ids: list[int], group_ids: list[int],
) -> list[dict]:
    """Namespaces accordés à l'**org active / groupe actif** via `resource_grants`
    (principal org/group), avec la permission gagnante.

    Volontairement **PAS** les grants `principal_type='user'` : un partage *en propre*
    (cross-org, ex. un namespace de ton org perso partagé à ton compte) ne doit pas
    polluer la vue Données de CHAQUE org — l'org est le contexte (ADR 0023, scope décidé
    avec l'utilisateur le 2026-07-01). La résolution par nom (`resolve_datastore_ns`) est
    elle aussi scopée à l'org active côté appelant (2026-07-03). `sub` ne sert plus qu'à
    exclure les reliques perso possédées (gérées à part)."""
    org_txt = [str(o) for o in org_ids]
    grp_txt = [str(g) for g in group_ids]
    with _connect() as conn:
        rows = conn.execute(
            "SELECT d.id, d.owner_type, d.owner_id, d.namespace, d.created_at, "
            "       max(g.permission) AS permission "
            "FROM resource_grants g "
            "JOIN user_datastores d ON d.id::text = g.resource_id "
            "WHERE g.resource_type = 'datastore_namespace' AND ("
            "     (g.principal_type = 'org'   AND g.principal_id = ANY(%(org)s))"
            "  OR (g.principal_type = 'group' AND g.principal_id = ANY(%(grp)s)) ) "
            "AND NOT (d.owner_type = 'user' AND d.owner_id = %(sub)s) "
            "GROUP BY d.id, d.owner_type, d.owner_id, d.namespace, d.created_at "
            "ORDER BY d.namespace",
            {"sub": sub, "org": org_txt, "grp": grp_txt},
        ).fetchall()
        return [dict(r) for r in rows]


def rename_datastore_namespace_by_id(ns_id: int, new: str) -> bool:
    """Renomme un namespace par id (l'id BIGSERIAL est conservé → URL/deeplink/grants
    stables ; les grants sont keyés par id, donc rien à propager). Lève si le même
    propriétaire a déjà ce nom, ou si l'id est introuvable."""
    new = (new or "").strip()
    if not new:
        raise ValueError("nouveau nom de namespace requis")
    with _connect() as conn:
        with conn.transaction():
            cur = conn.execute(
                "SELECT owner_type, owner_id, namespace FROM user_datastores WHERE id = %s FOR UPDATE",
                (ns_id,),
            ).fetchone()
            if not cur:
                raise ValueError("namespace introuvable")
            if cur["namespace"] == new:
                return True
            if conn.execute(
                "SELECT 1 FROM user_datastores WHERE owner_type = %s AND owner_id = %s AND namespace = %s",
                (cur["owner_type"], cur["owner_id"], new),
            ).fetchone():
                raise ValueError(f"un namespace `{new}` existe déjà")
            conn.execute(
                "UPDATE user_datastores SET namespace = %s WHERE id = %s", (new, ns_id),
            )
    return True


def delete_datastore_namespace_by_id(ns_id: int) -> bool:
    """Supprime un namespace par id (CASCADE sur `datastore_rows`) + ses grants
    (`resource_grants` n'a pas de FK car `resource_id` est générique) + son
    éventuel index de clé métier (#109 ch.3 — orphelin inoffensif sinon, mais
    autant nettoyer)."""
    with _connect() as conn:
        with conn.transaction():
            conn.execute(
                "DELETE FROM resource_grants WHERE resource_type = 'datastore_namespace' AND resource_id = %s",
                (str(ns_id),),
            )
            cur = conn.execute("DELETE FROM user_datastores WHERE id = %s", (ns_id,))
    datastore_drop_key_index(ns_id)
    return cur.rowcount > 0


def reparent_datastore_namespace(ns_id: int, new_owner_type: str, new_owner_id: str) -> None:
    """Re-parente un namespace vers un nouveau propriétaire (cœur du transfert).
    Lève si le destinataire possède déjà un namespace de ce nom."""
    with _connect() as conn:
        with conn.transaction():
            row = conn.execute(
                "SELECT namespace FROM user_datastores WHERE id = %s FOR UPDATE", (ns_id,),
            ).fetchone()
            if not row:
                raise ValueError("namespace introuvable")
            if conn.execute(
                "SELECT 1 FROM user_datastores WHERE owner_type = %s AND owner_id = %s AND namespace = %s",
                (new_owner_type, new_owner_id, row["namespace"]),
            ).fetchone():
                raise ValueError(f"le destinataire possède déjà un namespace `{row['namespace']}`")
            conn.execute(
                "UPDATE user_datastores SET owner_type = %s, owner_id = %s WHERE id = %s",
                (new_owner_type, new_owner_id, ns_id),
            )


def list_all_datastore_namespaces() -> list[dict]:
    """Tous les namespaces, toutes propriétés confondues — pour l'object-browser
    PLATEFORME (gate super_admin/platform_admin côté capacité)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, owner_type, owner_id, namespace, created_at "
            "FROM user_datastores ORDER BY owner_type, owner_id, namespace",
        ).fetchall()
        return [dict(r) for r in rows]


def count_datastore_rows_for_ns(ns_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM datastore_rows WHERE ns_id = %s", (ns_id,),
        ).fetchone()
        return int(row["n"]) if row else 0


# ADR 0048 — le rôle est la source de vérité ; `permission` (plan CONTENU) en dérive.
_ROLE_TO_PERMISSION = {"viewer": "read", "editor": "write", "manager": "write"}
_PERMISSION_TO_ROLE = {"read": "viewer", "write": "editor"}


def _normalize_role(role: Optional[str], permission: Optional[str]) -> str:
    """Rôle effectif d'un grant. `role` prime ; sinon rétro-compat depuis `permission`
    (read→viewer, write→editor) ; défaut `editor`."""
    if role in _ROLE_TO_PERMISSION:
        return role
    return _PERMISSION_TO_ROLE.get(permission or "", "editor")


def grant_resource(
    resource_type: str, resource_id: str, principal_type: str, principal_id: str,
    permission: Optional[str] = None, granted_by: Optional[str] = None,
    role: Optional[str] = None,
) -> None:
    """Accorde (ou met à jour) un RÔLE à un principal sur une ressource (ADR 0048).
    `role` ∈ {viewer, editor, manager} prime ; à défaut `permission` read/write est mappé
    (rétro-compat). `permission` (plan CONTENU) est TOUJOURS dérivée du rôle (viewer→read,
    editor/manager→write) → tout le SQL du plan contenu reste inchangé. Idempotent :
    ON CONFLICT met à jour rôle + permission."""
    eff_role = _normalize_role(role, permission)
    eff_perm = _ROLE_TO_PERMISSION[eff_role]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO resource_grants "
            "(resource_type, resource_id, principal_type, principal_id, permission, role, granted_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (resource_type, resource_id, principal_type, principal_id) "
            "DO UPDATE SET permission = EXCLUDED.permission, role = EXCLUDED.role, "
            "granted_by = EXCLUDED.granted_by",
            (resource_type, resource_id, principal_type, principal_id,
             eff_perm, eff_role, granted_by),
        )


def revoke_resource_grant(
    resource_type: str, resource_id: str, principal_type: str, principal_id: str,
) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM resource_grants WHERE resource_type = %s AND resource_id = %s "
            "AND principal_type = %s AND principal_id = %s",
            (resource_type, resource_id, principal_type, principal_id),
        )
        return cur.rowcount > 0


def get_resource_grant(
    resource_type: str, resource_id: str, principal_type: str, principal_id: str,
) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT permission, role FROM resource_grants WHERE resource_type = %s AND resource_id = %s "
            "AND principal_type = %s AND principal_id = %s",
            (resource_type, resource_id, principal_type, principal_id),
        ).fetchone()
        return dict(row) if row else None


def list_resource_grants(resource_type: str, resource_id: str) -> list[dict]:
    """Bénéficiaires d'une ressource (principal + permission + email si user), pour
    l'UI de gestion du partage."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT g.principal_type, g.principal_id, g.permission, g.role, g.granted_at, u.email "
            "FROM resource_grants g "
            "LEFT JOIN users u ON g.principal_type = 'user' AND u.sub = g.principal_id "
            "WHERE g.resource_type = %s AND g.resource_id = %s "
            "ORDER BY g.granted_at",
            (resource_type, resource_id),
        ).fetchall()
        return [dict(r) for r in rows]


def datastore_insert_row(ns_id: int, row_id: str, data: dict,
                         created_at: Optional[str] = None,
                         updated_at: Optional[str] = None) -> dict:
    """Insère une row. `created_at`/`updated_at` optionnels (override pour le
    backfill ; sinon NOW())."""
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO datastore_rows (ns_id, row_id, data, created_at, updated_at) "
            "VALUES (%s, %s, %s::jsonb, COALESCE(%s::timestamptz, NOW()), COALESCE(%s::timestamptz, NOW())) "
            "RETURNING row_id, created_at, updated_at, data",
            (ns_id, row_id, json.dumps(data), created_at, updated_at),
        ).fetchone()
        return dict(row)


def datastore_upsert_row(ns_id: int, row_id: str, data: dict) -> tuple[dict, bool]:
    """Insère OU met à jour une row par sa clé `(ns_id, row_id)`. Idempotent :
    re-poser le même `row_id` remplace `data` au lieu de dupliquer (sert la
    dédup par clé stable, ex. urn LinkedIn). Renvoie `(row, inserted)` où
    `inserted` est True si la row n'existait pas (ON CONFLICT non déclenché)."""
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO datastore_rows (ns_id, row_id, data, created_at, updated_at) "
            "VALUES (%s, %s, %s::jsonb, NOW(), NOW()) "
            "ON CONFLICT (ns_id, row_id) DO UPDATE SET data = EXCLUDED.data, updated_at = NOW() "
            "RETURNING row_id, created_at, updated_at, data, (xmax = 0) AS inserted",
            (ns_id, row_id, json.dumps(data)),
        ).fetchone()
        inserted = bool(row.pop("inserted"))
        return dict(row), inserted


def datastore_find_row_id_by_key(ns_id: int, key_field: str, key_value) -> Optional[str]:
    """Trouve le `row_id` d'une row par une CLÉ MÉTIER (champ JSONB `data->>key`),
    pour la dédup d'un batch write. Renvoie le plus ancien match (ordre stable) ou
    None. La clé est interpolée en LITTÉRAL SQL (psycopg.sql, jamais un f-string) :
    un paramètre `data->>$1` ne matcherait pas l'index d'expression de clé métier
    (#109 ch.3) — le littéral rend le lookup indexé, O(1)."""
    from psycopg import sql as _sql
    q = _sql.SQL(
        "SELECT row_id FROM datastore_rows WHERE ns_id = %s AND data->>{k} = %s "
        "ORDER BY created_at ASC LIMIT 1"
    ).format(k=_sql.Literal(str(key_field)))
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


def datastore_ensure_key_index(ns_id: int, key: str) -> None:
    """Pose l'index UNIQUE partiel de clé métier du namespace (dépose l'ancien —
    la clé a pu changer). Nom déterministe `ds_bkey_<ns_id>` (int → sûr) ; la clé
    est un LITTÉRAL composé via psycopg.sql (le DDL ne se paramètre pas)."""
    from psycopg import sql as _sql
    name = _bkey_index_name(ns_id)
    with _connect() as conn:
        conn.execute(_sql.SQL("DROP INDEX IF EXISTS {n}").format(n=_sql.Identifier(name)))
        conn.execute(_sql.SQL(
            "CREATE UNIQUE INDEX {n} ON datastore_rows ((data->>{k})) "
            "WHERE ns_id = {ns} AND data->>{k} IS NOT NULL"
        ).format(n=_sql.Identifier(name), k=_sql.Literal(str(key)),
                 ns=_sql.Literal(int(ns_id))))


def datastore_drop_key_index(ns_id: int) -> None:
    from psycopg import sql as _sql
    with _connect() as conn:
        conn.execute(_sql.SQL("DROP INDEX IF EXISTS {n}").format(
            n=_sql.Identifier(_bkey_index_name(ns_id))))


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


def datastore_get_row(ns_id: int, row_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT row_id, created_at, updated_at, data, claimed_by, claimed_until "
            "FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id),
        ).fetchone()
        return dict(row) if row else None


# Filtres par colonne (vue tableau dashboard, oto-dashboard#18). Chaque filtre =
# {field, op, value}. Le champ est TOUJOURS paramétré (`data ->> %s`) et l'op tiré
# d'une whitelist → fragment SQL fixe, zéro interpolation de valeur = pas d'injection.
_DS_FILTER_OPS = {"contains", "eq", "ne", "in", "gt", "gte", "lt", "lte", "empty", "not_empty"}


_DS_CMP_SQL = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


_DS_NUM_RE = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")  # numérique strict (pas de nan/1e5)


_DS_MAX_FILTERS = 30


def _ds_filter_clauses(filters: Optional[list]) -> tuple[list[str], list]:
    """Construit les fragments WHERE (combinés en AND) pour des filtres par colonne
    JSONB. Champ paramétré + op whitelisté → pas d'injection. Les comparaisons
    ordonnées (`gt/gte/lt/lte`) sont numériques si la valeur EST numérique (cast
    gardé `::numeric`, les rows non numériques sont écartées), sinon textuelles
    (l'ISO `YYYY-MM-DD` se compare correctement en lexicographique). Lève
    `ValueError` sur un filtre malformé (→ 400 côté route)."""
    clauses: list[str] = []
    params: list = []
    if not filters:
        return clauses, params
    if len(filters) > _DS_MAX_FILTERS:
        raise ValueError("too many filters")
    for f in filters:
        if not isinstance(f, dict):
            raise ValueError("invalid filter")
        field, op, val = f.get("field"), f.get("op"), f.get("value")
        if not isinstance(field, str) or not field or op not in _DS_FILTER_OPS:
            raise ValueError("invalid filter")
        if op == "empty":
            clauses.append("(data ->> %s IS NULL OR data ->> %s = '')")
            params.extend([field, field])
        elif op == "not_empty":
            clauses.append("(data ->> %s IS NOT NULL AND data ->> %s <> '')")
            params.extend([field, field])
        elif op == "in":
            vals = [str(v) for v in (val if isinstance(val, list) else [val])
                    if v is not None and str(v) != ""]
            if not vals:
                continue
            clauses.append("data ->> %s = ANY(%s)")
            params.extend([field, vals])
        elif op == "contains":
            clauses.append("data ->> %s ILIKE %s")
            params.extend([field, f"%{val}%"])
        elif op == "eq":
            clauses.append("data ->> %s = %s")
            params.extend([field, str(val)])
        elif op == "ne":
            clauses.append("(data ->> %s IS DISTINCT FROM %s)")
            params.extend([field, str(val)])
        else:  # gt/gte/lt/lte
            sym = _DS_CMP_SQL[op]
            sval = str(val)
            if _DS_NUM_RE.match(sval):
                clauses.append(
                    "(data ->> %s ~ '^-?[0-9]+(\\.[0-9]+)?$' "
                    f"AND (data ->> %s)::numeric {sym} %s::numeric)")
                params.extend([field, field, sval])
            else:
                clauses.append(f"data ->> %s {sym} %s")
                params.extend([field, sval])
    return clauses, params


def _ds_where(ns_id: int, q: Optional[str], filters: Optional[list]) -> tuple[str, list]:
    """Clause WHERE partagée par list/count (même filtrage → total cohérent)."""
    where = "WHERE ns_id = %s"
    params: list = [ns_id]
    if q:
        where += " AND data::text ILIKE %s"
        params.append(f"%{q}%")
    fclauses, fparams = _ds_filter_clauses(filters)
    for c in fclauses:
        where += f" AND {c}"
    params.extend(fparams)
    return where, params


def datastore_list_rows(ns_id: int, *, offset: int = 0, limit: Optional[int] = None,
                        order_by: Optional[str] = None, order_dir: str = "desc",
                        q: Optional[str] = None, filters: Optional[list] = None) -> list[dict]:
    """Page de rows d'un namespace. `order_by` : `_created_at`/`_updated_at`/`_id`
    (colonnes méta) ou un nom de champ user → `data->>field`. `q` : recherche
    plein-texte sur tout le JSON (`data::text ILIKE`). `filters` : filtres par
    colonne (liste `{field, op, value}`, combinés AND — cf. `_ds_filter_clauses`).
    Tri/pagination/recherche/filtres côté SQL (server-side, ADR 0016). `limit=None`
    = toutes les rows (compat `store.list_rows` / MCP `data_rows`)."""
    direction = "ASC" if str(order_dir).lower() == "asc" else "DESC"
    where, params = _ds_where(ns_id, q, filters)
    if order_by in (None, "", "_created_at"):
        order_sql = f"created_at {direction}, row_id {direction}"
    elif order_by == "_updated_at":
        order_sql = f"updated_at {direction}, row_id {direction}"
    elif order_by == "_id":
        order_sql = f"row_id {direction}"
    else:
        order_sql = f"data ->> %s {direction}, row_id {direction}"
        params.append(order_by)  # valeur paramétrée → pas d'injection
    tail = ""
    if limit is not None:
        tail = " LIMIT %s OFFSET %s"
        params.extend([limit, offset])
    with _connect() as conn:
        rows = conn.execute(
            "SELECT row_id, created_at, updated_at, data, claimed_by, claimed_until "
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
            "SELECT row_id, created_at, updated_at, data, claimed_by, claimed_until "
            f"FROM datastore_rows {where} ORDER BY row_id ASC LIMIT %s",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]


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


_NUMERIC_RE = r'^\s*-?[0-9]+(\.[0-9]+)?\s*$'


def _build_aggregate(ns_id: int, group_by: Optional[str], metrics: Optional[list],
                     q: Optional[str], filters: Optional[list],
                     limit: int) -> tuple[str, list, list]:
    """Construit `(sql, params, names)` de l'agrégat — PUR (aucun I/O), testable sans PG.
    `names` = `[(alias_sql, nom_lisible)]`. Ordre des `%s` : colonnes SELECT (group +
    métriques) puis WHERE puis LIMIT — l'ordre de `params` doit suivre EXACTEMENT.
    Les noms de champs passent en PARAMÈTRES (`data->>%s`), jamais interpolés (anti-injection)."""
    metrics = metrics or [{"op": "count"}]
    select, sparams, names = [], [], []  # noms lisibles alignés sur les alias mN
    if group_by:
        select.append("data->>%s AS grp")
        sparams.append(group_by)
    for i, m in enumerate(metrics):
        op = str(m.get("op", "")).lower()
        field = m.get("field")
        alias = f"m{i}"
        if op == "count" and not field:
            select.append(f"COUNT(*) AS {alias}")
            names.append((alias, "count"))
        elif op == "count":
            select.append(f"COUNT(data->>%s) AS {alias}")
            sparams.append(field)
            names.append((alias, f"count_{field}"))
        elif op in ("sum", "avg", "min", "max"):
            if not field:
                raise ValueError(f"agrégat: op '{op}' exige un `field`")
            select.append(
                f"{op.upper()}(CASE WHEN data->>%s ~ %s THEN (data->>%s)::numeric END) AS {alias}")
            sparams.extend([field, _NUMERIC_RE, field])
            names.append((alias, f"{op}_{field}"))
        else:
            raise ValueError(f"agrégat: op inconnu {op!r} (count|sum|avg|min|max)")
    where, wparams = _ds_where(ns_id, q, filters)
    sql = f"SELECT {', '.join(select)} FROM datastore_rows {where}"
    params = sparams + wparams
    if group_by:
        sql += " GROUP BY grp ORDER BY m0 DESC NULLS LAST, grp ASC"
    sql += " LIMIT %s"
    params.append(limit)
    return sql, params, names


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
    for r in rows:
        d: dict = {}
        if group_by:
            d[group_by] = r["grp"]
        for alias, name in names:
            v = r[alias]
            d[name] = float(v) if isinstance(v, Decimal) else v
        out.append(d)
    return out


def datastore_update_row(ns_id: int, row_id: str, data: dict, updated_at: str) -> Optional[dict]:
    """Remplace `data` (le store a déjà fusionné le patch) + `updated_at`."""
    with _connect() as conn:
        row = conn.execute(
            "UPDATE datastore_rows SET data = %s::jsonb, updated_at = %s::timestamptz "
            "WHERE ns_id = %s AND row_id = %s "
            "RETURNING row_id, created_at, updated_at, data",
            (json.dumps(data), updated_at, ns_id, row_id),
        ).fetchone()
        return dict(row) if row else None


def datastore_merge_row_locked(ns_id: int, row_id: str, apply_fn, updated_at: str):
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
    """
    with _connect() as conn:
        with conn.transaction():
            locked = conn.execute(
                "SELECT data FROM datastore_rows WHERE ns_id = %s AND row_id = %s FOR UPDATE",
                (ns_id, row_id),
            ).fetchone()
            if locked is None:
                return None
            current = locked["data"]
            if not isinstance(current, dict):
                current = json.loads(current) if current else {}
            merged = apply_fn(current)
            row = conn.execute(
                "UPDATE datastore_rows SET data = %s::jsonb, updated_at = %s::timestamptz "
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

def datastore_claim_next(ns_id: int, *, worker: str, lease_seconds: int = 900,
                         filters: Optional[list] = None) -> Optional[dict]:
    """Claim atomique de la prochaine row claimable du namespace (ordre de
    création — row_id uuid7 monotone). `filters` = mêmes filtres whitelistés que
    la lecture (`_ds_filter_clauses`), typiquement `[{field:'status',op:'eq',…}]`.
    Renvoie la row (avec bail posé) ou None si plus rien à traiter."""
    fclauses, fparams = _ds_filter_clauses(filters)
    where = "WHERE ns_id = %s AND (claimed_until IS NULL OR claimed_until < NOW())"
    params: list = [ns_id, *fparams]
    for c in fclauses:
        where += f" AND {c}"
    with _connect() as conn:
        picked = conn.execute(
            f"SELECT row_id FROM datastore_rows {where} "
            "ORDER BY row_id ASC LIMIT 1 FOR UPDATE SKIP LOCKED",
            tuple(params),
        ).fetchone()
        if not picked:
            return None
        row = conn.execute(
            "UPDATE datastore_rows SET claimed_by = %s, "
            "claimed_until = NOW() + (%s || ' seconds')::interval "
            "WHERE ns_id = %s AND row_id = %s "
            "RETURNING row_id, created_at, updated_at, data, claimed_by, claimed_until",
            (str(worker), int(lease_seconds), ns_id, picked["row_id"]),
        ).fetchone()
        return dict(row) if row else None


def datastore_claimed_rows(ns_id: int) -> list[dict]:
    """Rows sous bail de file de travail (ADR 0046 D) — la vue « en cours » du
    dashboard. Bail actif OU expiré confondus (le consommateur tranche sur
    `claimed_until`), plus ancien bail d'abord."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT row_id, created_at, updated_at, data, claimed_by, claimed_until "
            "FROM datastore_rows WHERE ns_id = %s AND claimed_by IS NOT NULL "
            "ORDER BY claimed_until ASC",
            (ns_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def datastore_release_claim(ns_id: int, row_id: str, worker: Optional[str]) -> bool:
    """Libère le bail d'une row. `worker` non-None = gardé (on ne libère pas le
    claim d'un autre) ; None = libération inconditionnelle (chemin interne : entrée
    en état terminal). Renvoie False si rien n'a été libéré (pas de bail, ou bail
    d'un autre worker)."""
    guard = "" if worker is None else " AND claimed_by = %s"
    params: tuple = (ns_id, row_id) if worker is None else (ns_id, row_id, str(worker))
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE datastore_rows SET claimed_by = NULL, claimed_until = NULL "
            f"WHERE ns_id = %s AND row_id = %s AND claimed_by IS NOT NULL{guard}",
            params,
        )
        return (cur.rowcount or 0) > 0
