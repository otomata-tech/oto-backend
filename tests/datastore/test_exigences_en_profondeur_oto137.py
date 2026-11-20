"""Une exigence posée sur un sous-champ ARME la validation (oto#137).

Le défaut : le cran qui décide si un tableau valide ses écritures ne cherchait
`required` et `required_when` qu'au premier niveau. Un schéma dont la seule exigence
vivait dans un élément de liste (« chaque contact a un nom ») était accepté, et la
validation restait éteinte : la ligne passait sans erreur ni avertissement. Une garde
déclarée qui n'existe pas — pire qu'une garde absente, parce qu'on cesse de la
chercher.

Trois gestes le ferment :
- l'armement cherche en profondeur (`required`, `required_when`, `max_length`,
  `max_items`, et `options` dans un sous-record) ;
- les deux formes qui resteraient inertes sont refusées à la pose, avec la forme
  correcte : `max_items` dans `of`, des sous-champs sous une colonne sans `type` ;
- dans une liste fusionnée par élément (`of.key`), seuls les éléments ÉCRITS par le
  geste sont jugés : un ancien élément fautif part dans `gelees`, il ne bloque pas
  l'écriture d'un autre.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp.datastore import schema as dsv2
from oto_mcp.datastore.definition import validate_schema_def
from oto_mcp.datastore.errors import RowValidationError


def _contacts(of_extra: dict | None = None, **attr) -> dict:
    return {"fields": [{"key": "contacts", "type": "list",
                        "of": {**(of_extra or {}), "fields": [
                            {"key": "role", "type": "text"},
                            {"key": "nom", "type": "text", **attr}]}}]}


# ── ① l'armement en profondeur ───────────────────────────────────────────────

def test_un_requis_de_sous_champ_ARME_la_validation():
    assert dsv2.validation_active(_contacts(required=True))


def test_un_requis_dans_un_objet_ARME_la_validation():
    assert dsv2.validation_active({"fields": [{"key": "siege", "type": "object", "fields": [
        {"key": "ville", "type": "text", "required": True}]}]})


def test_required_when_de_sous_champ_ARME_la_validation():
    assert dsv2.validation_active(_contacts(required_when={"role": "RH"}))


def test_des_options_EN_PROFONDEUR_arment_la_validation():
    """L'avertissement des options inertes ne regarde que le premier niveau : dans un
    sous-record, une liste de choix inerte était muette."""
    assert dsv2.validation_active(_contacts(options=["a", "b"]))
    assert dsv2.validation_active({"fields": [{"key": "tags", "type": "list",
                                               "of": {"type": "enum",
                                                      "options": ["x", "y"]}}]})


def test_max_items_ARME_la_validation():
    assert dsv2.validation_active({"fields": [
        {"key": "tags", "type": "list", "max_items": 3, "of": {"type": "text"}}]})


def test_des_options_de_COLONNE_sont_jugees_partout():
    """oto#124 : le format déclaré fait contrat sur tout tableau à colonnes — les
    options d'une colonne comprises."""
    assert dsv2.validation_active(
        {"fields": [{"key": "statut", "type": "text", "options": ["a", "b"]}]})


def test_le_requis_de_sous_champ_REFUSE_l_element_qui_ne_le_porte_pas():
    """Éprouvé avant le correctif : zéro erreur sur un élément sans son `nom`."""
    errs = dsv2.validate_row(_contacts(required=True),
                             {"contacts": [{"role": "RH", "nom": "Alice"},
                                           {"role": "DAF"}]})
    assert errs and "contacts[1].nom" in errs[0] and "required" in errs[0], errs


def test_une_option_hors_liste_en_profondeur_est_JUGEE():
    errs = dsv2.validate_row(_contacts(options=["Alice", "Bob"]),
                             {"contacts": [{"role": "RH", "nom": "Carole"}]})
    assert errs and "contacts[0].nom" in errs[0] and "not among the options" in errs[0], errs


# ── ② les formes inertes, refusées à la pose ─────────────────────────────────

def test_max_items_sur_l_element_est_REFUSE_avec_la_forme_correcte():
    errs = validate_schema_def(_contacts(of_extra={"max_items": 2}))
    assert len(errs) == 1, errs
    assert "fields.contacts.of" in errs[0] and "max_items" in errs[0]
    assert '"key": "contacts", "type": "list", "max_items": 2' in errs[0], (
        "le refus donne la forme qui marche")


def test_des_sous_champs_sous_une_colonne_SANS_type_sont_REFUSES():
    errs = validate_schema_def({"fields": [{"key": "siege", "fields": [
        {"key": "ville", "required": True}]}]})
    assert len(errs) == 1 and "`fields` déclaré sans `type`" in errs[0], errs
    assert '"type": "object"' in errs[0]

    errs = validate_schema_def({"fields": [{"key": "contacts", "of": {"fields": [
        {"key": "nom", "required": True}]}}]})
    assert len(errs) == 1 and "`of` déclaré sans `type`" in errs[0], errs
    assert '"type": "list"' in errs[0]


def test_la_forme_correcte_reste_posable():
    assert validate_schema_def({"fields": [{"key": "contacts", "type": "list",
                                            "max_items": 2, "of": {"fields": [
                                                {"key": "nom"}]}}]}) == []


# ── ③ liste à `of.key` : seuls les éléments ÉCRITS sont jugés ────────────────

_A_CLE = _contacts(of_extra={"key": "role"}, required=True)
_AVANT = {"contacts": [{"role": "RH", "nom": "Alice"}, {"role": "DAF"}]}


def test_un_ancien_element_fautif_renvoye_TEL_QUEL_ne_bloque_pas():
    """Le geste normal : relire la liste, corriger UN contact, tout renvoyer. Le
    contact DAF, sans nom depuis avant la déclaration, sort de la fusion identique."""
    apres = {"contacts": [{"role": "RH", "nom": "Alicia"}, {"role": "DAF"}]}
    gelees: list = []
    errs = dsv2.validate_row(_A_CLE, apres, written={"contacts"}, en_place=_AVANT,
                             gelees=gelees)
    assert errs == []
    assert [g["champ"] for g in gelees] == ["contacts[1].nom"], (
        "l'ancien défaut n'est pas tu : il se DIT, comme une colonne non écrite")


def test_l_element_ECRIT_reste_juge():
    apres = {"contacts": [{"role": "RH", "nom": "Alice"},
                          {"role": "DAF", "email": "d@x.fr"}]}
    errs = dsv2.validate_row(_A_CLE, apres, written={"contacts"}, en_place=_AVANT)
    # `email` n'est pas déclaré : le sous-record fermé le refuse aussi (oto#124).
    assert any("contacts[1].nom" in e for e in errs), errs


def test_un_element_NEUF_est_juge():
    apres = {"contacts": [{"role": "RH", "nom": "Alice"}, {"role": "DAF"},
                          {"role": "PAIE"}]}
    errs = dsv2.validate_row(_A_CLE, apres, written={"contacts"}, en_place=_AVANT)
    assert len(errs) == 1 and "contacts[2].nom" in errs[0], errs


def test_sans_etat_anterieur_tout_est_juge():
    """Création, remplacement : tout ce qui est posé vient du geste."""
    errs = dsv2.validate_row(_A_CLE, _AVANT, written=None)
    assert errs and "contacts[1].nom" in errs[0]


def test_une_liste_SANS_of_key_se_juge_en_bloc():
    """Elle se remplace entière : chaque élément est celui du geste."""
    errs = dsv2.validate_row(_contacts(required=True), _AVANT, written={"contacts"},
                             en_place=_AVANT)
    assert errs and "contacts[1].nom" in errs[0]


# ── ④ le vrai chemin : fusion PUIS validation, sous le verrou ────────────────

def _table(schema_initial: dict):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = str(db.create_datastore("user", "sub-test", "t-" + uuid.uuid4().hex[:6]))
    st = make_store("sub-test")
    st.set_schema(ns, schema_initial)
    return st, ns


def test_bout_en_bout_l_ancien_element_ne_bloque_pas_l_ecriture_d_un_autre(live):
    """L'ordre compte : la fusion par créneau d'abord, la validation ensuite, contre
    la ligne en place lue sous le verrou. Le contact DAF est écrit avant que le nom
    ne devienne exigé ; on corrige ensuite le seul contact RH."""
    st, ns = _table({"fields": [
        {"key": "contacts", "type": "list", "of": {"key": "role", "fields": [
            {"key": "role", "type": "text"}, {"key": "nom", "type": "text"}]}}]})
    ligne = st.append_row(ns, {"contacts": [{"role": "RH", "nom": "Alice"},
                                            {"role": "DAF"}]})
    st.set_schema(ns, _A_CLE)

    st.update_row(ns, ligne["_id"], {"contacts": [{"role": "RH", "nom": "Alicia"},
                                                  {"role": "DAF"}]})
    assert "contacts[1].nom" in st.off_geles

    with pytest.raises(RowValidationError) as e:
        st.update_row(ns, ligne["_id"], {"contacts": [
            {"role": "RH", "nom": "Alicia"}, {"role": "DAF", "email": "d@x.fr"}]})
    assert "contacts[1].nom" in str(e.value), "l'élément que le geste touche est jugé"
