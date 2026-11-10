"""Connecteur Sivan — planning et gestion du personnel, lecture seule.

Les tripwires génériques couvrent déjà le registre, l'éditeur, le logo, la prose
servie et la jointure au client oto-core. Ce fichier verrouille ce qui est PROPRE
à ce module :

- les six outils montés, un par méthode de `SivanClient`, chacun avec sa description ;
- chaque outil appelle la BONNE méthode, avec les bons arguments ;
- la fenêtre ≤ 31 jours est portée par le client (ValueError → refus nommé) ;
- la traduction des erreurs Sivan (401 clé, 403 module/droit) ;
- la sonde de connexion sur `list_companies`.

Ne tournent qu'après installation d'oto-core ≥ v1.176.0 (la classe `sivan` y vit).
"""
import asyncio
from unittest.mock import patch

import pytest

from oto_mcp import providers
from oto_mcp.mcp_errors import McpError


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None: ("sivan_k", False))


def _tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import sivan

    m = FastMCP("t")
    sivan.register(m)
    return {t.name: t for t in asyncio.run(m._list_tools())}


def _fn(name):
    return _tools()[name].fn


@pytest.fixture()
def client():
    with patch("oto.tools.sivan.SivanClient") as cls:
        yield cls.return_value


# --- registre ---------------------------------------------------------------

def test_registry_entry():
    c = providers.REGISTRY["sivan"]
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == {"byo_user", "byo_org"}
    assert c.category == "HR & recruiting"


def test_six_tools_served_with_descriptions():
    tools = _tools()
    assert set(tools) == {"sivan_companies", "sivan_salaries", "sivan_events",
                          "sivan_badged_events", "sivan_absences", "sivan_timesheets",
                          "sivan_timesheets_summary"}
    assert all(t.description for t in tools.values())


def test_probe_lists_companies(client):
    from oto_mcp.tools.sivan import _verify
    _verify({"key": "sivan_k"})
    client.list_companies.assert_called_once_with()


# --- un outil par méthode ----------------------------------------------------

def test_companies(client):
    client.list_companies.return_value = {"companies": []}
    assert _fn("sivan_companies")() == {"companies": []}
    client.list_companies.assert_called_once_with()


def test_salaries_passes_counters(client):
    _fn("sivan_salaries")(with_counter=True, with_absences_counter=True)
    client.list_salaries.assert_called_once_with(
        with_counter=True, with_absences_counter=True)


def test_salaries_defaults(client):
    _fn("sivan_salaries")()
    client.list_salaries.assert_called_once_with(
        with_counter=False, with_absences_counter=False)


def test_events(client):
    _fn("sivan_events")(start="2026-10-01", end="2026-10-15",
                        with_absence_type=True, companies_ids=["c1"],
                        salaries_ids=["s1"])
    client.list_events.assert_called_once_with(
        "2026-10-01", "2026-10-15", with_absence_type=True,
        companies_ids=["c1"], salaries_ids=["s1"])


def test_badged_events(client):
    _fn("sivan_badged_events")(start="2026-10-01", end="2026-10-15")
    client.list_badged_events.assert_called_once_with(
        "2026-10-01", "2026-10-15", companies_ids=None, salaries_ids=None)


def test_absences(client):
    _fn("sivan_absences")(start="2026-01-01", end="2026-12-31", status="PENDING")
    client.list_absences.assert_called_once_with(
        "2026-01-01", "2026-12-31", status="PENDING", salaries_ids=None)


def test_timesheets(client):
    _fn("sivan_timesheets")(start="2026-10-01", end="2026-10-15",
                            salaries_ids=["s1"])
    client.list_timesheets.assert_called_once_with(
        "2026-10-01", "2026-10-15", salaries_ids=["s1"])


def test_timesheets_summary(client):
    _fn("sivan_timesheets_summary")(start="2026-10-01", end="2026-10-15",
                                    salaries_ids=["s1"])
    client.list_timesheets_summary.assert_called_once_with(
        "2026-10-01", "2026-10-15", salaries_ids=["s1"])


# --- erreurs -----------------------------------------------------------------

def _upstream(status, body):
    from oto.tools.common.errors import UpstreamHTTPError
    return UpstreamHTTPError(status, body, service="sivan")


def test_client_value_error_becomes_tool_error(client):
    """La fenêtre ≤ 31 jours est portée par le client : son ValueError se rend en refus."""
    client.list_events.side_effect = ValueError("window must be at most 31 days")
    with pytest.raises(McpError, match="31 days"):
        _fn("sivan_events")(start="2026-01-01", end="2026-03-01")


def test_401_names_the_key(client):
    client.list_companies.side_effect = _upstream(401, {"message": "invalid key"})
    with pytest.raises(McpError, match="X-Api-Key"):
        _fn("sivan_companies")()


def test_403_mentions_the_badging_module(client):
    client.list_badged_events.side_effect = _upstream(403, {"message": "module off"})
    with pytest.raises(McpError, match="badging module"):
        _fn("sivan_badged_events")(start="2026-10-01", end="2026-10-15")
