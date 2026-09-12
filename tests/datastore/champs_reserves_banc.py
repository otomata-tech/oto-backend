"""Le banc des champs réservés (#586/#606), partagé par deux fichiers de tests.

Un module (pas un conftest) : les constantes s'importent, et la fixture `banc`
s'importe avec elles — pytest la reconnaît dans l'espace de noms du module de test."""
from __future__ import annotations

import pytest

from oto_mcp.datastore import core as dsm

# ── le banc des champs réservés (#586/#606) — `banc`, partagé par
# test_champs_reserves_586_606.py et test_champs_reserves_identique.py ──────────
FIELDS = [{"key": "siren", "type": "text"},
           {"key": "raison_sociale", "type": "text"},
           {"key": "adresse", "type": "text", "readonly": True},
           {"key": "naf", "type": "text", "readonly": True},
           {"key": "libre", "type": "text"}]
SCHEMA = {"key": "siren", "fields": FIELDS}
LIGNE = {"siren": "552081317", "raison_sociale": "ACME",
          "adresse": "1 rue A", "naf": "62.01Z"}


def _fake_merge_locked(rows):
    """Stub du seam verrou de ligne (#197), comme `test_datastore_key_required`."""
    def merge_locked(ns_id, row_id, apply_fn, updated_at, **k):
        if row_id not in rows:
            return None
        merged = apply_fn(dict(rows[row_id]))
        rows[row_id] = dict(merged)
        return ({"row_id": row_id, "created_at": "t0", "updated_at": updated_at,
                 "data": dict(merged)}, merged)
    return merge_locked


@pytest.fixture()
def banc(monkeypatch):
    """Un tableau `viviers` d'UNE ligne, schéma commutable.

    Rend `(store, etat)` — `etat["lignes"]` est la base, `etat["creees"]` relève les
    insertions réellement parties, `etat["maj"]` les mises à jour par identifiant :
    c'est ce qui distingue « rien n'a été écrit » d'une erreur rendue après coup."""
    st = dsm.DatastorePg("u", acting_org=35)
    etat = {"schema": SCHEMA, "lignes": {"r1": dict(LIGNE)}, "creees": [], "maj": []}
    monkeypatch.setattr(st, "_resolve", lambda ns, write=False: 7)
    monkeypatch.setattr(dsm.db, "get_datastore_by_id",
                        lambda ns_id: {"id": ns_id, "datastore": "viviers",
                                       "schema": etat["schema"]})

    def find(ns_id, key, kv):
        for rid, data in etat["lignes"].items():
            if key and str(data.get(key)) == str(kv):
                return rid
        return None

    def insert(ns_id, rid, data, *a, **k):
        etat["creees"].append(data)
        etat["lignes"][rid] = dict(data)
        return {"row_id": rid, "created_at": "t", "updated_at": "t", "data": data}

    def get_row(ns_id, rid):
        data = etat["lignes"].get(rid)
        return ({"row_id": rid, "created_at": "t", "updated_at": "t",
                 "data": dict(data)} if data is not None else None)

    monkeypatch.setattr(dsm.db, "datastore_find_row_id_by_key", find)
    monkeypatch.setattr(dsm.db, "datastore_get_row", get_row)
    monkeypatch.setattr(dsm.db, "datastore_insert_row", insert)
    monkeypatch.setattr(dsm.db, "datastore_active_lease", lambda ns_id, rid: None)
    fusion = _fake_merge_locked(etat["lignes"])

    def fusion_relevee(ns_id, rid, apply_fn, updated_at, **k):
        # Le patch par `id` écrit par la fusion sous verrou depuis le 12/09/2026 : une
        # fusion ABOUTIE est une écriture, un refus levé dans `apply_fn` n'en est pas une.
        sortie = fusion(ns_id, rid, apply_fn, updated_at, **k)
        if sortie is not None:
            etat["maj"].append(rid)
        return sortie

    monkeypatch.setattr(dsm.db, "datastore_merge_row_locked", fusion_relevee)
    return st, etat


