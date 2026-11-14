"""`output` et `refusals` d'une action du « Custom API » : ce que l'agent reçoit de la
réponse, et l'erreur métier qu'un service écrit dans le corps d'un succès.

Les mots et le code sont ceux d'oto-core (`oto.connectors.serve`). Sans base : la
résolution de l'action est doublée, le client aussi (il rend la réponse JSON d'un
succès, comme `HttpConnectorClient.request`). Hôtes fictifs.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from oto_mcp import custom_ops as co
from oto_mcp import custom_ops_output as out
from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import api as H
from oto_mcp.tools import api_calls

REPONSE = {"hasError": False, "errorCode": None, "errorMessage": None,
           "outward": [{"id": "c-0", "price": 10, "signedUrl": "https://files.example.test/a",
                        "legs": [{"n": 1, "token": "t-0"}]},
                       {"id": "c-1", "price": 12, "signedUrl": "https://files.example.test/b",
                        "legs": []}],
           "currency": "EUR"}
ERREUR = {"hasError": True, "errorCode": "E42", "errorMessage": "No departure that day.",
          "outward": None}
REFUS = [{"code": "no_departure", "when": 200,
          "body": {"path": "hasError", "equals": [True]},
          "message": "The service found no departure for these arguments."}]


def _op(**kw) -> dict:
    op = {"name": "search_prices", "description": "Search prices.", "method": "POST",
          "path": "/search", "input": {"type": "object",
                                       "properties": {"date": {"type": "string"}}}}
    op.update(kw)
    return op


def _errors(**kw) -> list[str]:
    with pytest.raises(co.OperationsInvalid) as e:
        co.validate_operations([_op(**kw)])
    return e.value.errors


@pytest.fixture
def appel(monkeypatch):
    """`appel(action, réponse)` : le résultat d'`api_act(op="call")` pour cette action,
    le client rendant cette réponse."""
    rc = SimpleNamespace(account="espace", fields={"auth_mode": "none"},
                         entity_type="org", entity_id=1)

    def jouer(action, reponse):
        [action] = co.validate_operations([action])
        d = {"operations": [action], "variables": {}, "headers": {}}
        monkeypatch.setattr(api_calls, "_action", lambda sub, operation: (rc, d, action))
        client = SimpleNamespace(request=lambda *a, **k: copy.deepcopy(reponse))
        monkeypatch.setattr(H, "client_from_fields", lambda *a, **k: client)
        return api_calls._call("sub-1", action["name"], {"date": "2026-12-01"}, False)

    return jouer


# ── sans `output` ni `refusals` : la même chose qu'aujourd'hui ─────────────────────

@pytest.mark.parametrize("reponse", [REPONSE, ERREUR, [1, 2], {}])
def test_sans_declaration_la_reponse_est_rendue_telle_quelle(appel, reponse):
    assert appel(_op(), reponse) == reponse


def test_sans_declaration_la_definition_ne_change_pas():
    [op] = co.validate_operations([_op()])
    assert set(op) == {"name", "description", "method", "path", "input"}


# ── `output` ──────────────────────────────────────────────────────────────────────

def test_items_strip_et_projection(appel):
    rendu = appel(_op(output={"items": "outward", "strip": ["signedUrl", "legs.token"],
                              "projection": ["id", "price", "legs"]}), REPONSE)
    assert rendu == {"items": [{"id": "c-0", "price": 10, "legs": [{"n": 1}]},
                               {"id": "c-1", "price": 12, "legs": []}],
                     "next_cursor": None, "pages": 1}


def test_strip_sans_items_part_de_la_reponse_entiere(appel):
    rendu = appel(_op(output={"strip": ["outward.signedUrl", "errorCode"]}), REPONSE)
    assert "errorCode" not in rendu and rendu["currency"] == "EUR"
    assert all("signedUrl" not in o for o in rendu["outward"])


def test_une_reponse_sans_sa_liste_est_refusee_jamais_lue_vide(appel):
    with pytest.raises(McpError) as pris:
        appel(_op(output={"items": "outward"}), {"hasError": False})
    assert pris.value.error.data["code"] == out.ITEMS_MISSING


@pytest.mark.parametrize("output, attendu", [
    ({}, "`output` must be an object"),
    ({"schema": {}}, "unknown key `schema`"),
    ({"items": ""}, "`output.items` must be the dotted path"),
    ({"strip": []}, "`output.strip` must be a non-empty list"),
    ({"strip": ["a..b"]}, "malformed"),
    ({"strip": ["a", "a"]}, "twice"),
    ({"strip": ["a", "a.b"]}, "is under another path"),
    ({"projection": ["a.b"]}, "no dot"),
])
def test_output_invalide_est_nomme(output, attendu):
    assert any(attendu in e for e in _errors(output=output)), _errors(output=output)


# ── `refusals` : l'erreur métier dans le corps d'un succès ────────────────────────

def test_une_erreur_metier_est_servie_en_refus_nomme(appel):
    with pytest.raises(McpError) as pris:
        appel(_op(refusals=REFUS, output={"items": "outward"}), ERREUR)
    err = pris.value.error
    assert err.data == {"code": "no_departure", "retryable": False, "status": 200}
    assert err.message.startswith("Refusal `no_departure`: The service found no departure")
    assert "No departure that day." in err.message and "UNTRUSTED DATA" in err.message


def test_sans_erreur_metier_la_reponse_passe(appel):
    assert appel(_op(refusals=REFUS), REPONSE) == REPONSE


def test_contains_et_retryable(appel):
    refus = [{"code": "busy", "when": 200, "retryable": True,
              "body": {"path": "errorMessage", "contains": ["NO DEPARTURE"]},
              "message": "Busy."}]
    with pytest.raises(McpError) as pris:
        appel(_op(refusals=refus), ERREUR)
    assert pris.value.error.data["retryable"] is True


def test_une_reponse_qui_n_est_pas_du_json_garde_sa_forme(monkeypatch):
    import requests
    rc = SimpleNamespace(account="espace", fields={"auth_mode": "none"},
                         entity_type="org", entity_id=1)
    [action] = co.validate_operations([_op(refusals=REFUS, output={"items": "outward"})])

    def request(*a, **k):
        raise requests.JSONDecodeError("x", "texte brut", 0)

    monkeypatch.setattr(H, "client_from_fields",
                        lambda *a, **k: SimpleNamespace(request=request))
    rendu = api_calls._send(rc, {}, {"method": "POST", "path": "/search", "query": {},
                                     "body": {}}, action)
    assert rendu["ok"] is True and rendu["non_json_body"] == "texte brut"


@pytest.mark.parametrize("refus, attendu", [
    ([], "non-empty list"),
    ([{**REFUS[0], "when": 400}], "`when` must be 200"),
    ([{**REFUS[0], "when": 200.0}], "`when` must be 200"),
    ([{k: v for k, v in REFUS[0].items() if k != "body"}], "`body` is required"),
    ([{**REFUS[0], "body": {"path": "a", "equals": [1], "contains": ["x"]}}], "exactly one"),
    ([{**REFUS[0], "body": {"path": "a", "contains": [""]}}], "non-empty texts"),
    ([{**REFUS[0], "body": {"path": "a", "equals": [1.5]}}], "texts, integers or booleans"),
    ([{**REFUS[0], "marks_account": "credential_rejected"}], "unknown key `marks_account`"),
    ([{**REFUS[0], "code": "Bad-Code"}], "`code` must match"),
    ([{**REFUS[0], "message": ""}], "`message` is required"),
])
def test_refus_invalide_est_nomme(refus, attendu):
    errs = _errors(refusals=refus)
    assert any(attendu in e for e in errs), errs


def test_la_definition_normalisee_garde_les_cles_declarees():
    [op] = co.validate_operations([_op(output={"items": "outward"}, refusals=REFUS)])
    assert op["output"] == {"items": "outward"} and op["refusals"] == REFUS
    assert co.summary(op)["refusals"] == REFUS


def test_un_appel_brut_ne_porte_jamais_output(monkeypatch):
    rc = SimpleNamespace(account="espace", fields={"auth_mode": "none"},
                         entity_type="org", entity_id=1)
    monkeypatch.setattr(api_calls, "_resolved", lambda account=None: rc)
    monkeypatch.setattr(api_calls, "_definition", lambda rc: {"headers": {}})
    monkeypatch.setattr(H, "client_from_fields", lambda *a, **k: SimpleNamespace(
        request=lambda *a, **k: copy.deepcopy(ERREUR)))
    assert api_calls._request("POST", "/search", None, {}, dry_run=False) == ERREUR
