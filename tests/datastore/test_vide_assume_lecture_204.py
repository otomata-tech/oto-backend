"""Le vide ASSUMÉ se lit, se valide et se sert — sans jamais s'émettre (oto#204, étape 1).

**Pourquoi un marqueur.** Un sous-champ requis doit pouvoir porter « aucune source ne donne
ce titre, et je l'écris » sans que l'agent invente la valeur. Une chaîne vide ordinaire et
une cellule CSV vide ne le disent pas : elles restent refusées. Le vide assumé est rangé
dans l'enveloppe de la cellule, `{"valeur": "", "oto.vide_assume": true}`.

**Pourquoi une étape 1 sans émission.** Préprod et prod partagent la base : la version qui
LIT le marqueur doit être servie partout avant que quiconque en ÉCRIVE un. Ce banc tient
cette étape, sans base : le nom, la lecture sans fuite, `required`, la fusion, le refus
d'un client qui écrirait la clé, l'absence d'émission, et la page publique.
"""
from __future__ import annotations

import json

import pytest

from oto_mcp import db, share_ui
from oto_mcp.datastore import couches as dsl
from oto_mcp.datastore import layers as dslayers
from oto_mcp.datastore.columns import _merge_column, refuser_cles_internes
from oto_mcp.datastore.core import DatastorePg
from oto_mcp.datastore.definition import validate_schema_def
from oto_mcp.datastore.errors import RowValidationError
from oto_mcp.datastore.validation import validate_row

MARQUEE = {"valeur": "", dsl.VIDE_ASSUME: True}
SCHEMA = {"strict": True, "fields": [
    {"key": "raison", "type": "text"},
    {"key": "fonction", "type": "text", "required": True},
    {"key": "contacts", "type": "list", "of": {"type": "object", "fields": [
        {"key": "nom", "type": "text"},
        {"key": "fonction", "type": "text", "required": True}]}},
]}


def _aucune_trace(obj) -> None:
    assert "vide_assume" not in json.dumps(obj, ensure_ascii=False, default=str), (
        "le marqueur interne FUIT dans ce qui est servi")


def _servie(data: dict, layers: str) -> dict:
    ligne = {"row_id": "r1", "created_at": "t", "updated_at": "t", "data": data}
    return DatastorePg._row_to_dict(ligne, SCHEMA, layers=layers)


# ── le nom ───────────────────────────────────────────────────────────────────

def test_le_nom_nest_aucune_couche_publique():
    assert dsl.VIDE_ASSUME not in dsl.ALL_LAYER_KEYS
    assert "." in dsl.VIDE_ASSUME, "le point est ce qui interdit la collision"


@pytest.mark.parametrize("schema", [
    {"fields": [{"key": dsl.VIDE_ASSUME, "type": "text"}]},
    {"fields": [{"key": "contacts", "type": "list", "of": {"type": "object", "fields": [
        {"key": dsl.VIDE_ASSUME, "type": "text"}]}}]},
], ids=["colonne", "sous_champ"])
def test_aucun_schema_ne_peut_declarer_une_cle_de_ce_nom(schema):
    assert validate_schema_def(schema), (
        "un schéma a pu déclarer le nom du marqueur : une donnée d'utilisateur pourrait le porter")


# ── la lecture : `""` partout, jamais le marqueur ────────────────────────────

@pytest.mark.parametrize("layers", [dslayers.DEFAUT, dslayers.NESTED])
def test_une_cellule_marquee_se_sert_vide_sans_fuite(layers):
    out = _servie({"raison": "ACME", "fonction": MARQUEE,
                   "contacts": [{"nom": "X", "fonction": MARQUEE}]}, layers)
    assert out["fonction"] == ""
    assert out["contacts"][0]["fonction"] == ""
    _aucune_trace(out)


def test_une_cellule_marquee_annotee_garde_son_annotation():
    cellule = {**MARQUEE, "comment": "aucune source ne donne ce titre"}
    plat = _servie({"fonction": cellule}, dslayers.DEFAUT)
    imbrique = _servie({"fonction": cellule}, dslayers.NESTED)
    assert (plat["fonction"], plat["fonction.comment"]) == ("", cellule["comment"])
    assert imbrique["fonction"] == {"valeur": "", "comment": cellule["comment"]}
    _aucune_trace(plat), _aucune_trace(imbrique)


# ── la validation ────────────────────────────────────────────────────────────

def test_le_vide_assume_satisfait_required_en_colonne_et_en_sous_champ():
    ligne = {"fonction": MARQUEE, "contacts": [{"nom": "X", "fonction": MARQUEE}]}
    assert validate_row(SCHEMA, ligne) == []


@pytest.mark.parametrize("vide", ["", {"valeur": ""}, None],
                         ids=["chaine_vide", "valeur_vide", "absente"])
def test_un_vide_ordinaire_reste_refuse(vide):
    ligne = {"contacts": [{"nom": "X", "fonction": "CEO"}]}
    if vide is not None:
        ligne["fonction"] = vide
    erreurs = validate_row(SCHEMA, ligne)
    assert any("fonction" in e and "requis" in e for e in erreurs), erreurs


def test_un_sous_champ_vide_ordinaire_reste_refuse():
    erreurs = validate_row(SCHEMA, {"fonction": "DG", "contacts": [{"nom": "X", "fonction": ""}]})
    assert any("fonction" in e and "requis" in e for e in erreurs), erreurs


def test_le_marqueur_nest_pas_un_sous_champ_inconnu():
    ligne = {"fonction": {**MARQUEE, "comment": "rien trouvé"},
             "contacts": [{"nom": "X", "fonction": MARQUEE}]}
    assert not [e for e in validate_row(SCHEMA, ligne) if "inconnu" in e]


# ── la fusion ────────────────────────────────────────────────────────────────

def test_renvoyer_le_vide_servi_garde_le_marqueur():
    assert _merge_column(MARQUEE, "") == MARQUEE
    annotee = {**MARQUEE, "comment": "rien trouvé"}
    assert _merge_column(annotee, "") == annotee


@pytest.mark.parametrize("existant, nouveau, attendu", [
    (MARQUEE, "Directrice", "Directrice"),
    ({**MARQUEE, "origine": "registre"}, "Directrice",
     {"valeur": "Directrice", "origine": "registre"}),
    (MARQUEE, {"valeur": "Directrice", "comment": "site"},
     {"valeur": "Directrice", "comment": "site"}),
], ids=["scalaire", "avec_origine", "en_couches"])
def test_une_vraie_valeur_fait_tomber_le_marqueur(existant, nouveau, attendu):
    assert _merge_column(existant, nouveau) == attendu


@pytest.mark.parametrize("existant, nouveau", [
    (None, dsl.VIDE_DELIBERE), ("Directrice", dsl.VIDE_DELIBERE),
    (None, {"valeur": dsl.VIDE_DELIBERE, "comment": "rien trouvé"}),
])
def test_l_etape_1_n_emet_jamais_le_marqueur(existant, nouveau):
    """`@empty` se résout toujours en `""` dans cette étape. L'émission du marqueur est
    l'étape 2, qui ne touche `main` qu'une fois celle-ci servie en production."""
    _aucune_trace(_merge_column(existant, nouveau))


# ── un client n'écrit pas la clé interne ─────────────────────────────────────

@pytest.mark.parametrize("corps", [
    {"fonction": MARQUEE},
    {"contacts": [{"nom": "X", "fonction": MARQUEE}]},
    {"brut": {"a": {dsl.VIDE_ASSUME: True}}},
], ids=["colonne", "sous_champ", "json_profond"])
def test_un_client_qui_ecrit_la_cle_interne_est_refuse(corps):
    with pytest.raises(RowValidationError) as e:
        refuser_cles_internes(corps)
    assert dsl.VIDE_DELIBERE in str(e.value), "le refus doit nommer le geste à employer"


def test_le_geste_empty_nest_pas_refuse():
    refuser_cles_internes({"fonction": dsl.VIDE_DELIBERE,
                           "contacts": [{"fonction": {"valeur": dsl.VIDE_DELIBERE}}]})


# ── la page publique lit la base brute ───────────────────────────────────────

def test_la_page_publique_d_un_tableau_partage_ne_montre_pas_le_marqueur(monkeypatch):
    projet = {"id": 5, "name": "Projet démo", "brief_md": "", "mcp_access": "secret",
              "mcp_expose_datastore": True, "mcp_expose_docs": True}
    monkeypatch.setattr(db, "list_project_links", lambda pid: [
        {"target_type": "tableau", "target_ref": "22", "label": "Vivier",
         "datastore": "vivier"}])
    monkeypatch.setattr(db, "list_docs_for_project", lambda pid: [])
    monkeypatch.setattr(db, "get_datastore_by_id",
                        lambda rid: {"datastore": "vivier", "schema": SCHEMA})
    monkeypatch.setattr(db, "datastore_count_rows", lambda rid: 1)
    monkeypatch.setattr(db, "datastore_list_rows", lambda rid, **kw: [{"data": {
        "raison": "Alice SA", "fonction": {**MARQUEE, "comment": "rien trouvé"},
        "contacts": [{"nom": "X", "fonction": MARQUEE}]}}])

    html, status = share_ui.build_page(projet, "/data/22", connect_url="u")

    assert status == 200 and "Alice SA" in html
    assert "vide_assume" not in html, "le marqueur interne fuit dans la page publique"
    assert "vide_assume" not in share_ui._cell(MARQUEE)


def test_renvoyer_une_liste_A_cle_d_element_garde_le_marqueur():
    """Sur une liste à `of.key`, la fusion se fait élément par élément et attribut par
    attribut : le `""` servi est la même valeur, le marqueur reste. (Sans clé, la liste se
    remplace en bloc — limite prouvée par le banc live.)"""
    champ = {"key": "contacts", "type": "list", "of": {"type": "object", "key": "role",
             "fields": [{"key": "role", "type": "text"},
                        {"key": "fonction", "type": "text", "required": True}]}}
    en_place = [{"role": "rh", "fonction": MARQUEE}]
    servie = [{"role": "rh", "fonction": ""}]
    assert _merge_column(en_place, servie, champ) == en_place
