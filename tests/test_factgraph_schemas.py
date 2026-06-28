"""Tests purs du registre de schémas factgraph (ADR 0008).

`schemas.validate_fact` est la garde « records typés » :
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


# ── lead générique + describe_kinds (« theme data model » exposé à la vue) ────
def test_lead_data_plus_qualif_texte():
    out = schemas.validate_fact("lead", {"raison_sociale": "ACME", "ca": 450000,
                                         "pourquoi_lead": "utilise Swile", "emetteur_actuel": "Swile"})
    assert out["raison_sociale"] == "ACME"
    assert out["statut"] == "nouveau"           # défaut
    assert out["pourquoi_lead"] == "utilise Swile"


def test_describe_kinds_expose_roles_et_domaine():
    by_kind = {k["kind"]: k for k in schemas.describe_kinds()}
    lead = by_kind["lead"]
    assert lead["domain"] == "prospection"
    roles = {f["name"]: f["role"] for f in lead["fields"]}
    assert roles["raison_sociale"] == "title"
    # les champs de qualification sont du TEXTE LIBRE (role=qualif/note)
    assert roles["pourquoi_lead"] == "qualif"
    assert roles["accroche"] == "qualif"
    assert roles["notes"] == "note"
    # chaque kind du registre est décrit + mappé à un domaine
    assert set(by_kind) == set(schemas.REGISTRY)
    assert all(k["domain"] for k in by_kind.values())
