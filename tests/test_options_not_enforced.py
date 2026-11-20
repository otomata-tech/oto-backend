"""Des options déclarées que le tableau fait respecter (#319, puis oto#124).

Signalé sur pièce par une mission : `options: ["oui","non","inconnu"]` posées sur un
tableau non-strict acceptaient « Peut-être » sans un mot. Le 05/10/2026 (oto#124) :
**toujours refuser, plus aucun réglage** — la liste s'applique sur TOUS les tableaux qui
déclarent une colonne (`validation_complete`).
"""
from __future__ import annotations

import pytest

from oto_mcp.datastore import schema as dsv2


ENUM = {"key": "priorite", "type": "enum", "options": ["haute", "basse"]}


def test_a_value_outside_the_options_is_refused():
    errs = dsv2.validate_row({"fields": [ENUM]}, {"priorite": "Moyenne"})
    assert errs and "not among the options (haute, basse)" in errs[0]


def test_a_value_inside_the_options_says_nothing():
    assert dsv2.validate_row({"fields": [ENUM]}, {"priorite": "haute"}) == []


def test_an_absent_field_is_not_a_violation():
    assert dsv2.validate_row({"fields": [ENUM]}, {}) == []


def test_a_value_written_in_LAYERS_is_unwrapped_before_being_judged():
    assert dsv2.validate_row({"fields": [ENUM]},
                             {"priorite": {"valeur": "haute", "comment": "urgent"}}) == []


def test_a_LAYERED_value_really_outside_the_list_is_still_refused():
    errs = dsv2.validate_row({"fields": [ENUM]},
                             {"priorite": {"valeur": "Moyenne", "comment": "x"}})
    assert errs and "'Moyenne'" in " ".join(errs), "la valeur, pas la structure"


@pytest.mark.parametrize("vide", ["", {"valeur": ""}, {"comment": "sans valeur"}])
def test_an_EMPTY_cell_is_not_a_value_outside_the_list(vide):
    assert dsv2.validate_row({"fields": [ENUM]}, {"priorite": vide}) == []


def test_an_enum_without_options_condemns_nothing():
    schema = {"fields": [{"key": "p", "type": "enum"}]}
    assert dsv2.validate_row(schema, {"p": "n'importe quoi"}) == []


def test_nothing_to_warn_gives_no_message():
    assert dsv2.json_depth_warning([]) is None


# ── le champ json (② du lot) ─────────────────────────────────────────────────

def test_a_json_field_is_flagged_at_pose_time():
    """Le fait est documenté mais invisible au moment de déclarer : une mission y
    avait mis toute sa traçabilité par champ avant de découvrir qu'elle n'était ni
    filtrable ni agrégeable."""
    champs = dsv2.json_fields_depth({"fields": [{"key": "provenance", "type": "json"},
                                                {"key": "nom", "type": "text"}]})
    assert champs == ["provenance"]

    msg = dsv2.json_depth_warning(champs)
    assert "premier niveau" in msg


def test_the_json_warning_states_the_fact_without_prescribing():
    """⚠️ Contrainte explicite du lot : énoncer le FAIT, sans recommander de
    contournement — la provenance native est en cours de conception, et conseiller une
    structure aujourd'hui reviendrait à prescrire ce qui sera obsolète demain."""
    msg = dsv2.json_depth_warning(["provenance"])

    for prescription in ("plutôt", "préfère", "utilise", "déclare", "remplace",
                         "aplatis", "colonne dédiée"):
        assert prescription not in msg.lower(), f"« {prescription} » prescrit un remède"


def test_json_is_found_in_depth():
    """Un champ `json` niché dans une fiche pose le même problème, moins visiblement."""
    champs = dsv2.json_fields_depth({"fields": [
        {"key": "occupant", "type": "object",
         "fields": [{"key": "meta", "type": "json"}]}]})
    assert champs == ["meta"]


# ── le vrai chemin d'écriture (PostgreSQL réel) ──────────────────────────────


def _store():
    from oto_mcp.datastore.core import make_store
    return make_store("sub-test")


def _table(st, schema):
    import uuid

    from oto_mcp import db
    ns = str(db.create_datastore("user", "sub-test", "t-" + uuid.uuid4().hex[:6]))

    out = st.set_schema(ns, schema)
    return ns, out


def test_the_real_write_path_refuses(live):
    from oto_mcp.datastore.core import RowValidationError
    st = _store()
    ns, _ = _table(st, {"fields": [ENUM]})
    with pytest.raises(RowValidationError):
        st.append_row(ns, {"priorite": "Moyenne"})


def test_posing_a_json_field_warns(live):
    st = _store()
    _, out = _table(st, {"fields": [ENUM, {"key": "prov", "type": "json"}]})
    assert "premier niveau" in (out.get("warning") or "")


def test_a_clean_schema_says_nothing(live):
    st = _store()
    _, out = _table(st, {"fields": [ENUM]})
    assert "premier niveau" not in (out.get("warning") or "")
