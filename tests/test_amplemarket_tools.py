"""Connecteur Amplemarket — prospection B2B, servi en PAIRE.

Les tripwires génériques couvrent déjà le registre, l'éditeur, le logo, la prose
servie, la convention `<ns>`/`<ns>_act` et la jointure au client oto-core. Ce
fichier verrouille ce qui est PROPRE à ce module :

- deux outils, `amplemarket` (lecture) et `amplemarket_act` (acte), avec leurs ops ;
- chaque op appelle la BONNE méthode du client, avec les bons arguments ;
- la vue par défaut allège les lignes du chercheur, `full=True` rend le brut ;
- le curseur de page est remonté en `next_page_after` ;
- la traduction des erreurs Amplemarket (401, 402 crédits, 451) ;
- la sonde de connexion sur `get_account_info`.

Ne tournent qu'après installation d'un oto-core qui porte `oto.tools.amplemarket`.
"""
import asyncio
from unittest.mock import patch

import pytest

from oto_mcp import providers
from oto_mcp.mcp_errors import McpError


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None: ("am_k", False))


def _tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import amplemarket

    m = FastMCP("t")
    amplemarket.register(m)
    return {t.name: t for t in asyncio.run(m._list_tools())}


def _fn(name):
    return _tools()[name].fn


@pytest.fixture()
def client():
    with patch("oto.tools.amplemarket.AmplemarketClient") as cls:
        yield cls.return_value


# --- registre et surface -------------------------------------------------------

def test_registry_entry():
    c = providers.REGISTRY["amplemarket"]
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == {"byo_user", "byo_org"}
    assert c.category == "Prospecting"


def test_the_pair_is_served_with_descriptions_and_annotations():
    tools = _tools()
    assert set(tools) == {"amplemarket", "amplemarket_act"}
    assert all(t.description for t in tools.values())
    assert tools["amplemarket"].annotations.readOnlyHint is True
    assert tools["amplemarket_act"].annotations.readOnlyHint is False


# --- lecture ---------------------------------------------------------------------

def test_find_person_passes_the_reveal_flags(client):
    client.find_person.return_value = {"id": "p1"}
    out = _fn("amplemarket")(op="find_person", linkedin_url="https://l/in/x",
                             reveal_email=True)
    assert out == {"id": "p1"}
    client.find_person.assert_called_once_with(
        email=None, linkedin_url="https://l/in/x", name=None, title=None,
        company_name=None, company_domain=None, reveal_email=True,
        reveal_phone_numbers=None)


def test_search_people_spreads_filters_and_slims_rows(client):
    client.search_people.return_value = {
        "results": [{"id": "p1", "name": "Jane", "about": "long prose",
                     "experiences": [{"title": "x"}],
                     "company": {"id": "c1", "name": "Acme", "technologies": ["a"] * 50}}],
        "_pagination": {"page": 1, "total": 1}}
    out = _fn("amplemarket")(op="search_people", filters={"person_titles": ["CEO"]},
                             page_size=10)
    client.search_people.assert_called_once_with(page=None, page_size=10,
                                                 person_titles=["CEO"])
    row = out["results"][0]
    assert "about" not in row and "experiences" not in row
    assert row["company"] == {"id": "c1", "name": "Acme"}
    assert out["_pagination"] == {"page": 1, "total": 1}


def test_full_returns_the_raw_rows(client):
    raw = {"results": [{"id": "p1", "about": "long prose"}]}
    client.search_people.return_value = raw
    assert _fn("amplemarket")(op="search_people", full=True) == raw


def test_cursor_lists_surface_next_page_after(client):
    client.list_users.return_value = {
        "users": [{"id": "u1"}],
        "_links": {"next": {"href": "/users?page[size]=1&page[after]=u1"}}}
    out = _fn("amplemarket")(op="users", page_size=1)
    assert out == {"users": [{"id": "u1"}], "next_page_after": "u1"}
    client.list_users.assert_called_once_with(status=None, email=None, page_size=1,
                                              page_after=None)


def test_contact_by_id_or_email(client):
    _fn("amplemarket")(op="contact", id="ct_1")
    client.get_contact.assert_called_once_with("ct_1")
    _fn("amplemarket")(op="contact", email="a@example.com")
    client.get_contact_by_email.assert_called_once_with("a@example.com")


def test_tasks_need_a_user(client):
    with pytest.raises(McpError, match="user_id"):
        _fn("amplemarket")(op="tasks")
    client.list_tasks.assert_not_called()


# --- acte ------------------------------------------------------------------------

def test_add_sequence_leads(client):
    client.add_leads_to_sequence.return_value = {"total": 1, "total_added_to_sequence": 1}
    leads = [{"email": "a@example.com", "data": {"first_name": "A"}}]
    out = _fn("amplemarket_act")(op="add_sequence_leads", id="seq_1", leads=leads)
    assert out["total_added_to_sequence"] == 1
    client.add_leads_to_sequence.assert_called_once_with("seq_1", leads, settings=None)


def test_add_sequence_leads_without_leads_is_refused_before_any_call(client):
    with pytest.raises(McpError, match="leads"):
        _fn("amplemarket_act")(op="add_sequence_leads", id="seq_1")
    client.add_leads_to_sequence.assert_not_called()


def test_batch_enrichment_start_and_tasks(client):
    _fn("amplemarket_act")(op="create_person_enrichment",
                           leads=[{"email": "a@example.com"}], reveal_email=True)
    client.start_person_enrichment.assert_called_once_with(
        [{"email": "a@example.com"}], reveal_email=True, reveal_phone_numbers=None)
    _fn("amplemarket_act")(op="set_task_completed", id="tsk_1")
    client.complete_task.assert_called_once_with("tsk_1", set_lead_to_completed=None)


def test_contact_dict_with_an_unknown_key_is_named(client):
    client.create_contact.side_effect = TypeError("unexpected keyword argument 'nick'")
    with pytest.raises(McpError, match="nick"):
        _fn("amplemarket_act")(op="create_contact", contact={"email": "a@b.c", "nick": "x"})


# --- erreurs et sonde ---------------------------------------------------------------

@pytest.mark.parametrize("status,needle", [
    (401, "Settings > API"), (402, "insufficient_credits"), (451, "privacy")])
def test_upstream_errors_are_translated(client, status, needle):
    from oto.tools.common.errors import UpstreamHTTPError
    client.find_company.side_effect = UpstreamHTTPError(status, {"_errors": []},
                                                        service="amplemarket")
    with pytest.raises(McpError, match=needle.replace(">", ".")):
        _fn("amplemarket")(op="find_company", domain="example.com")


def test_client_value_error_becomes_a_refusal(client):
    client.find_company.side_effect = ValueError("find_company needs domain or linkedin_url")
    with pytest.raises(McpError, match="needs domain"):
        _fn("amplemarket")(op="find_company")


def test_verify_probe_reads_the_workspace():
    from oto_mcp.tools import amplemarket
    with patch("oto.tools.amplemarket.AmplemarketClient") as cls:
        amplemarket._verify({"key": "am_k"})
    cls.assert_called_once_with(api_key="am_k")
    cls.return_value.get_account_info.assert_called_once_with()
