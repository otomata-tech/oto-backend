"""Un refus de validation porte la CHARGE EXACTE à renvoyer (oto#135, J4 d'oto#140).

Le défaut : un agent qui reçoit une erreur qu'il ne sait pas corriger rejoue à
l'identique, ou abandonne la ligne — mesuré, cinq refus sur quatre-vingt-onze écritures
d'une soirée de campagne. Un refus qui nomme le geste exact est suivi dans la minute.

La charge est un fragment de `row` calculé DEPUIS LA DÉCLARATION : les seuls champs à
corriger, un gabarit `<…>` à la place de chaque valeur, `@empty` en alternative là où il
est permis, et dans une liste l'élément fautif SEUL — désigné par son `of.key`, sinon
par son rang. Jamais la liste entière : pas de données recopiées.

Quatre familles : requis manquant, type ou format, sous-champ inconnu, couche exigée.
REST rend `details` tel quel ; MCP recopie la charge en fin de message.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from oto_mcp.datastore import schema as dsv2
from oto_mcp.datastore import charge_a_renvoyer as car
from oto_mcp.datastore.errors import RowValidationError

_CONTACTS = {"key": "contacts", "type": "list", "of": {"key": "role", "fields": [
    {"key": "role", "type": "text", "required": True},
    {"key": "nom", "type": "text", "required": True},
    {"key": "email", "type": "email"}]}}
_SCHEMA = {"fields": [
    {"key": "siren", "type": "text", "required": True, "max_length": 9,
     "pattern": "^[0-9]{9}$"},
    {"key": "effectif", "type": "number"},
    _CONTACTS,
    {"key": "tags", "type": "list", "of": {"type": "enum", "options": ["a", "b"]}},
    {"key": "siege", "type": "object", "fields": [
        {"key": "ville", "type": "text", "required": True}]},
    {"key": "qualif", "type": "text", "required_layers": ["comment"]}]}


def _refus(schema: dict, row: dict, **kw) -> tuple[list, dict]:
    details: dict = {}
    errs = dsv2.validate_row(schema, row, details=details, **kw)
    assert errs, "la ligne aurait dû être refusée"
    return errs, details


# ── requis manquant ──────────────────────────────────────────────────────────

def test_requis_de_colonne_gabarit_et_empty_en_alternative():
    _, d = _refus(_SCHEMA, {"effectif": 3})
    assert d["a_renvoyer"] == {
        "siren": "<text, ≤ 9 characters, pattern ^[0-9]{9}$> | @empty"}
    assert "a_renvoyer_elements" not in d


def test_requis_de_sous_champ_l_element_SEUL_designe_par_son_identite():
    _, d = _refus(_SCHEMA, {"siren": "123456789", "contacts": [
        {"role": "RH", "nom": "Alice Martin", "email": "alice@x.fr"},
        {"role": "DAF"}]})
    assert d["a_renvoyer"] == {"contacts": [{"role": "DAF", "nom": "<text> | @empty"}]}
    assert d["a_renvoyer_elements"] == ["contacts[role=DAF]"]
    assert "Alice" not in json.dumps(d), "jamais la liste entière : pas de données recopiées"


def test_requis_sans_of_key_l_element_designe_par_son_RANG():
    schema = {"fields": [{"key": "contacts", "type": "list", "of": {"fields": [
        {"key": "nom", "type": "text", "required": True}]}}]}
    _, d = _refus(schema, {"contacts": [{"nom": "Alice"}, {"fonction": "DAF"}]})
    assert d["a_renvoyer"] == {"contacts": [{"nom": "<text> | @empty"}]}
    assert d["a_renvoyer_elements"] == ["contacts[1]"]


def test_l_identite_d_un_element_n_offre_pas_empty():
    """`@empty` est refusé sur l'identité (`of.key`) : l'offrir ferait rejouer un refus."""
    _, d = _refus(_SCHEMA, {"siren": "123456789", "contacts": [{"nom": "Bob"}]})
    assert d["a_renvoyer"] == {"contacts": [{"role": "<text>"}]}


def test_un_sous_champ_d_objet_n_offre_pas_empty():
    """`@empty` est refusé dans un objet."""
    _, d = _refus(_SCHEMA, {"siren": "123456789", "siege": {"cp": "75001"}})
    assert d["a_renvoyer"] == {"siege": {"ville": "<text>"}}


# ── type ou format ───────────────────────────────────────────────────────────

def test_type_de_colonne():
    _, d = _refus(_SCHEMA, {"siren": "123456789", "effectif": "beaucoup"})
    assert d["a_renvoyer"] == {"effectif": "<number>"}


def test_format_dans_un_element():
    _, d = _refus(_SCHEMA, {"siren": "123456789", "contacts": [
        {"role": "RH", "nom": "Alice", "email": "pas-un-mail"}]})
    assert d["a_renvoyer"] == {"contacts": [{"role": "RH", "email": "<email>"}]}


def test_motif_de_colonne():
    _, d = _refus(_SCHEMA, {"siren": "12345678X"})
    assert d["a_renvoyer"] == {"siren": "<text, ≤ 9 characters, pattern ^[0-9]{9}$>"}


def test_option_d_une_liste_de_valeurs_par_rang():
    _, d = _refus(_SCHEMA, {"siren": "123456789", "tags": ["a", "z"]})
    assert d["a_renvoyer"] == {"tags": ["<a | b>"]}
    assert d["a_renvoyer_elements"] == ["tags[1]"]


def test_type_sur_un_tableau_souple():
    """Le type s'arme seul, hors `validation_active` (`types_declares`) : même charge."""
    _, d = _refus({"fields": [{"key": "effectif", "type": "number"}]},
                  {"effectif": "beaucoup"})
    assert d["a_renvoyer"] == {"effectif": "<number>"}


# ── sous-champ inconnu ───────────────────────────────────────────────────────

def test_attribut_inconnu_la_cle_declaree_la_plus_proche():
    errs, d = _refus({**_SCHEMA, "unknown_columns": "report"}, {"siren": "123456789", "contacts": [
        {"role": "RH", "nom": "Alice", "emial": "alice@x.fr"}]})
    assert "closest: `email`" in errs[0], errs
    assert d["a_renvoyer"] == {"contacts": [{"role": "RH", "email": "<email>"}]}
    assert "alice@x.fr" not in json.dumps(d)


def test_couche_inconnue_la_couche_la_plus_proche():
    errs, d = _refus(_SCHEMA, {"siren": "123456789",
                               "effectif": {"valeur": 3, "commentaire": "registre"}})
    assert "closest: `comment`" in errs[0], errs
    assert d["a_renvoyer"] == {"effectif": {"comment": "<where the value comes from, in plain words>"}}


# ── couche exigée ────────────────────────────────────────────────────────────

def test_couche_exigee_de_colonne():
    _, d = _refus(_SCHEMA, {"siren": "123456789", "qualif": "oui"})
    assert d["a_renvoyer"] == {"qualif": {"valeur": "<text>",
                                          "comment": "<where the value comes from, in plain words>"}}


def test_couche_exigee_dans_un_element():
    schema = {"fields": [{"key": "contacts", "type": "list", "of": {"key": "role", "fields": [
        {"key": "role", "type": "text"},
        {"key": "email", "type": "text", "required_layers": ["link"]}]}}]}
    _, d = _refus(schema, {"contacts": [{"role": "RH", "email": "a@x.fr"}]})
    assert d["a_renvoyer"] == {"contacts": [
        {"role": "RH", "email": {"valeur": "<text>", "link": "<the URL of the source>"}}]}


# ── ce qui n'entre PAS dans la charge ────────────────────────────────────────

def test_un_element_non_ecrit_n_entre_pas_dans_la_charge():
    """Il n'est pas jugé contre le geste (oto#137) : lui demander d'être renvoyé
    serait faire réparer à l'agent ce qu'il n'est pas venu toucher."""
    avant = {"siren": "123456789", "contacts": [{"role": "RH", "nom": "Alice"},
                                                {"role": "DAF"}]}
    apres = {"siren": "123456789", "contacts": [
        {"role": "RH", "nom": "Alice", "email": "x"}, {"role": "DAF"}]}
    _, d = _refus(_SCHEMA, apres, written={"contacts"}, en_place=avant)
    assert d["a_renvoyer"] == {"contacts": [{"role": "RH", "email": "<email>"}]}


def test_une_colonne_gelee_n_entre_pas_dans_la_charge():
    details: dict = {}
    errs = dsv2.validate_row(_SCHEMA, {"siren": "123456789", "effectif": "x"},
                             written={"siren"}, details=details)
    assert errs == [] and "a_renvoyer" not in details


# ── les deux faces ───────────────────────────────────────────────────────────

def test_la_clause_mcp_porte_la_charge_et_la_regle_de_la_liste():
    _, d = _refus(_SCHEMA, {"siren": "123456789", "contacts": [{"role": "DAF"}]})
    texte = car.clause(d)
    assert json.dumps(d["a_renvoyer"], ensure_ascii=False) in texte
    assert "contacts[role=DAF]" in texte and "WHOLE" in texte


def test_la_face_REST_rend_details_tel_quel():
    from oto_mcp.capabilities.datastore.rows import _write_refusal
    _, d = _refus(_SCHEMA, {"effectif": 3})
    refus = _write_refusal(RowValidationError(["x"], details=d))
    assert refus.details["a_renvoyer"] == {
        "siren": "<text, ≤ 9 characters, pattern ^[0-9]{9}$> | @empty"}


def _table(schema: dict) -> str:
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = "t-" + uuid.uuid4().hex[:6]
    ns = str(db.create_datastore("user", "sub-test", ns))
    make_store("sub-test").set_schema(ns, schema)
    return ns


def test_la_face_MCP_finit_le_message_par_la_charge(live, monkeypatch):
    """Le vrai outil, tel que le serveur le monte : la charge traverse `data_write`."""
    from fastmcp import FastMCP

    from oto_mcp.datastore.core import make_store
    from oto_mcp.tools import datastore as T
    monkeypatch.setattr(T, "_acting_store", lambda: make_store("sub-test"))
    monkeypatch.setattr(T, "_ns", lambda ns: ns)
    monkeypatch.setattr(T, "_project_hint", lambda ns: None)
    m = FastMCP("banc-135-charge")
    T.register(m)
    ns = _table({"fields": [_CONTACTS]})

    with pytest.raises(Exception) as e:
        asyncio.run(m.call_tool("data_write", {"datastore": ns, "row": {
            "contacts": [{"role": "RH", "nom": "Alice"}, {"role": "DAF"}]}}))
    msg = str(e.value)
    assert "contacts[1].nom" in msg, msg
    assert ('To send back, with the `<…>` templates replaced: '
            '{"contacts": [{"role": "DAF", "nom": "<text> | @empty"}]}') in msg, msg
    assert "Alice" not in msg.split("To send back")[1], "l'élément fautif seul"


def test_un_lot_garde_la_charge(live):
    """Un refus de lot change de désignation, pas de `details`."""
    from oto_mcp.datastore.core import make_store
    ns = _table({"fields": [{"key": "siren", "type": "text"}, _CONTACTS]})
    with pytest.raises(RowValidationError) as e:
        make_store("sub-test").write_rows(ns, [
            {"siren": "1", "contacts": [{"role": "RH", "nom": "A"}]},
            {"siren": "2", "contacts": [{"role": "DAF"}]}], key="siren")
    assert e.value.details["a_renvoyer"] == {
        "contacts": [{"role": "DAF", "nom": "<text> | @empty"}]}
