"""Le contrat d'écriture d'une case, règle finale (oto#140, J2 et J3).

- **J2** : `""` et `[]` REMPLACENT la valeur en place, et la valeur remplacée revient
  dans `valeurs_effacees` ; les gardes #608 (vide ignoré) et #724 (« écriture sans
  effet ») ne valent plus que pour `{}` ;
- **J3** : une écriture qui porte `@keep` ou `@clear`, où que ce soit, est REFUSÉE
  entière, et le refus nomme le geste à la place — jamais stockée, jamais traduite.

Les préavis datés de ces deux jalons sont retirés : la règle s'applique sans date ni
réglage.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp.datastore import couches as dsl
from oto_mcp.datastore import mots_deprecies as mdp
from oto_mcp.datastore.columns import (
    arbitrer_les_vides,
    effacements_report,
    ignores_report,
    refuser_geste_sans_effet,
)
from oto_mcp.datastore.errors import RowValidationError


# ── J2 : l'arbitrage des vides ───────────────────────────────────────────────

EN_PLACE = {"site": "a.fr", "tags": ["x"], "meta": {"k": 1}}


def test_j2_chaine_et_liste_vides_remplacent_et_se_disent():
    pose, effaces, ecartes = arbitrer_les_vides(
        EN_PLACE, {"site": "", "tags": {"valeur": [], "comment": "vu"}, "meta": {}}, "r1")
    assert pose == {"site": "", "tags": {"valeur": [], "comment": "vu"}}
    assert effaces == [{"ligne": "r1", "champ": "site", "valeur": "a.fr"},
                       {"ligne": "r1", "champ": "tags", "valeur": ["x"]}]
    # `{}` n'est pas une valeur : toujours écarté, et le relevé ne cite que lui.
    assert [r["champ"] for r in ecartes] == ["meta"]
    hint = ignores_report(ecartes)["valeurs_ignorees_hint"]
    assert "empty object `{}`" in hint and "empty string" not in hint
    assert "REPLACE" in effacements_report(effaces)["valeurs_effacees_hint"]


def test_j2_un_vide_seul_n_est_pas_refuse_sauf_objet_vide():
    pose, _, ecartes = arbitrer_les_vides(EN_PLACE, {"site": ""})
    refuser_geste_sans_effet(pose, ecartes)
    assert ecartes == []
    pose, _, ecartes = arbitrer_les_vides(EN_PLACE, {"meta": {}})
    with pytest.raises(ValueError, match=r"empty object `\{\}`"):
        refuser_geste_sans_effet(pose, ecartes)


# ── J3 : le refus des mots retirés ───────────────────────────────────────────

CORPS = {"a": dsl.EFFACEMENT, "b": {"valeur": "x", "comment": dsl.GARDE},
         "c": [{"nom": "Alice", "fonction": dsl.GARDE}]}


def test_j3_l_ecriture_est_refusee_et_le_refus_nomme_le_geste():
    with pytest.raises(RowValidationError) as e:
        mdp.controler({"x": "ok"}, CORPS)
    t = str(e.value)
    assert "write refused" in t and "nothing was written" in t
    assert "`@clear` (`a`)" in t and "`null`" in t
    assert "`@keep` (`b`, `c`)" in t and "leave the sub-field out" in t
    assert "send its value back as it is" in t
    assert "depuis le" not in t and "octobre" not in t


def test_j3_rien_ne_mord_sans_mot():
    mdp.controler({"a": None, "b": dsl.VIDE_DELIBERE, "c": "contact@keepcalm.example"})


# ── le texte servi : la règle au présent, sans date ──────────────────────────

def test_la_description_de_data_write_dit_la_regle_au_present():
    from oto_mcp.tools import datastore as outil

    assert "REFUSED whole" in mdp.DESCRIPTION_ECRITURE
    assert "write `null`" in mdp.DESCRIPTION_ECRITURE
    src = outil.__loader__.get_source(outil.__name__)
    assert "<<vide_remplace>>" not in src
    assert "REPLACE the value in place" in src
    for date in ("2026-10-06", "2026-10-08", "From 2026", "until then", "Until then"):
        assert date not in mdp.DESCRIPTION_ECRITURE


# ── sur PostgreSQL ───────────────────────────────────────────────────────────

SUB = "usr_regle_finale_140"


def _table():
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store

    db.upsert_user(SUB, email=f"{SUB}@regle-finale.invalid", name=SUB)
    ns_id = db.create_datastore("user", SUB, "tbd-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    st = make_store(SUB)
    st.set_schema(ns, {"key": "siren", "fields": [
        {"key": "siren", "type": "text"}, {"key": "raison", "type": "text"},
        {"key": "site_web", "type": "text"}, {"key": "nom", "type": "text",
                                              "required": True},
        {"key": "tags", "type": "list", "of": {"type": "text"}}]})
    return st, ns, ns_id


def _data(ns_id, rid):
    from oto_mcp import db
    return db.datastore_get_row(ns_id, rid)["data"]




@pytest.mark.parametrize("chemin", ["par_id", "fusion", "lot"])
def test_j2_live_le_vide_remplace_sur_les_trois_chemins(live, chemin):
    st, ns, ns_id = _table()
    rid = st.append_row(ns, {"siren": "2", "nom": "B", "site_web": "b.fr",
                             "tags": ["x", "y"]})["_id"]
    st.off_erased.clear()
    corps = {"site_web": "", "tags": []}
    if chemin == "par_id":
        st.update_row(ns, rid, corps)             # le vide SEUL : plus de #724
    elif chemin == "fusion":
        st.append_row(ns, {"siren": "2", **corps}, upsert=True)
    else:
        st.write_rows(ns, [{"siren": "2", **corps}], key="siren")
    data = _data(ns_id, rid)
    assert data["site_web"] == "" and data["tags"] == []
    rendu = effacements_report(st.off_erased)["valeurs_effacees"]
    assert {(r["champ"], str(r["valeur"])) for r in rendu} == {
        ("site_web", "b.fr"), ("tags", "['x', 'y']")}


def test_j2_live_un_vide_sur_un_requis_reste_refuse_et_compte_vide(live):
    """`""` posé ne satisfait pas `required` — comme sur une case vide aujourd'hui — et
    le filtre `empty` le compte vide. `@empty` reste le seul « cherché, rien »."""
    st, ns, ns_id = _table()
    rid = st.append_row(ns, {"siren": "3", "nom": "C", "site_web": "c.fr"})["_id"]
    with pytest.raises(ValueError, match="required field missing"):
        st.update_row(ns, rid, {"nom": ""})
    assert _data(ns_id, rid)["nom"] == "C"
    with pytest.raises(ValueError, match="required field missing"):
        st.append_row(ns, {"siren": "3b", "nom": ""})

    st.update_row(ns, rid, {"site_web": ""})
    vides = st.page_rows(ns, filters=[{"field": "site_web", "op": "empty"}])
    assert rid in {r["_id"] for r in vides["rows"]}




def test_j3_live_refuse_partout_et_n_ecrit_rien(live):
    st, ns, ns_id = _table()
    rid = st.append_row(ns, {"siren": "5", "nom": "E", "site_web": "e.fr"})["_id"]
    avant = _data(ns_id, rid)

    with pytest.raises(RowValidationError, match=r"`@clear` \(`site_web`\)"):
        st.update_row(ns, rid, {"site_web": dsl.EFFACEMENT})
    with pytest.raises(RowValidationError, match=r"`@keep` \(`site_web`\)"):
        st.append_row(ns, {"siren": "5", "raison": "R",
                           "site_web": {"valeur": "x.fr", "comment": dsl.GARDE}})
    assert _data(ns_id, rid) == avant

    # Le LOT est refusé entier : la première ligne, saine, n'est pas écrite non plus.
    with pytest.raises(RowValidationError, match=r"`@clear` \(`raison`\)"):
        st.write_rows(ns, [{"siren": "6", "nom": "F"},
                           {"siren": "5", "raison": dsl.EFFACEMENT}], key="siren")
    from oto_mcp import db
    assert db.datastore_find_row_id_by_key(ns_id, "siren", "6") is None
    assert _data(ns_id, rid) == avant
