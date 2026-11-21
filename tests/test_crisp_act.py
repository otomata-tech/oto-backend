"""Connecteur Crisp — l'outil qui agit, `crisp_act` ; celui qui lit : `test_crisp_tools.py`.

Ce fichier verrouille ce qui est propre à `crisp_act` :

- `ACTE`, ses ops, et le texte servi qui dit que les écritures touchent de vraies
  conversations de patients et qu'un message envoyé ne se reprend pas ;
- chaque op appelle la BONNE méthode du client, avec les bons arguments ;
- l'envoi au contact exige `confirm_send=True`, que nulle autre op ne prend : sans lui,
  rien ne part et la réponse est un aperçu lu chez Crisp ;
- les segments s'ajoutent et se retirent sans écraser les autres, Crisp relu avant et
  après : une liste que Crisp n'a pas prise est une erreur ;
- aucun argument ignoré en silence, les requis nommés, rien n'est appelé avant ;
- la traduction des refus propres aux écritures (406, 409, 404).

Client doublé (`oto.tools.crisp.CrispClient`), jamais appelé en vrai. Valeurs factices.
"""
import asyncio
from unittest.mock import patch

import pytest

from oto_mcp.mcp_errors import McpError

CREDS = {"token_identifier": "tid_test", "token_key": "tkey_test", "website_id": "site-test"}
SESSION = "session_00000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _fake_creds(monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_credential_fields",
                        lambda provider, account=None: dict(CREDS))


def _tool():
    from fastmcp import FastMCP
    from oto_mcp.tools import crisp

    m = FastMCP("t")
    crisp.register(m)
    return {t.name: t for t in asyncio.run(m._list_tools())}["crisp_act"]


def _act(**kwargs):
    return _tool().fn(**kwargs)


@pytest.fixture()
def client():
    with patch("oto.tools.crisp.CrispClient") as cls:
        yield cls


def _upstream(status):
    from oto.tools.common.errors import UpstreamHTTPError
    return UpstreamHTTPError(status, {"error": True, "reason": "x", "data": {}}, service="crisp")


# --- l'outil servi -------------------------------------------------------------------

def test_acte_with_its_ops_and_the_warning():
    from oto_mcp.tools.lecture import ACTE

    t = _tool()
    assert t.annotations.model_copy(update={"title": None}) == ACTE
    assert t.parameters["properties"]["op"]["enum"] == [
        "add_note", "set_state", "update_conversation", "add_segments", "remove_segments",
        "update_person", "update_person_data", "send_message"]
    text = " ".join(t.description.split())
    assert "real conversations and contacts of the workspace (patients included)" in text
    assert "a message sent cannot be taken back" in text
    assert "WhatsApp's 24 hours" in text


def test_only_send_message_takes_confirm_send():
    from oto_mcp.tools import crisp_act as A
    assert [op for op, takes in A._ACT_TAKES.items() if "confirm_send" in takes] == ["send_message"]
    assert set(A._ACT_TAKES) == set(A._ACT_OPS) == set(A._ACT_NEEDS)


# --- ops -> méthodes ------------------------------------------------------------------

def test_writes_reach_the_right_method(client):
    c = client.return_value
    _act(op="add_note", session_id=SESSION, text="for the team")
    _act(op="set_state", session_id=SESSION, state="resolved")
    _act(op="update_conversation", session_id=SESSION, email="jane@example.test")
    _act(op="update_person", people_id="p_1", nickname="Jane Doe", phone="+33600000000")
    _act(op="update_person_data", people_id="p_1", data={"followup": "call back", "score": 3})
    client.assert_called_with(**CREDS)
    c.add_conversation_note.assert_called_once_with(SESSION, "for the team")
    c.change_conversation_state.assert_called_once_with(SESSION, "resolved")
    c.update_conversation_meta.assert_called_once_with(SESSION, email="jane@example.test")
    c.update_person.assert_called_once_with("p_1", nickname="Jane Doe", phone="+33600000000")
    c.update_person_data.assert_called_once_with("p_1", {"followup": "call back", "score": 3})
    c.send_message.assert_not_called()


# --- l'envoi au contact ---------------------------------------------------------------

def test_without_confirm_send_nothing_is_sent_and_a_preview_is_read(client):
    c = client.return_value
    c.get_conversation_meta.return_value = {"data": {"origin": "urn:crisp.im:whatsapp:0",
                                                     "nickname": "Jane Doe"}}
    c.list_conversation_messages.return_value = {"data": [
        {"from": "user", "timestamp": 1000}, {"from": "operator", "timestamp": 3000},
        {"from": "user", "timestamp": 2000}]}
    out = _act(op="send_message", session_id=SESSION, text="Your appointment is confirmed.")
    c.send_message.assert_not_called()
    assert out["sent"] is False
    assert out["preview"] == {"session_id": SESSION, "channel": "urn:crisp.im:whatsapp:0",
                              "contact": "Jane Doe", "text": "Your appointment is confirmed.",
                              "last_contact_message_at": 2000}
    assert "Nothing was sent" in out["next"] and "confirm_send=true" in out["next"]


def test_with_confirm_send_the_text_is_sent_once(client):
    c = client.return_value
    c.send_message.return_value = {"error": False, "reason": "dispatched",
                                   "data": {"fingerprint": 1}}
    out = _act(op="send_message", session_id=SESSION, text="Hello", confirm_send=True)
    c.send_message.assert_called_once_with(SESSION, "Hello")
    c.get_conversation_meta.assert_not_called()
    assert out["reason"] == "dispatched"


@pytest.mark.parametrize("status, words", [
    (406, "messaging rule"), (409, "too many messages"), (404, "session_id"),
])
def test_a_refused_message_says_nothing_was_sent(client, status, words):
    client.return_value.send_message.side_effect = _upstream(status)
    with pytest.raises(McpError, match=words) as caught:
        _act(op="send_message", session_id=SESSION, text="Hello", confirm_send=True)
    assert client.return_value.send_message.call_count == 1
    assert "crisp_" not in str(caught.value)


def test_confirm_send_is_refused_on_another_op(client):
    with pytest.raises(McpError, match="confirm_send"):
        _act(op="add_note", session_id=SESSION, text="x", confirm_send=True)
    client.assert_not_called()


# --- segments --------------------------------------------------------------------------

def test_add_segments_keeps_the_others_and_reads_back(client):
    c = client.return_value
    c.get_conversation_meta.side_effect = [{"data": {"segments": ["vip"]}},
                                           {"data": {"segments": ["vip", "callback"]}}]
    out = _act(op="add_segments", session_id=SESSION, segments=["callback", "vip"])
    c.set_conversation_segments.assert_called_once_with(SESSION, ["vip", "callback"])
    assert out == {"changed": True, "segments": ["vip", "callback"]}


def test_remove_segments_on_a_contact(client):
    c = client.return_value
    c.get_person.side_effect = [{"data": {"segments": ["patient", "followup"]}},
                                {"data": {"segments": ["patient"]}}]
    out = _act(op="remove_segments", people_id="p_1", segments=["followup"])
    c.set_person_segments.assert_called_once_with("p_1", ["patient"])
    assert out["segments"] == ["patient"]


def test_nothing_to_change_sends_nothing(client):
    c = client.return_value
    c.get_person.return_value = {"data": {"segments": ["patient"]}}
    out = _act(op="remove_segments", people_id="p_1", segments=["absent"])
    c.set_person_segments.assert_not_called()
    assert out["changed"] is False


def test_a_change_crisp_did_not_keep_is_an_error(client):
    c = client.return_value
    c.get_conversation_meta.side_effect = [{"data": {"segments": ["vip", "old"]}},
                                           {"data": {"segments": ["vip", "old"]}}]
    with pytest.raises(McpError, match="expected"):
        _act(op="remove_segments", session_id=SESSION, segments=["old"])


@pytest.mark.parametrize("kwargs", [
    {"segments": ["x"]},
    {"segments": ["x"], "session_id": SESSION, "people_id": "p_1"},
])
def test_segments_act_on_one_conversation_or_one_contact(client, kwargs):
    with pytest.raises(McpError, match="ONE conversation"):
        _act(op="add_segments", **kwargs)
    client.assert_not_called()


# --- refus locaux et de Crisp --------------------------------------------------------

@pytest.mark.parametrize("kwargs, name", [
    ({"op": "add_note", "session_id": SESSION}, "text"),
    ({"op": "send_message", "text": "x"}, "session_id"),
    ({"op": "set_state", "session_id": SESSION}, "state"),
    ({"op": "update_person"}, "people_id"),
    ({"op": "update_person", "people_id": "p_1"}, "nickname"),
    ({"op": "update_conversation", "session_id": SESSION}, "nickname"),
    ({"op": "update_person_data", "people_id": "p_1"}, "data"),
    ({"op": "add_segments", "session_id": SESSION, "segments": []}, "segments"),
])
def test_required_argument_is_named(client, kwargs, name):
    with pytest.raises(McpError, match=name):
        _act(**kwargs)
    client.assert_not_called()


@pytest.mark.parametrize("kwargs, name", [
    ({"op": "add_note", "session_id": SESSION, "text": "x", "people_id": "p"}, "people_id"),
    ({"op": "set_state", "session_id": SESSION, "state": "resolved", "text": "x"}, "text"),
    ({"op": "update_person", "people_id": "p", "phone": "+33", "data": {"a": "b"}}, "data"),
])
def test_ignored_argument_is_refused(client, kwargs, name):
    with pytest.raises(McpError, match=name):
        _act(**kwargs)
    client.assert_not_called()


def test_email_taken_and_unknown_contact(client):
    client.return_value.update_person.side_effect = _upstream(409)
    with pytest.raises(McpError, match="already has this email"):
        _act(op="update_person", people_id="p_1", email="jane@example.test")
    client.return_value.update_person_data.side_effect = _upstream(404)
    with pytest.raises(McpError, match="people_id"):
        _act(op="update_person_data", people_id="p_1", data={"a": "b"})


def test_a_local_refusal_of_the_client_says_nothing_was_done(client):
    client.return_value.change_conversation_state.side_effect = ValueError("`state` invalid.")
    with pytest.raises(McpError, match="Nothing was done"):
        _act(op="set_state", session_id=SESSION, state="resolved")
