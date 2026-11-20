"""oto#103, arbitré le 24/09/2026 : le type `phone` entre, et un `pattern` SEUL s'applique.

La famille des types métier reste limitée aux types dont le rendu et la validation
valent partout (`url`, `email`, `phone`) ; le reste (SIREN, IBAN…) se contraint par un
motif posé par le consommateur. D'où les deux moitiés de ce banc : `phone` se déclare,
se valide et se dit dans les refus ; un motif posé sans `max_length` n'est plus inerte.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp.datastore import schema as S
from oto_mcp.datastore import schema_keys as K
from oto_mcp.datastore import telephone
from oto_mcp.datastore.motifs import PATTERN_MAX_SUBJECT
from oto_mcp.datastore.phrases_de_refus import _forme_attendue, gabarit

_TEL = {"fields": [{"key": "ref", "type": "text"},
                   {"key": "tel", "type": "phone"}]}


# ── `phone` : ce qui passe, ce qui ne passe pas ──────────────────────────────

@pytest.mark.parametrize("brut,compact", [
    ("+33612345678", "+33612345678"),                 # E.164 strict
    ("+33 6 12 34 56 78", "+33612345678"),
    ("+33 (0)6 12 34 56 78", "+33612345678"),         # le (0) national retiré
    ("0033 6-12-34-56-78", "+33612345678"),           # 00 lu comme +
    ("+1 (415) 555-0132", "+14155550132"),
    ("06.12.34.56.78", "0612345678"),                 # national, sans devinette
    ("01 23 45 67 89", "0123456789"),
])
def test_un_numero_lisible_passe_et_se_normalise(brut, compact):
    assert telephone.normaliser(brut) == compact
    assert S.validate_row(_TEL, {"ref": "r", "tel": brut}, written={"tel"}) == []


@pytest.mark.parametrize("brut", [
    "non trouvé", "voir site", "+0612345678",          # indicatif en 0 : pas E.164
    "+33 6 12 34 56 78 90 12 34", "12345",             # trop long, trop court
    "contact@acme.fr", "552 100 554 00017 x",
    612345678,                                         # un nombre perd son 0
])
def test_ce_qui_n_est_pas_un_numero_est_refuse_meme_sans_strict(brut):
    """Armé par sa déclaration, comme `email` (`types_declares`). Le refus dit où va ce
    qui n'est pas le numéro."""
    errs = S.validate_row(_TEL, {"ref": "r", "tel": brut}, written={"tel"})
    refus = [e for e in errs if "`tel` is declared `phone`" in e]
    assert refus, errs
    assert ".comment" in refus[0]


def test_le_type_se_declare_et_se_dit():
    assert S.validate_schema_def(_TEL) == []
    assert S.validate_schema_def({"fields": [
        {"key": "contacts", "type": "list",
         "of": {"type": "object", "fields": [{"key": "tel", "type": "phone"}]}}]}) == []
    champ = _TEL["fields"][1]
    assert "E.164" in _forme_attendue(champ)
    assert gabarit(champ) == "<phone, +country code preferred>"
    assert "phone" in next(c.quoi for c in K.CLES if c.nom == "type")


def test_la_valeur_n_est_pas_reecrite_par_la_validation():
    row = {"ref": "r", "tel": "+33 6 12 34 56 78"}
    S.validate_row(_TEL, row, written={"tel"})
    assert row["tel"] == "+33 6 12 34 56 78"


# ── un motif SEUL s'applique ──────────────────────────────────────────────────

_SIREN = {"fields": [{"key": "ref", "type": "text"},
                     {"key": "siren", "type": "text", "pattern": r"^[0-9]{9}$"}]}


def test_un_motif_seul_se_pose_arme_la_validation_et_mord():
    """Avant : refusé à la pose, et inerte dans un schéma plus ancien."""
    assert S.validate_schema_def(_SIREN) == []
    assert S.validation_active(_SIREN)
    assert S.validate_row(_SIREN, {"siren": "552100554"}, written={"siren"}) == []
    (err,) = S.validate_row(_SIREN, {"siren": "55210055"}, written={"siren"})
    assert "does not match the pattern" in err


def test_un_motif_seul_ne_juge_que_ce_que_le_geste_ecrit():
    merged = {"ref": "r", "siren": "historique"}
    assert S.validate_row(_SIREN, merged, written={"ref"}) == []


def test_au_dela_de_la_borne_par_defaut_la_valeur_est_refusee_sans_executer_le_motif(
        monkeypatch):
    from oto_mcp.datastore import validation as V

    def _jamais(src):
        raise AssertionError("le motif ne doit pas s'exécuter au-delà de sa borne")
    monkeypatch.setattr(V, "_pattern_re", _jamais)
    long = "5" * (PATTERN_MAX_SUBJECT + 1)
    (err,) = S.validate_row(_SIREN, {"siren": long}, written={"siren"})
    assert f"{PATTERN_MAX_SUBJECT} characters at most" in err


def test_une_borne_declaree_reste_la_borne_du_motif():
    schema = {"fields": [{"key": "c", "type": "text", "max_length": 12,
                          "pattern": r"^[a-z]+$"}]}
    (err,) = S.validate_row(schema, {"c": "a" * 13}, written={"c"})
    assert "maximum 12" in err                       # la borne parle, une fois


# ── bout en bout : les deux faces ─────────────────────────────────────────────

SUB = "sub-oto103"


def _table(schema):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns_id = db.create_datastore("user", SUB, "p103-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    make_store(SUB).set_schema(ns, schema)
    return ns, ns_id


def test_face_REST_phone_et_motif_seul(live, monkeypatch):
    from _datastore_rest import call, stub_authz

    stub_authz(monkeypatch, org_id=None)
    ns, _ = _table({"fields": _TEL["fields"] + _SIREN["fields"][1:]})
    code, corps = call("me.datastore.append_row", path_params={"datastore": ns},
                       body={"ref": "a", "tel": "+33 6 12 34 56 78",
                             "siren": "552100554"}, sub=SUB)
    assert code == 201, corps
    code, corps = call("me.datastore.append_row", path_params={"datastore": ns},
                       body={"ref": "b", "tel": "non trouvé"}, sub=SUB)
    assert code == 400 and "phone" in corps["detail"], corps
    code, corps = call("me.datastore.append_row", path_params={"datastore": ns},
                       body={"ref": "c", "siren": "abc"}, sub=SUB)
    assert code == 400 and "pattern" in corps["detail"], corps


def test_face_OUTIL_phone_et_motif_seul(live, monkeypatch):
    import asyncio

    from fastmcp import FastMCP

    from oto_mcp import call_axes
    from oto_mcp.datastore.core import make_store
    from oto_mcp.mcp_errors import McpError
    from oto_mcp.tools import datastore as T
    from oto_mcp.tools import register_all

    monkeypatch.setattr(T, "_acting_store", lambda: make_store(SUB))
    monkeypatch.setattr(T, "_ns", lambda ns: ns)
    monkeypatch.setattr(T, "_project_hint", lambda ns: None)
    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: SUB)
    m = FastMCP("t-103")
    register_all(m)
    outil = asyncio.run(m.get_tool("data_write"))

    def appeler(**kw):
        r = outil.fn(**kw)
        return asyncio.run(r) if asyncio.iscoroutine(r) else r

    ns, _ = _table({"fields": _TEL["fields"] + _SIREN["fields"][1:]})
    appeler(datastore=ns, rows=[{"ref": "a", "tel": "0033 6 12 34 56 78",
                                 "siren": "552100554"}])
    with pytest.raises(McpError) as e:
        appeler(datastore=ns, rows=[{"ref": "b", "tel": "voir site"}])
    assert "phone" in e.value.error.message
    with pytest.raises(McpError) as e:
        appeler(datastore=ns, rows=[{"ref": "c", "siren": "abc"}])
    assert "pattern" in e.value.error.message
