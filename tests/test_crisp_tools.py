"""Connecteur Crisp — messagerie client, lecture seule.

Les tripwires génériques couvrent déjà le registre, l'éditeur, le logo, la prose
servie, la paire lire/agir et la jointure au client oto-core. Ce fichier verrouille
ce qui est PROPRE à ce module :

- un seul outil `crisp` (LECTURE), le verbe de lecture dans `op`, une op par fonction
  de la description `crisp` ;
- le texte servi dit que le contenu peut porter des données personnelles ou de santé ;
- chaque op appelle la BONNE méthode, avec les bons arguments, et le credential à
  trois champs (identifiant, clé, website ID) arrive au client ;
- aucun argument ignoré en silence, les arguments requis nommés, rien n'est appelé
  avant ;
- la traduction des refus de Crisp ;
- la sonde de connexion.

Ne tournent qu'après installation d'oto-core avec `oto.tools.crisp`. Toutes les
valeurs sont factices.
"""
import asyncio
from unittest.mock import patch

import pytest

from oto_mcp import providers
from oto_mcp.mcp_errors import McpError

CREDS = {"token_identifier": "tid_test", "token_key": "tkey_test", "website_id": "site-test"}
PRIVACY = ("May contain personal or health data: read only what the task needs, and do not "
           "look a person up by name or phone number unless the task requires it.")


@pytest.fixture(autouse=True)
def _fake_creds(monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_credential_fields",
                        lambda provider, account=None: dict(CREDS))


def _tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import crisp

    m = FastMCP("t")
    crisp.register(m)
    return {t.name: t for t in asyncio.run(m._list_tools())}


def _fn():
    return _tools()["crisp"].fn


@pytest.fixture()
def client():
    with patch("oto.tools.crisp.CrispClient") as cls:
        yield cls


# --- registre et outil servi ---------------------------------------------------

def test_registry_entry():
    c = providers.REGISTRY["crisp"]
    assert c.secret_kind == "fields"
    assert c.auth_modes == {"byo_user", "byo_org"}
    assert c.category == "Communication"
    fields = {f.name: f.secret for f in c.credential_fields}
    assert fields == {"token_identifier": False, "token_key": True, "website_id": False}


def test_one_read_tool_served_with_the_privacy_notice():
    tools = _tools()
    assert set(tools) == {"crisp"}
    t = tools["crisp"]
    assert t.annotations.readOnlyHint is True
    assert PRIVACY in " ".join(t.description.split())


def test_one_op_per_function_of_the_description():
    from oto_mcp.tools import crisp
    assert len(crisp._OPS) == 20
    assert set(crisp._REQUIRES) <= set(crisp._ACCEPTS)
    assert all(set(req) <= crisp._ACCEPTS[op] for op, req in crisp._REQUIRES.items())


# --- ops -> méthodes --------------------------------------------------------------

def test_credential_fields_reach_the_client(client):
    _fn()(op="website")
    client.assert_called_once_with(**CREDS)
    client.return_value.get_website.assert_called_once_with()


def test_conversations_default_and_incremental_reading(client):
    _fn()()
    _fn()(op="conversations", page=2, per_page=50, date_start="2026-03-01T00:00:00Z",
          order="updated", inbox_id="inbox_1", resolved=False, unread=True)
    first, second = client.return_value.list_conversations.call_args_list
    empty = dict(per_page=None, include_empty=None, filter_inbox_id=None, filter_unread=None,
                 filter_assigned=None, filter_unassigned=None, filter_date_start=None,
                 filter_date_end=None, order_date_created=None, order_date_updated=None,
                 order_date_waiting=None, filter_resolved=None, filter_not_resolved=None)
    assert first.args == (1,) and first.kwargs == empty
    assert second.args == (2,) and second.kwargs == {
        **empty, "per_page": 50, "filter_date_start": "2026-03-01T00:00:00Z",
        "order_date_updated": 1, "filter_inbox_id": "inbox_1", "filter_not_resolved": 1,
        "filter_unread": 1}


def test_a_false_narrowing_flag_sends_no_filter(client):
    _fn()(op="conversations", unread=False, unassigned=False, include_empty=False)
    kwargs = client.return_value.list_conversations.call_args.kwargs
    assert kwargs["filter_unread"] is None and kwargs["filter_unassigned"] is None
    assert kwargs["include_empty"] is None


def test_search_conversations(client):
    _fn()(op="search_conversations", query="vip", search_type="segment", resolved=True,
          order="waiting")
    call = client.return_value.search_conversations.call_args
    assert call.args == (1, "vip")
    assert call.kwargs["search_type"] == "segment"
    assert call.kwargs["filter_resolved"] == 1 and call.kwargs["filter_not_resolved"] is None
    assert call.kwargs["order_date_waiting"] == 1


@pytest.mark.parametrize("op, method", [
    ("conversation", "get_conversation"), ("conversation_meta", "get_conversation_meta")])
def test_one_conversation(client, op, method):
    _fn()(op=op, session_id="session_1")
    getattr(client.return_value, method).assert_called_once_with("session_1")


def test_messages(client):
    _fn()(op="messages", session_id="session_1", timestamp_before=1772380800000)
    client.return_value.list_conversation_messages.assert_called_once_with(
        "session_1", timestamp_before=1772380800000, timestamp_after=None,
        timestamp_around=None)


def test_people(client):
    _fn()(op="people", per_page=20, sort_field="nickname", sort_order="ascending",
          date_end="2026-03-01T00:00:00Z")
    client.return_value.list_people.assert_called_once_with(
        1, per_page=20, sort_field="nickname", sort_order="ascending",
        filter_date_start=None, filter_date_end="2026-03-01T00:00:00Z")
    _fn()(op="search_people", query="example.com", page=3)
    client.return_value.search_people.assert_called_once_with(
        3, "example.com", per_page=None, sort_field=None, sort_order=None,
        filter_date_start=None, filter_date_end=None)


@pytest.mark.parametrize("op, method", [
    ("person", "get_person"), ("person_data", "get_person_data")])
def test_one_person(client, op, method):
    _fn()(op=op, people_id="jane@example.test")
    getattr(client.return_value, method).assert_called_once_with("jane@example.test")


def test_person_conversations(client):
    _fn()(op="person_conversations", people_id="p_1", unread=True)
    client.return_value.list_person_conversations.assert_called_once_with(
        "p_1", 1, filter_unread=1, filter_resolved=None, filter_not_resolved=None)


def test_inboxes_and_inbox(client):
    _fn()(op="inboxes", page=2)
    _fn()(op="inbox", inbox_id="inbox_1")
    client.return_value.list_inboxes.assert_called_once_with(2)
    client.return_value.get_inbox.assert_called_once_with("inbox_1")


def test_helpdesk(client):
    _fn()(op="helpdesk")
    _fn()(op="helpdesk_locales")
    _fn()(op="helpdesk_pages", locale="fr", content_type="articles")
    _fn()(op="helpdesk_tree", locale="en", content_type="guides", search_title="refund",
          entry_status="draft", sub_path="/billing")
    c = client.return_value
    c.get_helpdesk.assert_called_once_with()
    c.list_helpdesk_locales.assert_called_once_with(1)
    c.list_helpdesk_pages.assert_called_once_with("fr", "articles", 1)
    c.list_helpdesk_tree.assert_called_once_with(
        "en", "guides", 1, sub_path="/billing", search_title="refund",
        search_filter_status="draft", filter_date_start=None, filter_date_end=None)


@pytest.mark.parametrize("op, method", [
    ("helpdesk_entry", "get_helpdesk_entry"), ("helpdesk_content", "get_helpdesk_content"),
    ("helpdesk_metadata", "get_helpdesk_metadata")])
def test_one_helpdesk_entry(client, op, method):
    _fn()(op=op, locale="en", content_type="articles", tree_path="getting-started/install")
    getattr(client.return_value, method).assert_called_once_with(
        "en", "articles", "getting-started/install")


# --- vue par défaut des listes ------------------------------------------------------

def test_conversation_list_view_drops_the_heavy_keys_and_names_them(client):
    rows = [{"session_id": "session_1", "state": "pending", "participants": [{"x": 1}],
             "compose": {}, "meta": {"origin": "chat"}}]
    client.return_value.list_conversations.return_value = {"error": False, "data": rows}
    out = _fn()(op="conversations")
    assert out["data"] == [{"session_id": "session_1", "state": "pending",
                            "meta": {"origin": "chat"}}]
    assert out["projection"]["omitted"] == ["compose", "participants"]
    assert out["error"] is False
    assert _fn()(op="conversations", full=True)["data"] == rows


def test_people_list_view_drops_the_notepad(client):
    rows = [{"people_id": "p_1", "email": "a@example.test", "notepad": "note"}]
    client.return_value.search_people.return_value = {"data": rows}
    out = _fn()(op="search_people", query="example.test")
    assert out["data"] == [{"people_id": "p_1", "email": "a@example.test"}]
    assert out["projection"]["omitted"] == ["notepad"]


def test_full_only_on_the_lists(client):
    with pytest.raises(McpError, match="full"):
        _fn()(op="person", people_id="p_1", full=True)
    client.assert_not_called()


# --- refus locaux ------------------------------------------------------------------

@pytest.mark.parametrize("kwargs, name", [
    ({"op": "inbox"}, "inbox_id"),
    ({"op": "messages"}, "session_id"),
    ({"op": "search_conversations"}, "query"),
    ({"op": "search_people", "query": ""}, "query"),
    ({"op": "person_data"}, "people_id"),
    ({"op": "helpdesk_pages", "locale": "en"}, "content_type"),
    ({"op": "helpdesk_content", "locale": "en", "content_type": "articles"}, "tree_path"),
])
def test_required_argument_is_named(client, kwargs, name):
    with pytest.raises(McpError, match=name):
        _fn()(**kwargs)
    client.assert_not_called()


@pytest.mark.parametrize("kwargs, name", [
    ({"op": "website", "page": 2}, "page"),
    ({"op": "conversations", "query": "x"}, "query"),
    ({"op": "people", "order": "updated"}, "order"),
    ({"op": "messages", "session_id": "s", "page": 2}, "page"),
    ({"op": "person", "people_id": "p", "resolved": True}, "resolved"),
    ({"op": "helpdesk_entry", "locale": "en", "content_type": "articles",
      "tree_path": "a", "search_title": "x"}, "search_title"),
])
def test_ignored_argument_is_refused(client, kwargs, name):
    with pytest.raises(McpError, match=name):
        _fn()(**kwargs)
    client.assert_not_called()


def test_a_local_refusal_of_the_client_is_an_invalid_params(client):
    client.return_value.list_people.side_effect = ValueError("`per_page` must be between 20 and 50")
    with pytest.raises(McpError, match="per_page"):
        _fn()(op="people", per_page=5)


# --- refus de Crisp -----------------------------------------------------------------

def _upstream(status):
    from oto.tools.common.errors import UpstreamHTTPError
    return UpstreamHTTPError(status, {"error": True, "reason": "x", "data": {}}, service="crisp")


@pytest.mark.parametrize("status, op, kwargs, words", [
    (401, "website", {}, "rolled"),
    (402, "website", {}, "plan"),
    (403, "website", {}, "website ID"),
    (404, "conversation", {"session_id": "s"}, "session_id"),
    (404, "person", {"people_id": "p"}, "people_id"),
    (404, "helpdesk", {}, "knowledge base"),
    (420, "website", {}, "10,000"),
    (429, "website", {}, "10,000"),
    (503, "website", {}, "temporarily"),
])
def test_crisp_refusals_are_translated(client, status, op, kwargs, words):
    method = {"website": "get_website", "conversation": "get_conversation",
              "person": "get_person", "helpdesk": "get_helpdesk"}[op]
    getattr(client.return_value, method).side_effect = _upstream(status)
    with pytest.raises(McpError, match=words) as caught:
        _fn()(op=op, **kwargs)
    assert "crisp_" not in str(caught.value)


# --- sonde ------------------------------------------------------------------------

def test_probe_reads_the_workspace(client):
    from oto_mcp.tools import crisp as C
    client.return_value.get_website.return_value = {"error": False,
                                                    "data": {"website_id": "site-test"}}
    C._verify(dict(CREDS))
    client.assert_called_once_with(**CREDS)


def test_probe_refuses_a_rejected_token(client):
    from oto_mcp.connectors import verify as connector_verify
    from oto_mcp.tools import crisp as C
    client.return_value.get_website.side_effect = _upstream(401)
    with pytest.raises(connector_verify.NonAutorise):
        C._verify(dict(CREDS))


def test_probe_refuses_an_answer_without_the_workspace(client):
    from oto_mcp.tools import crisp as C
    client.return_value.get_website.return_value = {"error": False, "data": {}}
    with pytest.raises(RuntimeError, match="website_id"):
        C._verify(dict(CREDS))
