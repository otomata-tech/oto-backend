"""Connecteur Malibou — paie et RH, lecture seule.

Les tripwires génériques couvrent déjà le registre, l'éditeur, le logo, la prose
servie, la paire lire/agir et la jointure au client oto-core. Ce fichier verrouille
ce qui est PROPRE à ce module :

- un seul outil `malibou` (LECTURE), le verbe de lecture dans `op` ;
- chaque op appelle la BONNE méthode, avec les bons arguments, et le credential à
  deux champs (clé + organisation) arrive au client ;
- aucun argument ignoré en silence, les arguments requis nommés ;
- le type d'une absence servi en `absence_type` ;
- la traduction des erreurs Malibou (401 clé, 403 droit ou organisation) ;
- la sonde de connexion ;
- le PLANCHER de rédaction (NIR, IBAN/BIC, motif d'absence), éprouvé par le chemin
  réel `redaction.redact_payload` sur des sentinelles.

Ne tournent qu'après installation d'oto-core avec `oto.tools.malibou`. Toutes les
valeurs sont factices.
"""
import asyncio
import json
from unittest.mock import patch

import pytest

from oto_mcp import field_filter_defaults, providers, redaction
from oto_mcp.mcp_errors import McpError

CREDS = {"api_key": "mk_test", "organization_id": "org_test"}


@pytest.fixture(autouse=True)
def _fake_creds(monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_credential_fields",
                        lambda provider, account=None: dict(CREDS))


def _tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import malibou

    m = FastMCP("t")
    malibou.register(m)
    return {t.name: t for t in asyncio.run(m._list_tools())}


def _fn():
    return _tools()["malibou"].fn


@pytest.fixture()
def client():
    with patch("oto.tools.malibou.MalibouClient") as cls:
        yield cls


# --- registre ---------------------------------------------------------------

def test_registry_entry():
    c = providers.REGISTRY["malibou"]
    assert c.secret_kind == "fields"
    assert c.auth_modes == {"byo_user", "byo_org"}
    assert c.category == "HR"
    fields = {f.name: f.secret for f in c.credential_fields}
    assert fields == {"api_key": True, "organization_id": False}


def test_one_read_tool_served():
    tools = _tools()
    assert set(tools) == {"malibou"}
    t = tools["malibou"]
    assert t.description
    assert t.annotations.readOnlyHint is True


# --- ops -> méthodes ----------------------------------------------------------

def test_credential_fields_reach_the_client(client):
    _fn()(op="collaborators")
    client.assert_called_once_with(api_key="mk_test", organization_id="org_test")


def test_collaborators(client):
    _fn()(op="collaborators", contract_status="active", establishment_id="est_1",
          page=2, limit=50)
    client.return_value.list_collaborators.assert_called_once_with(
        establishment_id="est_1", contract_status="active", page=2, limit=50)


def test_collaborator(client):
    _fn()(op="collaborator", collaborator_id="col_1")
    client.return_value.get_collaborator.assert_called_once_with(
        "col_1", establishment_id=None)


def test_work_locations(client):
    _fn()(op="work_locations", start_date="2026-10-01", end_date="2026-10-31",
          status="approved")
    client.return_value.list_work_locations.assert_called_once_with(
        "2026-10-01", "2026-10-31", status="approved", establishment_id=None,
        page=None, limit=None)


def test_absences_serve_the_leave_code_as_absence_type(client):
    client.return_value.list_absences.return_value = {
        "data": [{"id": "a1", "collaboratorId": "col_1", "type": "sick_leave",
                  "status": "approved"}],
        "total": 1, "page": 1, "limit": 20}
    out = _fn()(op="absences", start_date="2026-10-01", end_date="2026-10-31")
    client.return_value.list_absences.assert_called_once_with(
        "2026-10-01", "2026-10-31", status=None, establishment_id=None,
        page=None, limit=None)
    assert out["data"] == [{"id": "a1", "collaboratorId": "col_1",
                            "absence_type": "sick_leave", "status": "approved"}]
    assert out["total"] == 1


# --- refus locaux -------------------------------------------------------------

@pytest.mark.parametrize("kwargs, name", [
    ({"op": "collaborator"}, "collaborator_id"),
    ({"op": "absences", "end_date": "2026-10-31"}, "start_date"),
    ({"op": "work_locations", "start_date": "2026-10-01"}, "end_date"),
])
def test_required_argument_is_named(client, kwargs, name):
    with pytest.raises(McpError, match=name):
        _fn()(**kwargs)
    client.assert_not_called()


@pytest.mark.parametrize("kwargs, name", [
    ({"op": "collaborators", "start_date": "2026-10-01"}, "start_date"),
    ({"op": "collaborator", "collaborator_id": "c", "page": 2}, "page"),
    ({"op": "absences", "start_date": "2026-10-01", "end_date": "2026-10-31",
      "contract_status": "active"}, "contract_status"),
])
def test_ignored_argument_is_refused(client, kwargs, name):
    with pytest.raises(McpError, match=name):
        _fn()(**kwargs)
    client.assert_not_called()


def test_client_value_error_becomes_a_named_refusal(client):
    client.return_value.list_absences.side_effect = ValueError(
        "`start_date` must be a date YYYY-MM-DD; got '01/10/2026'.")
    with pytest.raises(McpError, match="YYYY-MM-DD"):
        _fn()(op="absences", start_date="01/10/2026", end_date="2026-10-31")


# --- erreurs amont ------------------------------------------------------------

@pytest.mark.parametrize("status, needle", [
    (401, "invalid or revoked"),
    (403, "scope"),
    (404, "not found"),
    (429, "retry later"),
    (503, "temporarily unavailable"),
])
def test_upstream_errors_are_translated(client, status, needle):
    from oto.tools.common.errors import UpstreamHTTPError
    client.return_value.list_collaborators.side_effect = UpstreamHTTPError(
        status, {"error": "x"}, service="malibou")
    with pytest.raises(McpError, match=needle):
        _fn()(op="collaborators")


# --- sonde --------------------------------------------------------------------

def test_verify_probes_with_both_fields(client):
    from oto_mcp.tools import malibou
    malibou._verify(dict(CREDS))
    client.assert_called_once_with(api_key="mk_test", organization_id="org_test")
    client.return_value.probe.assert_called_once_with()


@pytest.mark.parametrize("status", [401, 403])
def test_verify_auth_refusal_is_non_autorise(client, status):
    from oto.tools.common.errors import UpstreamHTTPError
    from oto_mcp.connectors import verify as connector_verify
    from oto_mcp.tools import malibou
    client.return_value.probe.side_effect = UpstreamHTTPError(
        status, {"error": "Unauthorized"}, service="malibou")
    with pytest.raises(connector_verify.NonAutorise):
        malibou._verify(dict(CREDS))


# --- plancher de rédaction ----------------------------------------------------

NIR = "SENTINELLE-NIR-0000000000000"
IBAN = "SENTINELLE-IBAN-FR0000000000"
BIC = "SENTINELLE-BIC-0000"
MOTIF = "SENTINELLE-MOTIF-MALADIE"
NOM = "SENTINELLE-NOM"


def _payload() -> dict:
    """Les formes réelles du connecteur : une page de collaborateurs (`ssn`), une
    fiche (`socialSecurityNumber`), une page d'absences (`absence_type`)."""
    return {
        "collaborators": {"data": [{
            "id": "c1", "lastName": NOM, "ssn": NIR,
            "bankAccount": {"iban": IBAN, "bic": BIC},
            "team": {"id": "t1", "name": "Équipe"},
        }], "total": 1, "page": 1, "limit": 20},
        "collaborator": {"id": "c1", "lastName": NOM, "socialSecurityNumber": NIR,
                         "bankAccount": {"iban": IBAN, "bic": BIC}},
        "absences": {"data": [{"id": "a1", "collaboratorId": "c1",
                               "absence_type": MOTIF, "status": "approved"}]},
    }


@pytest.fixture
def org_sans_politique(monkeypatch):
    monkeypatch.setattr("oto_mcp.access.current_user_sub_from_token", lambda: "sub-test")
    monkeypatch.setattr("oto_mcp.access.current_org", lambda sub: 1)
    monkeypatch.setattr("oto_mcp.access.rbac.org_store.get_org_field_filters",
                        lambda org_id: {})


def _rendu() -> str:
    out = redaction.redact_payload("malibou", _payload())
    assert out is not redaction.PASSTHROUGH, (
        "aucune politique ne s'est appliquée à `malibou` : le plancher serveur n'est "
        "pas monté, tout sort en clair.")
    return json.dumps(out, ensure_ascii=False)


@pytest.mark.parametrize("sentinelle", [NIR, IBAN, BIC, MOTIF])
def test_the_server_default_masks_the_sentinel(org_sans_politique, sentinelle):
    assert sentinelle not in _rendu()


def test_what_the_customer_comes_for_stays_readable(org_sans_politique):
    assert NOM in _rendu()


def test_the_floor_names_every_masked_field():
    assert set(field_filter_defaults.champs_du_plancher("malibou")) == {
        "ssn", "socialSecurityNumber", "iban", "bic", "absence_type"}


# --- vue par défaut des listes ------------------------------------------------

_ROW = {"id": "c1", "lastName": NOM, "ssn": NIR, "address": {"city": "Ville"},
        "bankAccount": {"iban": IBAN, "bic": BIC}, "birthDate": "1990-01-01",
        "currentContract": {"id": "k1", "natureContrat": "permanent"}}


def test_collaborators_default_view_drops_personal_details(client):
    client.return_value.list_collaborators.return_value = {
        "data": [dict(_ROW)], "total": 1, "page": 1, "limit": 20}
    out = _fn()(op="collaborators")
    assert out["data"] == [{"id": "c1", "lastName": NOM,
                            "currentContract": {"id": "k1", "natureContrat": "permanent"}}]
    assert (out["total"], out["page"], out["limit"]) == (1, 1, 20)


def test_full_returns_raw_rows(client):
    raw = {"data": [dict(_ROW)], "total": 1, "page": 1, "limit": 20}
    client.return_value.list_collaborators.return_value = raw
    assert _fn()(op="collaborators", full=True) == raw


def test_records_default_view_drops_created_at(client):
    client.return_value.list_work_locations.return_value = {
        "data": [{"id": "w1", "isRemoteWork": True, "createdAt": "2026-10-01T08:00:00Z"}]}
    out = _fn()(op="work_locations", start_date="2026-10-01", end_date="2026-10-31")
    assert out["data"] == [{"id": "w1", "isRemoteWork": True}]


def test_full_is_refused_on_a_single_record(client):
    with pytest.raises(McpError, match="full"):
        _fn()(op="collaborator", collaborator_id="c1", full=True)
