"""J1 du contrat d'écriture d'une case (oto#140) : l'import honnête.

Deux défauts relevés par une campagne le 11/09/2026, rejoués ici sur la base réelle
AVANT d'être corrigés — chaque banc a d'abord été rouge.

- **oto#164** : une origine `""` (le marqueur « rien n'avait été remis » de l'ancienne
  capture paresseuse) comptait comme DÉJÀ POSÉE. Un ré-import `donnees_d_origine=true`
  écrivait la valeur remise et laissait le marqueur figé, sans un mot : le relevé des
  colonnes était calculé puis jeté par les quatre appelants.
- **oto#165** : `{}` sur une case vide passait tous les types (le validateur le juge
  vide) et se stockait comme valeur (la fusion le garde), sans un mot.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp.datastore import schema as dsv2


def _store():
    from oto_mcp.datastore.core import make_store
    return make_store("sub-test")


def _blob(ns_id: int, row_id: str) -> dict:
    """Ce que porte la BASE, jamais ce que le store a bien voulu rendre."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        r = conn.execute("SELECT data FROM datastore_rows WHERE ns_id=%s AND row_id=%s",
                         (ns_id, row_id)).fetchone()
    return dict((r or {}).get("data") or {})


def _tableau(fields=None):
    from oto_mcp import db
    ns_id = db.create_datastore("user", "sub-test", "t-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    _store().set_schema(ns, {"key": "siren", "fields": fields or [
        {"key": "siren", "type": "text"}, {"key": "email", "type": "text"},
        {"key": "nom", "type": "text"}]})
    return ns, ns_id


def _ligne_au_marqueur(ns_id: int) -> str:
    """L'état exact de la case relevée : le marqueur seul, et le marqueur sous une
    valeur — tels que l'ancienne capture les a laissés, écrits en base directement."""
    from oto_mcp import db
    db.datastore_insert_row(ns_id, "r1", {
        "siren": {"valeur": "1", "origine": {"valeur": "1"}},
        "email": {"origine": ""},
        "nom": {"valeur": "Y", "origine": ""}})
    return "r1"


def _notices(store) -> str:
    return " ".join(store.off_schema_report().get("notices") or [])


# ── oto#164 : une origine vide est ABSENTE, un ré-import la pose ────────────────

_CHEMINS = {
    "fusion": lambda st, ns, rid, row: st.append_row(ns, row, donnees_d_origine=True,
                                                     upsert=True),
    "patch_par_id": lambda st, ns, rid, row: st.update_row(
        ns, rid, {k: v for k, v in row.items() if k != "siren"}, donnees_d_origine=True),
    "lot": lambda st, ns, rid, row: st.write_rows(ns, [row], key="siren",
                                                  donnees_d_origine=True),
}


@pytest.mark.parametrize("chemin", sorted(_CHEMINS))
def test_un_reimport_pose_l_origine_sur_le_marqueur_vide(live, chemin):
    ns, ns_id = _tableau()
    rid = _ligne_au_marqueur(ns_id)
    st = _store()
    _CHEMINS[chemin](st, ns, rid, {"siren": "1", "email": "x@x", "nom": "Z"})

    blob = _blob(ns_id, rid)
    assert blob["email"] == {"valeur": "x@x", "origine": {"valeur": "x@x"}}, (
        "le marqueur seul n'est pas une origine posée : le ré-import la pose")
    assert blob["nom"] == {"valeur": "Z", "origine": {"valeur": "Z"}}, (
        "le marqueur sous une valeur non plus")
    notices = _notices(st)
    assert "origin SET on `email` (1), `nom` (1)" in notices, notices


@pytest.mark.parametrize("chemin", sorted(_CHEMINS))
def test_une_origine_pleine_reste_figee_et_le_saut_est_dit(live, chemin):
    ns, ns_id = _tableau()
    rid = _ligne_au_marqueur(ns_id)
    st = _store()
    _CHEMINS[chemin](st, ns, rid, {"siren": "1", "email": "x@x", "nom": "Z"})
    st2 = _store()
    _CHEMINS[chemin](st2, ns, rid, {"siren": "1", "email": "autre@x", "nom": "W"})

    blob = _blob(ns_id, rid)
    assert blob["email"] == {"valeur": "autre@x", "origine": {"valeur": "x@x"}}, (
        "une origine non vide ne bouge jamais : le courant bouge, l'origine non")
    notices = _notices(st2)
    assert "origin NOT set, already set, on `email` (1), `nom` (1)" in notices, notices
    assert "origin SET" not in notices or "`email`" not in notices.split(
        "origin SET")[1].split("—")[0], notices


def test_le_releve_cumule_sur_un_lot_et_nomme_chaque_raison(live):
    ns, ns_id = _tableau()
    st = _store()
    st.write_rows(ns, [{"siren": "1", "email": "a@x", "nom": ""},
                       {"siren": "2", "email": "b@x", "nom": "  "},
                       {"siren": "3", "email": "c@x",
                        "nom": {"valeur": "N", "origine": "écrite"}}],
                  key="siren", donnees_d_origine=True, origine_override=True)
    notices = _notices(st)
    assert "origin SET on `email` (3), `siren` (3)" in notices, notices
    assert "origin NOT set, empty value, on `nom` (2)" in notices, notices
    assert "origin NOT set, written by the caller, on `nom` (1)" in notices, notices
    assert sum("origin SET" in n for n in st.off_schema_report()["notices"]) == 1, (
        "une phrase par raison sur le lot entier, jamais une par ligne")


def test_la_creation_unitaire_dit_aussi_ce_qu_elle_pose(live):
    ns, ns_id = _tableau()
    st = _store()
    st.append_row(ns, {"siren": "9", "email": "e@x"}, donnees_d_origine=True)
    assert "origin SET on `email` (1), `siren` (1)" in _notices(st)


def test_l_origine_posee_par_le_parametre_n_est_pas_ecrite_sans_le_dire(live):
    """Découvert en rejouant #164 : sur une ligne EXISTANTE, l'origine que la
    plateforme pose pour `donnees_d_origine=true` entrait dans le relevé des origines
    écrites par l'appelant sans le déclarer (oto#70), qui sont refusées. Le ré-import
    aurait été refusé pour avoir fait ce qu'il déclarait."""
    ns, ns_id = _tableau()
    from oto_mcp import db
    db.datastore_insert_row(ns_id, "r1", {"siren": "1", "email": "ancien@x"})
    for chemin in ("fusion", "patch_par_id", "lot"):
        st = _store()
        _CHEMINS[chemin](st, ns, "r1", {"siren": "1", "email": "x@x"})
        assert "origine_warning" not in st.off_schema_report(), chemin


# ── oto#165 : `{}` n'est jamais stocké comme valeur ──────────────────────────

_TYPES = [{"key": "siren", "type": "text"}, {"key": "n", "type": "number"},
          {"key": "etat", "type": "enum", "options": ["a", "b"]},
          {"key": "t", "type": "text"}]


def _ecartes(store) -> list:
    return [e["champ"] for e in store.off_schema_report().get("valeurs_ecartees") or []]


def test_creation_unitaire_n_ecrit_pas_l_objet_vide(live):
    ns, ns_id = _tableau(_TYPES)
    st = _store()
    ligne = st.append_row(ns, {"siren": "1", "n": {}, "etat": {}, "t": {}, "libre": {}})
    blob = _blob(ns_id, ligne["_id"])
    assert set(blob) == {"siren"}, blob
    assert sorted(_ecartes(st)) == ["etat", "libre", "n", "t"]
    assert "@empty" in st.off_schema_report()["valeurs_ecartees"][0]["motif"]


def test_fusion_n_ecrit_pas_l_objet_vide_sur_une_case_vide(live):
    ns, ns_id = _tableau(_TYPES)
    ligne = _store().append_row(ns, {"siren": "1", "t": "garde"})
    st = _store()
    st.append_row(ns, {"siren": "1", "n": {}, "t": "neuf"}, upsert=True)
    blob = _blob(ns_id, ligne["_id"])
    assert "n" not in blob and blob["t"] == "neuf", blob
    assert _ecartes(st) == ["n"]


def test_patch_par_id_n_ecrit_pas_l_objet_vide_et_ne_rompt_rien(live):
    """Le `{}` SEUL sur une case vide : écarté et dit, jamais refusé — J1 ne rompt
    rien pour les appelants qui l'envoient aujourd'hui."""
    ns, ns_id = _tableau(_TYPES)
    ligne = _store().append_row(ns, {"siren": "1"})
    st = _store()
    st.update_row(ns, ligne["_id"], {"n": {}})
    assert "n" not in _blob(ns_id, ligne["_id"])
    assert _ecartes(st) == ["n"]


def test_lot_n_ecrit_pas_l_objet_vide(live):
    ns, ns_id = _tableau(_TYPES)
    st = _store()
    st.write_rows(ns, [{"siren": "1", "etat": {}}, {"siren": "2", "etat": "a"}],
                  key="siren")
    lignes = {r["siren"]: r for r in st.list_rows(ns)}
    assert lignes["1"].get("etat") is None and lignes["2"]["etat"] == "a"
    assert _ecartes(st) == ["etat"]


def test_les_couches_qui_accompagnent_l_objet_vide_restent(live):
    ns, ns_id = _tableau(_TYPES)
    st = _store()
    ligne = st.append_row(ns, {"siren": "1", "t": {"valeur": {}, "comment": "cherché"}})
    assert _blob(ns_id, ligne["_id"])["t"] == {"comment": "cherché"}
    assert _ecartes(st) == ["t"]


def test_sur_une_valeur_en_place_le_chemin_608_est_inchange(live):
    """`{}` sur une valeur EN PLACE reste l'affaire de #608/#724 : préservée et dite,
    refusée quand elle est tout le geste. Ce lot n'y touche pas."""
    ns, ns_id = _tableau(_TYPES)
    ligne = _store().append_row(ns, {"siren": "1", "t": "garde"})
    with pytest.raises(ValueError, match="write without effect"):
        _store().update_row(ns, ligne["_id"], {"t": {}})
    assert _blob(ns_id, ligne["_id"])["t"] == "garde"
    assert dsv2.unwrap(_blob(ns_id, ligne["_id"])["t"]) == "garde"
