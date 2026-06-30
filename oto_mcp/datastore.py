"""Datastore — substrat natif PostgreSQL (ADR 0016).

Un namespace = une ligne `user_datastores` + ses rows dans `datastore_rows`
(une row = un dict JSONB). Schéma libre : aucune colonne à provisionner, les
champs apparaissent dans `data`. Trois champs auto-managés, exposés à plat dans
la row renvoyée :

- `_id` : identifiant uuid7-like (col `row_id`).
- `_created_at` / `_updated_at` : timestamps (colonnes dédiées).

Plus de dépendance Google : la vérité est en base, types préservés nativement
par JSONB (fin de la sentinelle `__j:` de l'ère Sheets). La propriété et le partage
passent par la primitive générique `ownership` (ADR 0030) : un namespace est possédé
par `(owner_type, owner_id)` (user/org/group) et accessible via owner-match ∪ grants
(`resource_grants`). L'export vers un provider tiers (Sheets/Notion…) est une
projection optionnelle, déférée à otomata#29.
"""
from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from . import db, ownership


_META_COLS = ("_id", "_created_at", "_updated_at")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    # uuid7-ish : timestamp ms + random. Construit à la main pour compat 3.10+.
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = uuid.uuid4().int & ((1 << 74) - 1)
    raw = (ms << 80) | (0x7 << 76) | (rand << 2)
    return str(uuid.UUID(int=raw))


def _dashboard_url() -> str:
    return os.environ.get("OTO_DASHBOARD_URL", "https://dashboard.oto.ninja").rstrip("/")


def _ns_url(namespace: str) -> str:
    """Deep-link vers la vue datastore du dashboard (surface d'édition canonique
    tant que l'export tiers — otomata#29 — n'existe pas)."""
    return f"{_dashboard_url()}/console/data?ns={namespace}"


class NamespaceNotFound(Exception):
    pass


class RowNotFound(Exception):
    pass


class NamespaceExists(Exception):
    pass


class NamespaceReadOnly(Exception):
    """Écriture tentée sur un namespace partagé en lecture seule."""
    pass


class NamespaceForbidden(Exception):
    """Action de gouvernance (supprimer/transférer) tentée sans droit de gouvernance."""
    pass


def make_store(sub: str) -> "DatastorePg":
    """Construit un store PG pour `sub`. Plus aucune dépendance externe (ADR 0016)
    — datastore est une surface plateforme self-contained."""
    return DatastorePg(sub)


class DatastorePg:
    """Store tabulaire per-user adossé à PostgreSQL.

    State-less, instancié par requête à partir du `sub`. Résout chaque namespace
    en `ns_id` (possédé OU partagé) et opère sur `datastore_rows`.
    """

    def __init__(self, sub: str):
        self.sub = sub
        self._scope_cache: Optional[ownership.AccessorScope] = None

    # --- résolution namespace -> ns_id ---------------------------------------

    def _scope(self) -> ownership.AccessorScope:
        if self._scope_cache is None:
            self._scope_cache = ownership.accessor_scope(self.sub)
        return self._scope_cache

    def _resolve(self, namespace: str, *, write: bool = False) -> int:
        """ns_id d'un namespace VISIBLE par l'acteur (possédé perso/org, ou accordé).
        `write=True` exige le droit d'écriture via `ownership.can_access`."""
        scope = self._scope()
        ns = db.resolve_datastore_ns(
            namespace, sub=self.sub, org_ids=scope.org_ids, group_ids=scope.group_ids)
        if not ns:
            raise NamespaceNotFound(namespace)
        ns_id = int(ns["id"])
        if write and not ownership.can_access(
                self.sub, "datastore_namespace", str(ns_id), "write"):
            raise NamespaceReadOnly(namespace)
        return ns_id

    @staticmethod
    def _row_to_dict(row: dict) -> dict:
        """Ligne `datastore_rows` → row API (`_id`/`_created_at`/`_updated_at` à
        plat + champs user)."""
        data = row.get("data") or {}
        return {
            "_id": row["row_id"],
            "_created_at": row["created_at"],
            "_updated_at": row["updated_at"],
            **{k: v for k, v in data.items() if k not in _META_COLS},
        }

    # --- namespace lifecycle -------------------------------------------------

    def _entry(self, n: dict, *, shared: bool, permission: Optional[str] = None) -> dict:
        ns_id = int(n["id"])
        perso = n.get("owner_type") == "user" and n.get("owner_id") == self.sub
        return {
            "id": ns_id,
            "namespace": n["namespace"],
            "created_at": n.get("created_at"),
            "url": _ns_url(n["namespace"]),
            "shared": shared,
            "owner_type": n.get("owner_type"),
            "owner_id": n.get("owner_id"),
            "permission": permission if shared else "write",
            "can_write": (permission == "write") if shared else True,
            "can_govern": ownership.can_govern(self.sub, "datastore_namespace", str(ns_id)),
            "is_personal": perso,
            "schema": n.get("schema"),   # mode typé optionnel (ADR 0032 §6 / 0029, B6) ; None = table libre
        }

    def list_namespaces(self) -> list[dict]:
        """Namespaces visibles DANS L'ORG ACTIVE (l'org est le contexte, ADR 0023) :
        possédés par l'org active + accordés à elle (grant d'org/groupe actif). Un
        namespace possédé par une AUTRE org — ou partagé à l'acteur *en propre* (grant
        user, cross-org) — ne fuite PLUS dans la vue d'une org tierce (scope décidé le
        2026-07-01). Dédupliqués par id (priorité possédé). L'agent garde l'accès par
        nom (`can_access` inchangé) même si le namespace n'est pas listé ici."""
        from . import access
        owner = ownership.active_owner(access.current_org(self.sub))
        if owner is None:
            return []
        org = int(owner[1])
        grp = access.current_group(self.sub)
        org_ids = [org]
        group_ids = [grp] if grp is not None else []
        out: dict[int, dict] = {}
        for n in db.list_datastore_namespaces_for_owners([owner]):
            out[int(n["id"])] = self._entry(n, shared=False)
        for n in db.list_datastore_namespaces_granted_to(self.sub, org_ids, group_ids):
            if int(n["id"]) in out:
                continue
            out[int(n["id"])] = self._entry(n, shared=True, permission=n.get("permission"))
        return list(out.values())

    def _default_owner(self) -> tuple[str, str]:
        """Owner d'un namespace créé sans précision = l'**org ACTIVE** (suppression du
        perso ; `current_org` toujours posé). Filet `user` si jamais None (ne devrait
        plus arriver)."""
        from . import access
        oid = access.current_org(self.sub)
        return ("org", str(oid)) if oid is not None else ("user", self.sub)

    def create_namespace(
        self, namespace: str, *, owner_type: Optional[str] = None, owner_id: Optional[str] = None,
    ) -> dict:
        """Crée un namespace. Défaut = **org active** de l'user (plus de perso). Pour un
        classeur d'org/groupe précis, passer `owner_type`/`owner_id` — l'autorisation
        (appartenance) est vérifiée par l'appelant (capacité/route)."""
        if owner_type is None:
            owner_type, owner_id = self._default_owner()
        oid = owner_id if owner_id is not None else self.sub
        try:
            db.create_datastore_namespace(owner_type, oid, namespace)
        except ValueError as e:
            raise NamespaceExists(str(e))
        return {"namespace": namespace, "url": _ns_url(namespace)}

    def delete_namespace(self, namespace: str) -> None:
        ns_id = self._resolve(namespace)
        if not ownership.can_govern(self.sub, "datastore_namespace", str(ns_id)):
            raise NamespaceForbidden(namespace)
        db.delete_datastore_namespace_by_id(ns_id)  # rows + grants partent avec

    def resolve_ns_id(self, namespace: str) -> int:
        """ns_id d'un namespace visible par l'acteur (lève `NamespaceNotFound`).
        Surface publique pour les chemins de gouvernance (partage/transfert)."""
        return self._resolve(namespace)

    def get_url(self, namespace: str) -> str:
        self._resolve(namespace)  # 404 si inconnu
        return _ns_url(namespace)

    # --- mode typé (ADR 0032 §6 / 0029, B6) ----------------------------------

    def get_schema(self, namespace: str) -> Optional[dict]:
        ns_id = self._resolve(namespace)
        ns = db.get_datastore_namespace_by_id(ns_id)
        return (ns or {}).get("schema")

    def set_schema(self, namespace: str, schema: Optional[dict]) -> dict:
        """Pose (ou retire si None) le schéma typé d'un namespace. Exige le droit
        d'écriture. SOFT : pas de validation des rows existantes (schéma de rendu)."""
        ns_id = self._resolve(namespace, write=True)
        if schema is not None and not isinstance(schema, dict):
            raise ValueError("schema doit être un objet {fields:[...]} ou null")
        db.set_datastore_schema(ns_id, schema)
        return {"namespace": namespace, "schema": schema}

    # --- row ops -------------------------------------------------------------

    def append_row(self, namespace: str, data: dict) -> dict:
        ns_id = self._resolve(namespace, write=True)
        user_data = {k: v for k, v in data.items() if k not in _META_COLS}
        row = db.datastore_insert_row(ns_id, _new_id(), user_data)
        return self._row_to_dict(row)

    def upsert_row(self, namespace: str, row_id: str, data: dict) -> tuple[dict, bool]:
        """Écrit une row à une clé `row_id` EXPLICITE (≠ append_row qui génère un
        id), en remplaçant si elle existe. Crée le namespace au besoin. Sert le
        stockage dédupliqué par clé stable (ex. urn LinkedIn). Renvoie
        `(row, inserted)` — `inserted` False = la row existait déjà."""
        try:
            ns_id = self._resolve(namespace, write=True)
        except NamespaceNotFound:
            _ot, _oid = self._default_owner()
            db.create_datastore_namespace(_ot, _oid, namespace)
            self._scope_cache = None  # le nouveau ns doit être visible à la résolution
            ns_id = self._resolve(namespace, write=True)
        user_data = {k: v for k, v in data.items() if k not in _META_COLS}
        row, inserted = db.datastore_upsert_row(ns_id, row_id, user_data)
        return self._row_to_dict(row), inserted

    def get_row(self, namespace: str, row_id: str) -> dict:
        ns_id = self._resolve(namespace)
        row = db.datastore_get_row(ns_id, row_id)
        if not row:
            raise RowNotFound(row_id)
        return self._row_to_dict(row)

    def list_rows(
        self,
        namespace: str,
        filter: Optional[dict] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Filtre exact k:v en Python (chemin MCP `data_rows`). Ordre stable plus
        ancien d'abord (compat historique)."""
        ns_id = self._resolve(namespace)
        out: list[dict] = []
        for row in db.datastore_list_rows(ns_id, order_by="_created_at", order_dir="asc"):
            record = self._row_to_dict(row)
            if filter and not all(str(record.get(k)) == str(v) for k, v in filter.items()):
                continue
            out.append(record)
            if len(out) >= limit:
                break
        return out

    def page_rows(
        self,
        namespace: str,
        *,
        offset: int = 0,
        limit: int = 50,
        order_by: Optional[str] = None,
        order_dir: str = "desc",
        q: Optional[str] = None,
        filters: Optional[list] = None,
    ) -> dict:
        """Page server-side (tri/recherche/filtres SQL) + total — pour le dashboard.
        `filters` = filtres par colonne (liste `{field, op, value}`, combinés AND).
        Renvoie `{rows, total, offset, limit}`."""
        ns_id = self._resolve(namespace)
        rows = db.datastore_list_rows(
            ns_id, offset=offset, limit=limit, order_by=order_by,
            order_dir=order_dir, q=q, filters=filters)
        return {
            "rows": [self._row_to_dict(r) for r in rows],
            "total": db.datastore_count_rows(ns_id, q=q, filters=filters),
            "offset": offset, "limit": limit,
        }

    def update_row(self, namespace: str, row_id: str, patch: dict) -> dict:
        ns_id = self._resolve(namespace, write=True)
        existing = db.datastore_get_row(ns_id, row_id)
        if not existing:
            raise RowNotFound(row_id)
        data = dict(existing.get("data") or {})
        for k, v in patch.items():
            if k in _META_COLS:
                continue
            data[k] = v
        row = db.datastore_update_row(ns_id, row_id, data, _now_iso())
        return self._row_to_dict(row)

    def delete_row(self, namespace: str, row_id: str) -> None:
        ns_id = self._resolve(namespace, write=True)
        if not db.datastore_delete_row(ns_id, row_id):
            raise RowNotFound(row_id)
