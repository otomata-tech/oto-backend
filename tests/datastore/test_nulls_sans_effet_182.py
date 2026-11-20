"""Un `null` qui n'efface rien ne s'écrit pas (oto#182).

**Pourquoi ce filtre existe.** La lecture sert à `null` toute colonne déclarée sans
valeur en place. Un agent qui relit une ligne puis la réémet renvoie ces `null` : écrits,
ils ajoutaient une clé et faisaient tourner la révision sur un geste qui ne change rien. La suite complète
l'a montré sur cinq bancs d'aller-retour.

**Ce qu'il ne doit PAS toucher.** Un `null` sur une valeur EN PLACE reste l'effacement
nommé. Ce banc tient les deux moitiés : la règle pure, sans base, et ses
effets sur les trois chemins d'écriture, sur PostgreSQL jetable.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp.datastore.columns import sans_les_nulls_sans_effet


def _jamais_appele():
    raise AssertionError("la ligne en place a été lue alors qu'aucun `null` n'est écrit")


# ── la règle, sans base ──────────────────────────────────────────────────────

def test_sans_null_la_ligne_en_place_nest_jamais_lue():
    corps = {"raison": "ACME", "site_web": ""}
    assert sans_les_nulls_sans_effet(corps, _jamais_appele) is corps


@pytest.mark.parametrize("en_place", [{}, {"site_web": None}, {"site_web": ""},
                                      {"site_web": {"comment": "à vérifier"}}],
                         ids=["absente", "null_stocke", "vide", "couche_seule"])
def test_un_null_sans_valeur_en_place_nest_pas_ecrit(en_place):
    sortie = sans_les_nulls_sans_effet({"raison": "ACME", "site_web": None},
                                       lambda: en_place)
    assert sortie == {"raison": "ACME"}


def test_un_null_sur_une_valeur_en_place_reste_un_effacement():
    corps = {"site_web": None, "effectif": {"valeur": None}}
    en_place = {"site_web": "acme.fr", "effectif": {"valeur": 12, "origine": "registre"}}
    assert sans_les_nulls_sans_effet(corps, lambda: en_place) == corps


def test_une_valeur_nulle_en_couches_garde_ce_qui_laccompagne():
    sortie = sans_les_nulls_sans_effet(
        {"site_web": {"valeur": None, "comment": "cherché, rien trouvé"}}, lambda: {})
    assert sortie == {"site_web": {"comment": "cherché, rien trouvé"}}


def test_les_colonnes_techniques_ne_sont_pas_filtrees():
    sortie = sans_les_nulls_sans_effet({"_claimed_by": None, "raison": None},
                                       lambda: {})
    assert sortie == {"_claimed_by": None}


def test_un_null_hors_du_declare_sans_valeur_en_place_ne_cree_pas_la_colonne():
    """Un `null` sur une colonne non déclarée n'est pas refusé : il efface. Sans valeur
    en place il n'efface rien, et l'écrire ferait naître la colonne à `null`, hors
    schéma (oto-backend#1292)."""
    assert sans_les_nulls_sans_effet({"raison": "ACME", "sit_web": None},
                                     lambda: {"raison": "X"}) == {"raison": "ACME"}


def test_un_null_hors_du_declare_efface_la_valeur_en_place():
    corps = {"sit_web": None}
    assert sans_les_nulls_sans_effet(corps, lambda: {"sit_web": "a.fr"}) == corps


# ── les trois chemins, sur PostgreSQL ────────────────────────────────────────

def _table():
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns_id = db.create_datastore("user", "sub-test", "t182n-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    st = make_store("sub-test")
    st.set_schema(ns, {"key": "siren", "fields": [
        {"key": "siren", "type": "text"}, {"key": "raison", "type": "text"},
        {"key": "site_web", "type": "text"}]})
    return st, ns, ns_id


def _stockee(ns_id, rid):
    from oto_mcp import db
    return db.datastore_get_row(ns_id, rid)


def test_reemettre_une_ligne_lue_ne_change_rien_et_ne_previent_pas(live):
    st, ns, ns_id = _table()
    rid = st.append_row(ns, {"siren": "1", "raison": "ACME"})["_id"]
    lu = st.get_row(ns, rid)
    avant = _stockee(ns_id, rid)
    st.off_notices.clear()

    st.update_row(ns, rid, {k: v for k, v in lu.items() if not k.startswith("_")})

    assert _stockee(ns_id, rid)["data"] == avant["data"], "un `null` a été écrit"
    assert _stockee(ns_id, rid)["rev"] == avant["rev"], "la révision a tourné"
    assert not st.off_notices, "une notice est partie sur une réémission"


def test_effacer_une_valeur_en_place_efface_sans_preavis(live):
    """oto#140 (23/09/2026) : `null` efface, définitivement — plus de préavis servi."""
    st, ns, ns_id = _table()
    rid = st.append_row(ns, {"siren": "2", "raison": "BETA", "site_web": "beta.fr"})["_id"]
    st.off_notices.clear()

    st.update_row(ns, rid, {"site_web": None})

    assert st.get_row(ns, rid)["site_web"] is None
    assert not any("`site_web`" in n for n in st.off_notices), st.off_notices


def test_creation_et_lot_ne_stockent_pas_un_null_sans_effet(live):
    st, ns, ns_id = _table()
    rid = st.append_row(ns, {"siren": "3", "raison": "GAMMA", "site_web": None})["_id"]
    assert "site_web" not in _stockee(ns_id, rid)["data"]

    st.write_rows(ns, [{"siren": "3", "raison": "GAMMA", "site_web": None}], key="siren")
    assert "site_web" not in _stockee(ns_id, rid)["data"], "le lot a écrit le `null`"
    assert st.get_row(ns, rid)["site_web"] is None, "la colonne reste servie à null"
