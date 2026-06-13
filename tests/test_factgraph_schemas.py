"""Tests purs du registre de schémas factgraph (ADR 0008).

`schemas.validate_fact`/`validate_edge` sont la garde « facts structurés » :
pures (registre seul), testables sans DB — comme `test_org_secret_meta`.
"""
import pytest

from oto_mcp.factgraph import schemas


# ── validate_fact ────────────────────────────────────────────────────────────
def test_fact_valide_normalise_les_defaults():
    out = schemas.validate_fact("entreprise", {"siren": "552032534", "nom": "TRP"})
    assert out == {"siren": "552032534", "nom": "TRP", "bp_an": None, "idcc": None}


def test_fact_champ_requis_manquant_rejete():
    with pytest.raises(schemas.SchemaError):
        schemas.validate_fact("contact", {"tel": "0600000000"})  # nom requis


def test_fact_enum_hors_domaine_rejete():
    with pytest.raises(schemas.SchemaError):
        schemas.validate_fact("action", {"canal": "fax", "outcome": "x"})


def test_fact_type_invalide_rejete():
    with pytest.raises(schemas.SchemaError):
        schemas.validate_fact("entreprise", {"siren": "1", "nom": "X", "bp_an": "beaucoup"})


def test_fact_kind_inconnu_rejete():
    with pytest.raises(schemas.SchemaError):
        schemas.validate_fact("alien", {"x": 1})


def test_fact_compta_meme_registre():
    # canari de généricité : un autre cas d'usage passe par le même registre.
    out = schemas.validate_fact("facture", {"numero": "F-1", "montant_cents": 120000, "tiers": "ACME"})
    assert out["montant_cents"] == 120000


# ── validate_edge ────────────────────────────────────────────────────────────
def test_edge_role_valide():
    schemas.validate_edge("concerns", "contact", "entreprise")  # ne lève pas


def test_edge_source_interdite_rejete():
    with pytest.raises(schemas.SchemaError):
        schemas.validate_edge("concerns", "entreprise", "contact")  # sens inverse


def test_edge_role_inconnu_rejete():
    with pytest.raises(schemas.SchemaError):
        schemas.validate_edge("bidon", "contact", "entreprise")


def test_edge_role_wildcard_accepte_tout():
    # derived-from = endpoints libres (set() vide).
    schemas.validate_edge("derived-from", "action", "facture")
