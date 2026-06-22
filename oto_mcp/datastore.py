"""Datastore — substrat natif PostgreSQL (ADR 0016).

Un namespace = une ligne `user_datastores` + ses rows dans `datastore_rows`
(une row = un dict JSONB). Schéma libre : aucune colonne à provisionner, les
champs apparaissent dans `data`. Trois champs auto-managés, exposés à plat dans
la row renvoyée :

- `_id` : identifiant uuid7-like (col `row_id`).
- `_created_at` / `_updated_at` : timestamps (colonnes dédiées).

Plus de dépendance Google : la vérité est en base, types préservés nativement
par JSONB (fin de la sentinelle `__j:` de l'ère Sheets). Le partage est DB-only
(`datastore_shares`) — le destinataire lit via son propre `sub`. L'export vers un
provider tiers (Sheets/Notion…) est une projection optionnelle, déférée à
otomata#29.
"""
from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from . import db


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

    # --- résolution namespace -> (ns_id, writable) ---------------------------

    def _resolve(self, namespace: str, *, write: bool = False) -> int:
        """ns_id d'un namespace possédé ou partagé. `write=True` exige le droit
        d'écriture (owner, ou partage `write`)."""
        own = db.get_datastore_namespace(self.sub, namespace)
        if own:
            return int(own["id"])
        share = db.get_shared_namespace(self.sub, namespace)
        if not share:
            raise NamespaceNotFound(namespace)
        if write and share.get("permission") != "write":
            raise NamespaceReadOnly(namespace)
        owner_ns = db.get_datastore_namespace(share["owner_sub"], namespace)
        if not owner_ns:
            raise NamespaceNotFound(namespace)
        return int(owner_ns["id"])

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

    def list_namespaces(self) -> list[dict]:
        own = db.list_datastore_namespaces(self.sub)
        out = [{"namespace": n["namespace"], "created_at": n.get("created_at"),
                "url": _ns_url(n["namespace"]), "shared": False} for n in own]
        for n in db.list_shared_namespaces(self.sub):
            out.append({"namespace": n["namespace"], "created_at": n.get("created_at"),
                        "url": _ns_url(n["namespace"]), "shared": True,
                        "owner_sub": n.get("owner_sub"), "permission": n.get("permission")})
        return out

    def create_namespace(self, namespace: str) -> dict:
        try:
            db.create_datastore_namespace(self.sub, namespace)
        except ValueError as e:
            raise NamespaceExists(str(e))
        return {"namespace": namespace, "url": _ns_url(namespace)}

    def delete_namespace(self, namespace: str) -> None:
        # Seul le propriétaire supprime (les rows partent en CASCADE).
        if not db.get_datastore_namespace(self.sub, namespace):
            raise NamespaceNotFound(namespace)
        db.delete_datastore_namespace(self.sub, namespace)

    def get_url(self, namespace: str) -> str:
        self._resolve(namespace)  # 404 si inconnu
        return _ns_url(namespace)

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
            db.create_datastore_namespace(self.sub, namespace)
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
        ns_id = self._resolve(namespace)
        out: list[dict] = []
        for row in db.datastore_list_rows(ns_id):
            record = self._row_to_dict(row)
            if filter and not all(str(record.get(k)) == str(v) for k, v in filter.items()):
                continue
            out.append(record)
            if len(out) >= limit:
                break
        return out

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
