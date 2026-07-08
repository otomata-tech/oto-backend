"""Datastore v2 (ADR 0046) — moteur de schéma structuré, module PUR.

Couvre : définition (types imbriqués, lifecycle), activation opt-in, validation
de row (required / required_when / types / imbrication) et cycle de vie (états,
transitions, terminaux dérivés). Le schéma de démo = la fiche « lead PV » de la
genèse (GR) : le guard-rail « pas de qualified sans les 4 livrables ».
"""
import pytest

from oto_mcp import datastore_schema as dsv2


LEAD_SCHEMA = {
    "strict": True,
    "key": "fact_id",
    "fields": [
        {"key": "fact_id", "type": "text", "required": True, "role": "title"},
        {"key": "siren", "type": "text"},
        {"key": "mwh", "type": "number"},
        {"key": "occupant", "type": "object",
         "fields": [{"key": "nom", "type": "text", "required": True},
                    {"key": "naf", "type": "text"}]},
        {"key": "contacts", "type": "list",
         "of": {"fields": [{"key": "nom", "type": "text", "required": True},
                           {"key": "email", "type": "text"}]}},
        {"key": "status", "role": "status",
         "lifecycle": {"states": ["nouveau", "en_cours", "qualified", "ecarte"],
                       "transitions": {"nouveau": ["en_cours"],
                                       "en_cours": ["qualified", "ecarte", "nouveau"]}}},
        {"key": "qualification", "type": "text",
         "required_when": {"status": "qualified"}},
        {"key": "cold_email", "type": "text",
         "required_when": {"status": "qualified"}},
    ],
}


# ── définition ────────────────────────────────────────────────────────────────

def test_flat_0016_schema_still_valid():
    assert dsv2.validate_schema_def(
        {"fields": [{"key": "a", "type": "text"}], "key": "a"}) == []


def test_nested_schema_valid():
    assert dsv2.validate_schema_def(LEAD_SCHEMA) == []


def test_def_rejects_unknown_type_and_malformed_composites():
    errs = dsv2.validate_schema_def({"fields": [
        {"key": "x", "type": "wat"},
        {"key": "o", "type": "object"},              # object sans fields
        {"key": "l", "type": "list"},                # list sans of
    ]})
    assert any("type inconnu" in e for e in errs)
    assert any("exige fields" in e for e in errs)
    assert any("exige of" in e for e in errs)


def test_def_rejects_lifecycle_inconsistencies():
    errs = dsv2.validate_schema_def({"fields": [
        {"key": "status", "role": "status",
         "lifecycle": {"states": ["a"], "transitions": {"a": ["b"]},
                       "terminal": ["c"]}}]})
    assert any("cible inconnu 'b'" in e for e in errs)
    assert any("terminal: état inconnu 'c'" in e for e in errs)


def test_def_rejects_lifecycle_on_non_status_field():
    errs = dsv2.validate_schema_def({"fields": [
        {"key": "etat", "lifecycle": {"states": ["a"]}}]})
    assert any('role="status"' in e for e in errs)


# ── activation opt-in ─────────────────────────────────────────────────────────

def test_validation_inactive_by_default():
    assert not dsv2.validation_active({"fields": [{"key": "a", "type": "number"}]})
    assert not dsv2.validation_active(None)


def test_validation_active_via_strict_or_required():
    assert dsv2.validation_active({"strict": True, "fields": []})
    assert dsv2.validation_active(
        {"fields": [{"key": "a", "required": True}]})
    assert dsv2.validation_active(
        {"fields": [{"key": "a", "required_when": {"s": "x"}}]})


# ── validation de row ─────────────────────────────────────────────────────────

def test_soft_schema_validates_nothing():
    schema = {"fields": [{"key": "mwh", "type": "number"}]}  # 0016 : soft
    assert dsv2.validate_row(schema, {"mwh": "pas-un-nombre"}) == []


def test_required_missing():
    errs = dsv2.validate_row(LEAD_SCHEMA, {"status": "nouveau"})
    assert any("fact_id" in e and "requis" in e for e in errs)


def test_guard_rail_required_when_qualified():
    """LE guard-rail GR : qualified sans livrables = refus ; avec = OK."""
    base = {"fact_id": "f1", "status": "qualified"}
    errs = dsv2.validate_row(LEAD_SCHEMA, base, prev_status="en_cours")
    assert any("qualification" in e for e in errs)
    assert any("cold_email" in e for e in errs)
    ok = dsv2.validate_row(
        LEAD_SCHEMA, {**base, "qualification": "site très intéressant…",
                      "cold_email": "Bonjour…"}, prev_status="en_cours")
    assert ok == []


def test_required_when_inert_on_other_status():
    errs = dsv2.validate_row(LEAD_SCHEMA,
                             {"fact_id": "f1", "status": "en_cours"},
                             prev_status="nouveau")
    assert errs == []


def test_type_conformity_scalars():
    errs = dsv2.validate_row(LEAD_SCHEMA, {"fact_id": "f1", "mwh": "abc"})
    assert any("mwh" in e and "number" in e for e in errs)
    # coercible : l'agent écrit "1200" → accepté
    assert dsv2.validate_row(LEAD_SCHEMA, {"fact_id": "f1", "mwh": "1200"}) == []
    assert dsv2.validate_row(LEAD_SCHEMA, {"fact_id": "f1", "mwh": 1200.5}) == []


def test_nested_object_and_list_validated():
    errs = dsv2.validate_row(LEAD_SCHEMA, {
        "fact_id": "f1",
        "occupant": {"naf": "4711F"},                 # nom requis manquant
        "contacts": [{"email": "a@b.fr"}, "oops"],    # [0] nom manquant, [1] pas un objet
    })
    assert any(e.startswith("occupant.nom") for e in errs)
    assert any(e.startswith("contacts[0].nom") for e in errs)
    assert any("contacts[1]" in e and "object" in e for e in errs)


def test_lifecycle_unknown_state_and_forbidden_transition():
    errs = dsv2.validate_row(LEAD_SCHEMA, {"fact_id": "f1", "status": "wat"})
    assert any("état inconnu" in e for e in errs)
    errs = dsv2.validate_row(LEAD_SCHEMA,
                             {"fact_id": "f1", "status": "qualified"},
                             prev_status="nouveau")  # nouveau → qualified interdit
    assert any("transition" in e and "interdite" in e for e in errs)


def test_lifecycle_same_state_write_is_free():
    assert dsv2.validate_row(LEAD_SCHEMA,
                             {"fact_id": "f1", "status": "en_cours"},
                             prev_status="en_cours") == []


def test_terminal_states_derived_and_explicit():
    # dérivés : qualified/ecarte n'ont pas de transition sortante
    assert dsv2.terminal_states(LEAD_SCHEMA) == {"qualified", "ecarte"}
    assert dsv2.is_terminal_status(LEAD_SCHEMA, "qualified")
    assert not dsv2.is_terminal_status(LEAD_SCHEMA, "en_cours")
    explicit = {"fields": [{"key": "s", "role": "status",
                            "lifecycle": {"states": ["a", "b"], "terminal": ["b"]}}]}
    assert dsv2.terminal_states(explicit) == {"b"}
