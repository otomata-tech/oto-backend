"""Poser un schéma dit, par chemin, combien de lignes EN PLACE il condamne déjà
(oto-backend#479) — `existing_violations`, avec échantillon d'identifiants, conséquence
et plafond ANNONCÉ.

Cas fondateur (28/08/2026) : une garde posée sur un sous-champ de liste que 854 lignes
sur 8 910 portaient déjà. Rien ne le disait au moment où on armait ; c'est l'ORDRE des
gestes qui piégeait. Le banc demandé par l'issue est le premier test ci-dessous.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp.datastore import violations_existantes as dsve


# --- le chemin se lit en tête du refus : chaque famille fixée -------------------------

@pytest.mark.parametrize("schema,ligne,attendu", [
    # type d'un sous-champ de liste (`_conformite_scalaire`)
    ({"fields": [{"key": "contacts", "type": "list", "of": {"type": "object", "fields": [
        {"key": "email", "type": "email"}]}}]},
     {"contacts": [{"email": "ok@x.fr"}, {"email": "pas un mail"}]}, "contacts[].email"),
    # attribut non déclaré dans un composite déclaré
    ({"fields": [{"key": "contacts", "type": "list", "of": {
        "type": "object", "fields": [{"key": "email", "type": "text"}]}}]},
     {"contacts": [{"email": "a", "perso": "b"}]}, "contacts[].perso"),
    # type déclaré hors `validation_active` (`types_trahis`, l'autre forme)
    ({"fields": [{"key": "n", "type": "number"}]}, {"n": "abc"}, "n"),
    # options de premier niveau (oto#124)
    ({"fields": [{"key": "statut", "type": "enum", "options": ["a", "b"]}]},
     {"statut": "zz"}, "statut"),
    # borne, et une clé qui porte une espace
    ({"fields": [{"key": "Nom complet", "type": "text", "max_length": 3}]},
     {"Nom complet": "abcdef"}, "Nom complet"),
    # nombre d'éléments
    ({"fields": [{"key": "tags", "type": "list", "max_items": 1}]},
     {"tags": ["a", "b"]}, "tags"),
])
def test_chaque_famille_de_refus_nomme_son_chemin(schema, ligne, attendu):
    assert dsve.juger_ligne(schema, ligne) == {attendu: dsve.AU_GESTE}


def test_required_when_BLOQUE_la_ligne_et_se_dit_bloquant():
    schema = {"fields": [{"key": "statut", "type": "text"},
                         {"key": "motif", "type": "text",
                          "required_when": {"statut": "perdu"}}]}
    assert dsve.juger_ligne(schema, {"statut": "perdu"}) == {"motif": dsve.BLOQUANTE}
    assert dsve.juger_ligne(schema, {"statut": "gagné"}) == {}


def test_une_ligne_sans_cle_metier_se_dit():
    schema = {"key": "ref", "fields": [{"key": "ref", "type": "text"}]}
    assert dsve.juger_ligne(schema, {"autre": 1}) == {"ref": dsve.SANS_CLE}
    assert dsve.juger_ligne(schema, {"ref": {"valeur": ""}}) == {"ref": dsve.SANS_CLE}
    assert dsve.juger_ligne(schema, {"ref": "r1"}) == {}


def test_le_garde_couvre_chaque_cran_que_le_moteur_fait_respecter():
    """Toute sonde de `enforced` que `validate_row` refuse doit armer le relevé : un cran
    neuf ignoré par `arme` laisserait l'existant non examiné, en silence."""
    from oto_mcp.datastore.schema import validate_row
    from oto_mcp.datastore.vocabulaire import _ENFORCEMENT_PROBES
    manques = [cle for cle, schema, ligne, _ in _ENFORCEMENT_PROBES
               if validate_row(schema, ligne) and not dsve.arme(schema)]
    assert manques == []


def test_un_schema_qui_n_arme_rien_ne_balaie_pas():
    """Le format déclaré fait contrat dès qu'une colonne est déclarée (oto#124) : seul un
    schéma sans colonne n'arme rien."""
    assert not dsve.arme({"fields": []})
    assert not dsve.arme(None)


# --- à la pose, sur une vraie base ----------------------------------------------------

def _tableau(lignes: list[dict]):
    """Un tableau dont les colonnes des lignes sont déclarées SANS type ni règle — le
    banc pose ensuite le format qu'il éprouve sur l'existant."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = str(db.create_datastore("user", "sub-479", "t-" + uuid.uuid4().hex[:6]))
    st = make_store("sub-479")
    st.set_schema(ns, {"fields": [{"key": k} for k in sorted({k for l in lignes
                                                               for k in l})]})
    st.write_rows(ns, lignes)
    return st, ns


def test_interdire_un_sous_champ_que_N_lignes_portent_rend_N(live):
    """Le banc de l'issue : poser une interdiction sur un sous-champ de liste que N
    lignes portent rend `existing_violations` = N, avec leurs identifiants."""
    st, ns = _tableau(
        [{"ref": f"r{i}", "contacts": [{"email": f"c{i}@x.fr", "perso": "oui"}]}
         for i in range(3)]
        + [{"ref": "propre", "contacts": [{"email": "p@x.fr"}]}])
    out = st.set_schema(ns, {"key": "ref", "fields": [
        {"key": "ref", "type": "text"},
        {"key": "contacts", "type": "list", "of": {"type": "object", "fields": [
            {"key": "email", "type": "email"}]}}]})
    v = out["existing_violations"]
    assert list(v) == ["contacts[].perso"], v
    assert v["contacts[].perso"]["rows"] == 3
    assert "blocking_rows" not in v["contacts[].perso"]
    ids = {r["_id"] for r in st.list_rows(ns) if r.get("ref") != "propre"}
    assert set(v["contacts[].perso"]["sample_ids"]) == ids
    assert "réécrit ce champ" in v["contacts[].perso"]["consequence"]
    assert out["existing_violations_scope"] == {
        "rows_examined": 4, "rows_total": 4, "complete": True}
    assert "`contacts[].perso` (3)" in out["warning"]


def test_le_blocage_annonce_est_celui_qui_se_produit(live):
    """`blocking_rows` ne vaut que s'il décrit ce qui arrive : une écriture d'une AUTRE
    colonne sur une ligne comptée bloquée est bien refusée."""
    st, ns = _tableau([{"ref": "r1", "statut": "perdu"},
                       {"ref": "r2", "statut": "gagné"}])
    out = st.patch_schema(ns, fields=[
        {"key": "statut", "type": "text"},
        {"key": "motif", "type": "text", "required_when": {"statut": "perdu"}}])
    v = out["existing_violations"]["motif"]
    assert (v["rows"], v["blocking_rows"]) == (1, 1)
    assert "PLUS AUCUNE écriture" in v["consequence"]
    with pytest.raises(Exception) as e:
        st.update_row(ns, v["sample_ids"][0], {"ref": "r1-bis"})
    assert "motif" in str(e.value)


def test_le_type_declare_compte_les_valeurs_non_convertibles(live):
    """Le quatrième cran muet (#284 → #479) : `42` en texte se convertit, pas `abc`."""
    st, ns = _tableau([{"n": "42"}, {"n": "abc"}, {"n": ["x"]}])
    out = st.patch_schema(ns, fields=[{"key": "n", "type": "number"}])
    assert out["existing_violations"]["n"]["rows"] == 2


def test_examine_rien_trouve_est_un_VIDE_pas_une_absence(live):
    st, ns = _tableau([{"n": 1}])
    out = st.patch_schema(ns, fields=[{"key": "n", "type": "number"}])
    assert out["existing_violations"] == {}
    assert out["existing_violations_scope"]["complete"] is True
    assert "existing_violations" not in (out.get("warning") or "")


def test_rien_a_juger_ne_rend_pas_de_releve(live):
    """Tableau vide, ou schéma qui n'arme rien : pas examiné, donc pas de clé."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = str(db.create_datastore("user", "sub-479", "t-" + uuid.uuid4().hex[:6]))
    out = make_store("sub-479").set_schema(ns, {"fields": [{"key": "n",
                                                            "type": "number"}]})
    assert "existing_violations" not in out
    st, ns = _tableau([{"notes": 1}])
    out = st.set_schema(ns, {"fields": []})
    assert "existing_violations" not in out
    assert "existing_violations_scope" not in out


def test_le_plafond_SE_DIT(live, monkeypatch):
    """Un relevé tronqué en silence transforme « je n'ai pas tout regardé » en « il n'y
    a rien » : au plafond, `complete: false` et le `warning` parle de PLANCHERS."""
    monkeypatch.setattr(dsve, "PLAFOND_LIGNES", 3)
    monkeypatch.setattr(dsve, "PAGE", 2)
    st, ns = _tableau([{"n": "abc"} for _ in range(5)])
    out = st.patch_schema(ns, fields=[{"key": "n", "type": "number"}])
    assert out["existing_violations"]["n"]["rows"] == 3
    assert out["existing_violations_scope"] == {
        "rows_examined": 3, "rows_total": 5, "complete": False}
    assert "PLANCHERS" in out["warning"]


def test_la_face_REST_porte_le_releve():
    """Les modèles de sortie filtrent ce qu'ils ne déclarent pas : sans le champ, la
    face REST perdrait le relevé en silence."""
    from oto_mcp.capabilities.datastore.columns import PatchSchemaResult
    from oto_mcp.capabilities.datastore.schema import SchemaPosed
    releve = {"existing_violations": {"n": {"rows": 1, "sample_ids": ["x"],
                                            "consequence": "…"}},
              "existing_violations_scope": {"rows_examined": 1, "rows_total": 1,
                                            "complete": True}}
    for modele in (SchemaPosed, PatchSchemaResult):
        servi = modele.model_validate({"datastore": "t", **releve}).model_dump(
            by_alias=True)
        assert servi["existing_violations"] == releve["existing_violations"]
        assert servi["existing_violations_scope"] == releve["existing_violations_scope"]
