"""Toute colonne DÉCLARÉE est servie, à `null` sans valeur (oto#182).

**Le défaut tenu fermé.** Dans une ligne JSONB stockée, une colonne jamais écrite n'existe
pas : elle était donc ABSENTE du document servi. Pour un agent, ce sont deux lignes
différentes — clé absente, il fabrique une valeur (un domaine composé depuis la raison
sociale, 67 fois sur 72 le 11/09) ; `null`, il la cherche.

Ce banc tient la projection elle-même (`DatastorePg._row_to_dict`, le point par lequel
passe toute ligne servie), sans base : la complétion, et tout ce qu'elle ne doit PAS
toucher — une valeur présente, une colonne masquée à l'agent, les clés techniques, les
couches, l'ordre du schéma. Le rejeu sur PostgreSQL vit dans `..._182_live.py`.
"""
from __future__ import annotations

import pytest

from oto_mcp import session_org
from oto_mcp.datastore import layers as dsl
from oto_mcp.datastore import schema as dsv2
from oto_mcp.datastore.core import DatastorePg
from oto_mcp.tools.datastore import _project_row

SCHEMA = {"fields": [
    {"key": "raison", "type": "text"},
    {"key": "site_web", "type": "text"},
    {"key": "contacts", "type": "list"},
    {"key": "effectif", "type": "number"},
    {"key": "suivi", "type": "text", "agent_access": "none"},
]}
_META = {"_id", "_created_at", "_updated_at"}


def _servie(data: dict, schema=SCHEMA, **kw) -> dict:
    ligne = {"row_id": "r1", "created_at": "2026-09-13T00:00:00Z",
             "updated_at": "2026-09-13T00:00:00Z", "data": data}
    return DatastorePg._row_to_dict(ligne, schema, **kw)


@pytest.fixture
def face_agent():
    jeton = session_org.set_call_face(session_org.FACE_MCP)
    try:
        yield
    finally:
        session_org.reset_call_face(jeton)


@pytest.mark.parametrize("layers", [dsl.DEFAUT, dsl.NESTED])
def test_une_colonne_jamais_ecrite_est_servie_null(layers):
    out = _servie({"raison": "ACME"}, layers=layers)
    for cle in ("site_web", "contacts", "effectif", "suivi"):
        assert cle in out and out[cle] is None, (
            f"`{cle}` déclarée mais jamais écrite : clé absente ou valeur fabriquée")
    assert out["contacts"] is None, "une liste jamais écrite vaut null, pas []"


def test_une_valeur_presente_est_servie_telle_quelle():
    """`""`, `null` écrit, une couche seule : servis exactement comme sans schéma. La
    complétion n'ajoute que ce qui MANQUE — elle ne normalise rien."""
    data = {"raison": "", "site_web": None, "effectif": {"comment": "à vérifier"}}
    sans = _servie(data, schema=None)
    avec = _servie(data)
    for cle, valeur in sans.items():
        assert avec[cle] == valeur, f"`{cle}` a changé de valeur servie"
    assert avec["raison"] == "", "un vide écrit a été servi null"


def test_une_colonne_masquee_reste_absente_pour_un_agent(face_agent):
    out = _servie({"raison": "ACME"})
    assert "suivi" not in out, "la complétion a fait réapparaître une colonne masquée"
    assert out["site_web"] is None


def test_le_proprietaire_voit_la_colonne_masquee_a_null():
    assert _servie({"raison": "ACME"})["suivi"] is None


def test_ni_cle_technique_ni_couche_nest_fabriquee():
    schema = {"fields": [{"key": "_claimed_by", "type": "text"},
                         {"key": "site_web", "type": "text"}]}
    out = _servie({}, schema=schema, versions=("current", "origine"))
    assert "_claimed_by" not in out, "une clé technique déclarée a été complétée"
    assert out["site_web"] is None
    assert not [c for c in out if c.startswith("site_web.")], "une couche a été fabriquée"


def test_les_colonnes_completees_suivent_lordre_du_schema():
    cles = [c for c in _servie({}) if c not in _META]
    assert cles == ["raison", "site_web", "contacts", "effectif", "suivi"]


@pytest.mark.parametrize("schema", [
    None, {}, {"fields": []}, {"fields": [{"type": "text"}, {"key": 3}, {"key": ""}]}],
    ids=["aucun", "vide", "sans_champ", "cles_malformees"])
def test_un_schema_absent_ou_malforme_ne_complete_rien(schema):
    assert _servie({"a": 1}, schema=schema) == _servie({"a": 1}, schema=None)


def test_une_projection_garde_la_colonne_declaree_a_null():
    out = _project_row(_servie({"raison": "ACME"}), ["site_web", "raison"])
    assert out == {"_id": "r1", "raison": "ACME", "site_web": None}


def test_cles_declarees_garde_lordre_et_retire_les_doublons():
    schema = {"fields": [{"key": "b"}, {"key": "a"}, {"key": "b"}, {"key": None}]}
    assert dsv2.cles_declarees(schema) == ["b", "a"]
