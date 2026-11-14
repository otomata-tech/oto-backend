"""Opérations décrites d'un « Custom API » (`custom_ops`) : le format, et le plan d'appel.

Pur, sans base : la validation refuse en NOMMANT chaque problème, et le plan d'appel
route chaque argument (path / query / body) — en refusant qu'un argument de chemin
sorte de son segment (`..`, `/`, encodé ou non), seule place où une valeur d'agent
atteint le CHEMIN de l'URL.
"""
from __future__ import annotations

import pytest

from oto_mcp import custom_ops as co


def _op(**kw) -> dict:
    op = {"name": "list_collaborators", "description": "List the collaborators.",
          "method": "GET", "path": "/organizations/{organizationId}/collaborators",
          "input": {"type": "object",
                    "properties": {"organizationId": {"type": "string"},
                                   "page": {"type": "integer"},
                                   "limit": {"type": "integer"}},
                    "required": ["organizationId"]}}
    op.update(kw)
    return op


def _errors(ops) -> list[str]:
    with pytest.raises(co.OperationsInvalid) as e:
        co.validate_operations(ops)
    return e.value.errors


# ── le format ─────────────────────────────────────────────────────────────────

def test_une_operation_valide_est_normalisee():
    [op] = co.validate_operations([_op(method="get")])
    assert op["method"] == "GET" and "in" not in op


def test_la_liste_vide_est_valide_elle_efface():
    assert co.validate_operations([]) == []


@pytest.mark.parametrize("champ, valeur, attendu", [
    ("name", "ListCollaborators", "`name`"),
    ("name", "1abc", "`name`"),
    ("description", "  ", "`description` is required"),
    ("description", "x" * 1001, "1000 max"),
    ("method", "HEAD", "`method`"),
    ("path", "organizations", "starting with `/`"),
    ("path", "//evil.test/x", "no scheme, no host"),
    ("path", "/a/../admin", "`..`"),
    ("path", "/a/%2e%2e/admin", "`..`"),
    ("path", "/x?api_key=1", "no `?`"),
])
def test_un_champ_invalide_est_nomme(champ, valeur, attendu):
    errs = _errors([_op(**{champ: valeur})])
    assert any(attendu in e for e in errs), errs


def test_cle_inconnue_refusee():
    assert any("unknown key `headers`" in e for e in _errors([_op(headers={"a": "b"})]))


# ── `class: read` : une action qui ne fait que lire, même en POST ──────────────────

@pytest.mark.parametrize("methode", ["GET", "post"])
def test_class_read_est_garde_et_resume(methode):
    [op] = co.validate_operations([_op(method=methode, path="/search", input=None,
                                       **{"class": "read"})])
    assert op["class"] == "read" and co.reads(op)
    assert co.summary(op)["class"] == "read"


def test_sans_class_rien_n_est_ajoute():
    [op] = co.validate_operations([_op()])
    assert "class" not in op and not co.reads(op)
    assert "class" not in co.summary(op)


@pytest.mark.parametrize("classe", ["write", "sensitive", "READ", "", None, True])
def test_class_n_admet_que_read(classe):
    [err] = _errors([_op(**{"class": classe})])
    assert "`class` accepts `read` only" in err


@pytest.mark.parametrize("methode", ["PUT", "PATCH", "DELETE"])
def test_class_read_sur_un_verbe_d_ecriture_refuse(methode):
    [err] = _errors([_op(method=methode, path="/x", input=None, **{"class": "read"})])
    assert f"GET or a POST only, not {methode}" in err


def test_parametre_de_chemin_doit_etre_un_argument_requis():
    op = _op()
    op["input"]["required"] = []
    assert any("must be listed in `input.required`" in e for e in _errors([op]))
    op = _op(path="/organizations/{orgId}/collaborators")
    assert any("`{orgId}` in `path` is not an argument" in e for e in _errors([op]))


def test_in_incoherent_refuse():
    assert any("`in.page` is `path`" in e for e in _errors([_op(**{"in": {"page": "path"}})]))
    assert any("its `in` must be `path`" in e
               for e in _errors([_op(**{"in": {"organizationId": "query"}})]))
    assert any("not an argument" in e for e in _errors([_op(**{"in": {"nope": "query"}})]))


def test_un_get_ne_porte_pas_de_corps():
    assert any("GET carries no body" in e
               for e in _errors([_op(**{"in": {"page": "body"}})]))


def test_noms_uniques_et_plafond():
    assert any("duplicate name" in e for e in _errors([_op(), _op()]))
    many = [_op(name=f"op_{i}") for i in range(co.MAX_OPERATIONS + 1)]
    assert any("100 max" in e for e in _errors(many))


def test_tous_les_problemes_sont_nommes_d_un_coup():
    errs = _errors([_op(name="Bad"), _op(name="ok", method="TRACE")])
    assert len(errs) >= 2


# ── le routage des arguments ──────────────────────────────────────────────────

def test_get_path_query_et_encodage():
    op = co.validate_operations([_op()])[0]
    plan = co.plan_call(op, {"organizationId": "org 1", "page": 2, "limit": None})
    assert plan == {"method": "GET", "path": "/organizations/org%201/collaborators",
                    "query": {"page": 2}, "body": None}


def test_post_route_le_reste_au_corps_et_respecte_in():
    op = co.validate_operations([{
        "name": "create_absence", "description": "Create an absence.", "method": "POST",
        "path": "/organizations/{org}/absences",
        "input": {"type": "object", "properties": {
            "org": {"type": "string"}, "start": {"type": "string"},
            "dry": {"type": "boolean"}}, "required": ["org", "start"]},
        "in": {"dry": "query"}}])[0]
    plan = co.plan_call(op, {"org": "o1", "start": "2026-10-01", "dry": True})
    assert plan["path"] == "/organizations/o1/absences"
    assert plan["query"] == {"dry": True} and plan["body"] == {"start": "2026-10-01"}


@pytest.mark.parametrize("valeur", ["..", ".", "a/b", "%2e%2e", "%2F", "..%2Fadmin", "", "a\\b"])
def test_un_argument_de_chemin_ne_sort_pas_de_son_segment(valeur):
    op = co.validate_operations([_op()])[0]
    with pytest.raises(co.CallRefused, match="single segment"):
        co.plan_call(op, {"organizationId": valeur})


def test_un_pourcent_legitime_est_encode_pas_refuse():
    op = co.validate_operations([_op()])[0]
    assert co.plan_call(op, {"organizationId": "50%"})["path"] == \
        "/organizations/50%25/collaborators"


@pytest.mark.parametrize("args, attendu", [
    ({}, "requires organizationId"),
    ({"organizationId": "o", "nope": 1}, "unknown argument"),
    ({"organizationId": "o", "page": "2"}, "type integer"),
    ({"organizationId": "o", "page": True}, "type integer"),
    ({"organizationId": ["o"]}, "type string"),
])
def test_les_arguments_sont_verifies(args, attendu):
    op = co.validate_operations([_op()])[0]
    with pytest.raises(co.CallRefused, match=attendu):
        co.plan_call(op, args)


def test_un_objet_ne_va_pas_dans_la_query():
    op = co.validate_operations([_op(input={
        "type": "object", "properties": {"organizationId": {"type": "string"},
                                         "filter": {}}, "required": ["organizationId"]})])[0]
    with pytest.raises(co.CallRefused, match="query string"):
        co.plan_call(op, {"organizationId": "o", "filter": {"a": 1}})
