"""Les champs que l'appelant n'écrit pas (#586, #606) — l'EFFET EN BASE, sur un
vrai PostgreSQL.

Le banc stubé (`test_champs_reserves_586_606.py`) prouve la règle ; celui-ci prouve
ce que porte la base, jamais ce que le store a bien voulu rendre (même parti que
`test_write_by_id_effect`) : la couche posée par le système est bien dans le blob et
se lit par son adresse (`raison_sociale.origine`), un refus ne laisse aucune trace,
et `data_patch_schema` pose ET lève un cran sans réécrire — la levée ne touchant
aucune ligne.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp.datastore.errors import RowValidationError


SCHEMA = {
    "key": "siren",
    "fields": [
        {"key": "siren", "type": "text"},
        {"key": "raison_sociale", "type": "text"},
        {"key": "adresse", "type": "text", "readonly": True},
    ],
}


def _store():
    from oto_mcp.datastore.core import make_store
    return make_store("sub-test")


@pytest.fixture
def table(live):
    """Un tableau sous les deux crans, et UNE ligne remise par le client."""
    from oto_mcp import db
    ns = "t-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", "sub-test", ns)
    ns = str(ns_id)
    st = _store()
    pose = st.set_schema(ns, SCHEMA)
    # ⚠️ `origine` a quitté cette liste le 08/09/2026 : `origine: "system"` est
    # SUPPRIMÉ. Une version qui l'annoncerait encore mentirait sur ce qu'elle
    # applique — c'est exactement ce que `enforced` existe pour empêcher.
    assert "readonly" in set(pose["enforced"])
    assert "origine" not in set(pose["enforced"])
    row = st.append_row(ns, {"siren": "552032534", "raison_sociale": "TEMOIN",
                             "adresse": "1 rue A"})
    return st, ns, ns_id, row["_id"]


def _donnees(ns_id: int, row_id: str) -> dict:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        r = conn.execute(
            "SELECT data FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id)).fetchone()
    return dict((r or {}).get("data") or {})


def test_une_valeur_identique_ne_detruit_pas_le_comment_en_base(table):
    """v1.165.0, trou du terrain : le comment posé sur une colonne source tombait au
    round-trip suivant. Sur `raison_sociale` (libre) l'identique est un no-op qui
    garde les couches ; sur `adresse` (readonly) il est refusé — et garde les couches."""
    st, ns, ns_id, rid = table
    st.update_row(ns, rid, {"raison_sociale": {"comment": "vérifiée"},
                            "adresse": {"comment": "registre — 2 rue B"}})
    st.update_row(ns, rid, {"raison_sociale": "TEMOIN"})
    st.update_row(ns, rid, {"adresse": "1 rue A"})           # identique : no-op
    d = _donnees(ns_id, rid)
    assert d["raison_sociale"] == {"valeur": "TEMOIN", "comment": "vérifiée"}
    assert d["adresse"] == {"valeur": "1 rue A", "comment": "registre — 2 rue B"}


ENRICHISSEMENT = {f"enrich_{i:02d}": f"valeur {i}" for i in range(1, 21)}


@pytest.fixture
def terrain(live):
    """Le tableau du terrain : `raison_sociale` verrouillée ET à origine système."""
    from oto_mcp import db
    ns = "t-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", "sub-test", ns)
    ns = str(ns_id)
    st = _store()
    st.set_schema(ns, {"key": "siren", "fields": [
        {"key": "siren", "type": "text"},
        {"key": "raison_sociale", "type": "text", "readonly": True},
        *({"key": k, "type": "text"} for k in ENRICHISSEMENT),
    ]})
    row = st.append_row(ns, {"siren": "552032534", "raison_sociale": "TEMOIN"})
    return st, ns, ns_id, row["_id"]


def test_terrain_1_valeur_et_origine_identiques_acceptees_rien_de_perdu(terrain):
    """① `{"raison_sociale": {"valeur": <identique>, "origine": <identique>}}`."""
    st, ns, ns_id, rid = terrain
    st.update_row(ns, rid, {"raison_sociale": {"comment": "vérifiée"}})
    st.update_row(ns, rid, {"raison_sociale": {"valeur": "TEMOIN", "origine": "TEMOIN"}},
                  origine_override=True)
    assert _donnees(ns_id, rid)["raison_sociale"] == {"valeur": "TEMOIN", "origine": "TEMOIN",
                                                     "comment": "vérifiée"}


def test_terrain_2_la_fiche_entiere_passe(terrain):
    """② le même, plus vingt colonnes d'enrichissement — la fiche passe en entier,
    par le chemin de `data_write(row=…)` (fusion sur la clé) ET par `id`."""
    st, ns, ns_id, rid = terrain
    fiche = {"siren": "552032534",
             "raison_sociale": {"valeur": "TEMOIN", "origine": "TEMOIN"}, **ENRICHISSEMENT}
    out = st.append_row(ns, fiche, upsert=True)
    assert out["_id"] == rid
    st.update_row(ns, rid, dict(fiche, enrich_01="valeur 1 bis"))
    d = _donnees(ns_id, rid)
    assert d["raison_sociale"] == {"valeur": "TEMOIN", "origine": "TEMOIN"}
    assert d["enrich_01"] == "valeur 1 bis" and d["enrich_20"] == "valeur 20"


def test_terrain_3_une_valeur_differente_est_refusee_rien_d_ecrit(terrain):
    """③ `{"raison_sociale": <valeur différente>}` → refusé, rien d'écrit."""
    st, ns, ns_id, rid = terrain
    avant = _donnees(ns_id, rid)
    with pytest.raises(RowValidationError, match="`raison_sociale`"):
        st.update_row(ns, rid, {"raison_sociale": "AUTRE", **ENRICHISSEMENT})
    with pytest.raises(RowValidationError, match="`raison_sociale`"):
        st.append_row(ns, {"siren": "552032534", "raison_sociale": "AUTRE"}, upsert=True)
    assert _donnees(ns_id, rid) == avant


def test_terrain_4_le_comment_survit_a_la_valeur_identique(terrain):
    """④ `{"raison_sociale": {"comment": "x"}}` puis `{"raison_sociale": <identique>}`."""
    st, ns, ns_id, rid = terrain
    st.update_row(ns, rid, {"raison_sociale": {"comment": "x"}})
    st.update_row(ns, rid, {"raison_sociale": "TEMOIN"})
    assert _donnees(ns_id, rid)["raison_sociale"] == {"valeur": "TEMOIN", "comment": "x"}


def test_patch_schema_refuse_readonly_sur_la_cle(table):
    st, ns, ns_id, rid = table
    with pytest.raises(ValueError, match="new_rows"):
        st.patch_schema(ns, fields=[{"key": "siren", "readonly": True}])
    assert "readonly" not in str(st.get_schema(ns)["fields"][0])

