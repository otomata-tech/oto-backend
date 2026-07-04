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


def make_org_store(org_id: int) -> "DatastorePg":
    """Store agissant SOUS L'AUTORITÉ d'une ORG, sans user (`sub=None`). Sert un
    endpoint MCP `secret` opt-in datastore (ADR 0032) : la résolution de namespace et
    le droit d'écriture se décident sur le principal ORG (owner-match / grant d'org),
    jamais sur un membre. N'expose PAS la gouvernance (create/delete/rename/share) —
    ces actes restent réservés à un user identifié (tools sub-only)."""
    return DatastorePg(None, acting_org=int(org_id))


class DatastorePg:
    """Store tabulaire adossé à PostgreSQL.

    State-less, instancié par requête. Normalement à partir du `sub` (l'acteur user) ;
    ou, pour un endpoint MCP agissant sous une org (`acting_org`, secret opt-in), avec
    `sub=None` — l'autorité est alors l'org propriétaire. Résout chaque namespace en
    `ns_id` (possédé OU partagé) et opère sur `datastore_rows`.
    """

    def __init__(self, sub: Optional[str], *, acting_org: Optional[int] = None):
        self.sub = sub
        self.acting_org = acting_org
        self._active_scope_cache: Optional[tuple[list[int], list[int]]] = None

    # --- résolution namespace -> ns_id ---------------------------------------

    def _active_scope(self) -> tuple[list[int], list[int]]:
        """Contexte de l'ORG ACTIVE (ADR 0023) : `([org active], [mes groupes dans cette
        org])`. La résolution par NOM scope là-dessus — comme `list_namespaces` — de sorte
        qu'un namespace d'une AUTRE de mes orgs ne se résout plus hors de son org (fuite
        cross-org, symétrique au fix projets). L'ownership PERSO (`owner=user`) et les
        grants perso (`principal user`) suivent l'acteur : ils n'appartiennent à aucune
        org, donc ne sont pas une fuite d'org — `resolve_datastore_ns` les garde via `sub`."""
        if self._active_scope_cache is None:
            if self.acting_org is not None:
                # Endpoint agissant-org (sub-less) : contexte = l'org propriétaire seule,
                # aucun groupe (pas de membre → pas de scope de groupe).
                self._active_scope_cache = ([int(self.acting_org)], [])
                return self._active_scope_cache
            from . import access, group_store
            oid = access.current_org(self.sub)
            if oid is None:
                self._active_scope_cache = ([], [])
            else:
                org = int(oid)
                groups = [int(g["group_id"])
                          for g in group_store.list_groups_for_user(self.sub, org)]
                self._active_scope_cache = ([org], groups)
        return self._active_scope_cache

    def _resolve(self, namespace: str, *, write: bool = False) -> int:
        """ns_id d'un namespace VISIBLE DANS L'ORG ACTIVE (possédé par elle, perso, ou
        accordé à son contexte). `write=True` exige le droit d'écriture via
        `ownership.can_access`."""
        org_ids, group_ids = self._active_scope()
        ns = db.resolve_datastore_ns(
            namespace, sub=self.sub, org_ids=org_ids, group_ids=group_ids)
        if not ns:
            raise NamespaceNotFound(namespace)
        ns_id = int(ns["id"])
        if write:
            ok = (ownership.org_can_access(self.acting_org, "datastore_namespace",
                                           str(ns_id), "write")
                  if self.acting_org is not None
                  else ownership.can_access(self.sub, "datastore_namespace",
                                            str(ns_id), "write"))
            if not ok:
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
        perso = (self.sub is not None
                 and n.get("owner_type") == "user" and n.get("owner_id") == self.sub)
        # Agissant-org (sub-less) : pas de gouvernance via l'endpoint (create/delete/
        # rename/share restent réservés à un user identifié).
        can_govern = (False if self.acting_org is not None
                      else ownership.can_govern(self.sub, "datastore_namespace", str(ns_id)))
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
            "can_govern": can_govern,
            "is_personal": perso,
            "schema": n.get("schema"),   # mode typé optionnel (ADR 0032 §6 / 0029, B6) ; None = table libre
        }

    def list_namespaces(self) -> list[dict]:
        """Namespaces visibles DANS L'ORG ACTIVE (l'org est le contexte, ADR 0023) :
        possédés par l'org active + accordés à elle ou à MES équipes dans cette org
        (grants d'org/groupe — tous mes groupes de l'org active, pas seulement le
        groupe actif : un partage d'équipe doit se voir sans basculer). Un namespace
        possédé par une AUTRE org — ou partagé à l'acteur *en propre* (grant user,
        cross-org) — ne fuite PLUS dans la vue d'une org tierce (scope décidé le
        2026-07-01). Dédupliqués par id (priorité possédé). La résolution PAR NOM
        (`_resolve`) scope désormais SUR LE MÊME contexte d'org (2026-07-03) : un
        namespace d'une autre org ne se résout plus hors de son org non plus."""
        from . import access, group_store
        if self.acting_org is not None:
            owner = ("org", str(self.acting_org))
            org, org_ids, group_ids = int(self.acting_org), [int(self.acting_org)], []
        else:
            owner = ownership.active_owner(access.current_org(self.sub))
            if owner is None:
                return []
            org = int(owner[1])
            org_ids = [org]
            group_ids = [int(g["group_id"])
                         for g in group_store.list_groups_for_user(self.sub, org)]
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

    def rename_namespace(self, namespace: str, new_name: str) -> dict:
        """Renomme un namespace (l'id/URL/grants restent stables, keyés par id — cf.
        `db.rename_datastore_namespace_by_id`). Exige le droit de GOUVERNANCE, comme la
        suppression. Le nouveau nom doit être libre chez le même propriétaire (sinon
        `NamespaceExists`) — c'est ce qui lève la collision cross-org du gap #71 avant
        un transfert/merge."""
        ns_id = self._resolve(namespace)
        if not ownership.can_govern(self.sub, "datastore_namespace", str(ns_id)):
            raise NamespaceForbidden(namespace)
        new_name = (new_name or "").strip()
        try:
            db.rename_datastore_namespace_by_id(ns_id, new_name)
        except ValueError as e:
            raise NamespaceExists(str(e))
        return {"id": ns_id, "namespace": new_name, "url": _ns_url(new_name)}

    def resolve_ns_id(self, namespace: str) -> int:
        """ns_id d'un namespace visible par l'acteur (lève `NamespaceNotFound`).
        Surface publique pour les chemins de gouvernance (partage/transfert)."""
        return self._resolve(namespace)

    def resolve_ns_id_for_write(self, namespace: str) -> int:
        """ns_id d'un namespace où l'acteur peut ÉCRIRE (lève `NamespaceNotFound`/
        `NamespaceReadOnly`). Sert à sceller la cible d'un upload signé au mint (org
        active présente) ; l'autz est réappliquée au receive via `ownership.can_access`
        sur `datastore_namespace` (org-agnostique), sans contexte d'org."""
        return self._resolve(namespace, write=True)

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
            self._active_scope_cache = None  # invalide le cache (le ns créé est dans l'org active)
            ns_id = self._resolve(namespace, write=True)
        user_data = {k: v for k, v in data.items() if k not in _META_COLS}
        row, inserted = db.datastore_upsert_row(ns_id, row_id, user_data)
        return self._row_to_dict(row), inserted

    def declared_key(self, namespace: str) -> Optional[str]:
        """Clé métier déclarée au schéma (`schema.key`) — sert la dédup au batch
        write. None si aucune (table libre / schéma sans clé)."""
        k = (self.get_schema(namespace) or {}).get("key")
        return k if isinstance(k, str) and k else None

    def write_rows(self, namespace: str, rows: list, *, key: Optional[str] = None) -> dict:
        """Écrit un LOT de rows en un appel. Si une clé métier est en vigueur (param
        `key` explicite, sinon `schema.key` déclarée), chaque row qui la porte fait un
        UPSERT (merge) sur la row existante de même valeur de clé — pas de doublon ;
        sinon append d'une nouvelle row. Renvoie un récap {inserted, updated, count,
        key, ids}. Résout le namespace UNE fois (write) pour tout le lot."""
        ns_id = self._resolve(namespace, write=True)
        return self._write_rows_to_ns(ns_id, rows, key=key or self.declared_key(namespace))

    def _write_rows_to_ns(self, ns_id: int, rows: list, *, key: Optional[str]) -> dict:
        """Cœur du batch, keyé par `ns_id` déjà résolu (réutilisable hors contexte
        d'org — matérialisation d'un upload signé, où l'org de session est absente)."""
        inserted, updated, ids = 0, 0, []
        for data in rows:
            if not isinstance(data, dict):
                raise ValueError("chaque row doit être un objet")
            user_data = {k: v for k, v in data.items() if k not in _META_COLS}
            kv = user_data.get(key) if key else None
            if key and kv is not None and str(kv) != "":
                existing_id = db.datastore_find_row_id_by_key(ns_id, key, kv)
                if existing_id is not None:
                    cur = db.datastore_get_row(ns_id, existing_id)
                    merged = dict((cur or {}).get("data") or {})
                    merged.update(user_data)
                    db.datastore_update_row(ns_id, existing_id, merged, _now_iso())
                    updated += 1
                    ids.append(existing_id)
                    continue
            row = db.datastore_insert_row(ns_id, _new_id(), user_data)
            inserted += 1
            ids.append(row["row_id"])
        return {"inserted": inserted, "updated": updated, "count": inserted + updated,
                "key": key, "ids": ids}

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
