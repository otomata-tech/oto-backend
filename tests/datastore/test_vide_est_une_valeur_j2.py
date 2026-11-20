"""Le vide est une valeur (oto#140, jalon J2) — et `null` qui démarque.

- **le vide remplace** : `""` / `[]` sur une valeur en place la REMPLACE, sur les trois
  chemins (cf. `test_regle_finale_j2_j3.py`), et plus aucune annonce datée n'est servie ;
- **`null` retire le vide assumé** : `@clear` est refusé, `null` efface donc une case
  marquée `@empty`, marqueur compris. Un `null` sur une case vraiment vide reste sans effet
  (oto#182).
"""
from __future__ import annotations

import pathlib
import uuid

import pytest

from oto_mcp.datastore import couches as dsl
from oto_mcp.datastore.columns import sans_les_nulls_sans_effet

MARQUEE = {"valeur": "", dsl.VIDE_ASSUME: True}


# ── le texte, sans base ──────────────────────────────────────────────────────

def test_le_guide_dit_la_regle_au_present():
    guide = (pathlib.Path(__file__).parents[2]
             / "oto_mcp/guides/datastore-semantics.md").read_text()
    assert "6 octobre 2026" not in guide and "8 octobre 2026" not in guide
    assert "6 October 2026" not in guide and "8 October 2026" not in guide
    assert "they REPLACE the value in place**" in guide


def test_le_module_du_preavis_est_retire():
    import importlib.util

    assert importlib.util.find_spec("oto_mcp.datastore.vide_remplace") is None


# ── `null` sur un vide assumé, la règle ──────────────────────────────────────

@pytest.mark.parametrize("en_place", [MARQUEE, {**MARQUEE, "comment": "registre : rien"}],
                         ids=["nu", "commente"])
def test_un_null_sur_un_vide_assume_est_ecrit(en_place):
    corps = {"raison": "ACME", "fonction": None}
    assert sans_les_nulls_sans_effet(corps, lambda: {"fonction": en_place}) == corps


def test_un_null_sur_un_vide_ordinaire_reste_sans_effet():
    sortie = sans_les_nulls_sans_effet({"raison": "ACME", "fonction": None},
                                       lambda: {"fonction": ""})
    assert sortie == {"raison": "ACME"}


# ── sur PostgreSQL ───────────────────────────────────────────────────────────

def _table():
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns_id = db.create_datastore("user", "sub-test", "tj2-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    st = make_store("sub-test")
    st.set_schema(ns, {"key": "siren", "fields": [
        {"key": "siren", "type": "text"}, {"key": "raison", "type": "text"},
        {"key": "site_web", "type": "text"}, {"key": "fonction", "type": "text"},
        {"key": "tags", "type": "list", "of": {"type": "text"}}]})
    return st, ns, ns_id


def _data(ns_id, rid):
    from oto_mcp import db
    return db.datastore_get_row(ns_id, rid)["data"]


@pytest.mark.parametrize("chemin", ["par_id", "fusion", "lot"])
def test_null_efface_un_vide_assume_marqueur_compris(live, chemin):
    st, ns, ns_id = _table()
    rid = st.append_row(ns, {"siren": "5", "fonction": {
        "valeur": dsl.VIDE_DELIBERE, "comment": "registre : rien"}})["_id"]
    assert dsl.vide_assume(_data(ns_id, rid)["fonction"]), "le banc ne pose pas le marqueur"

    if chemin == "par_id":
        st.update_row(ns, rid, {"fonction": None})
    elif chemin == "fusion":
        st.append_row(ns, {"siren": "5", "fonction": None}, upsert=True)
    else:
        st.write_rows(ns, [{"siren": "5", "fonction": None}], key="siren")

    assert not dsl.vide_assume(_data(ns_id, rid).get("fonction")), _data(ns_id, rid)
    assert dsl.unwrap(_data(ns_id, rid).get("fonction")) is None
    assert st.get_row(ns, rid, empties="sentinel")["fonction"] is None


def test_null_sur_une_case_vraiment_vide_reste_sans_effet(live):
    from oto_mcp import db
    st, ns, ns_id = _table()
    rid = st.append_row(ns, {"siren": "6", "raison": "F"})["_id"]
    avant = db.datastore_get_row(ns_id, rid)
    st.update_row(ns, rid, {"fonction": None})
    apres = db.datastore_get_row(ns_id, rid)
    assert apres["data"] == avant["data"] and apres["rev"] == avant["rev"]
