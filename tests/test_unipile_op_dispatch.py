"""Dispatch `op=` des 8 tools `linkedin_*` (ADR 0047 §Amendement, appliqué au
connecteur unipile le 2026-08-10 : 38 tools → 8).

Ce que ce fichier verrouille, et que les autres tests unipile ne couvraient PAS : ils
exercent des helpers purs (`_slim`, `_canonical_li_identifier`, rate-limit) — aucun ne
touchait la SURFACE. Une consolidation par `op=` déplace précisément le risque là :
une op mal câblée appelle silencieusement la mauvaise méthode du client, et rien ne
casse au boot. D'où, pour chaque op : la méthode client appelée, et le refus explicite
d'une op inconnue (jamais un fallback muet).
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from mcp.shared.exceptions import McpError


def _tool(name: str):
    from fastmcp import FastMCP
    from oto_mcp.tools import unipile as U

    m = FastMCP("t")
    U.register(m)
    return asyncio.run(m.get_tool(name)).fn


@pytest.fixture
def client(monkeypatch):
    """Faux client Unipile + `sub` résolu (les ops de scrape passent par `_scrape`)."""
    from oto_mcp.tools import unipile as U

    inst = MagicMock()
    monkeypatch.setattr(U, "unipile_client", lambda *a, **k: inst)
    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: "sub-1")
    monkeypatch.setattr(U, "_rate_limit_guard", lambda sub: None)
    U._COMPANY_CACHE.clear()
    return inst


# --- profil : membre / société / activité / actions ---------------------------

@pytest.mark.parametrize("op,kwargs,method", [
    ("me", {}, "get_own_profile"),
    ("person", {"identifier": "marie-dupont"}, "get_profile"),
    ("company", {"identifier": "otomata"}, "get_company"),
    ("posts", {"identifier": "marie-dupont"}, "list_member_posts"),
    ("comments", {"identifier": "marie-dupont"}, "list_member_comments"),
    ("reactions", {"identifier": "ACoAA1"}, "list_member_reactions"),
    ("followers", {}, "list_followers"),
    ("following", {}, "list_following"),
    ("endorse", {"identifier": "ACoAA1", "skill_endorsement_id": 7}, "endorse_profile"),
    ("action", {"identifier": "ACoAA1", "api": "recruiter",
                "action": "rejectApplicant"}, "member_action"),
])
def test_profile_ops_route_to_the_right_client_method(client, op, kwargs, method):
    _tool("linkedin_profile")(op=op, **kwargs)
    getattr(client, method).assert_called_once()


def test_profile_person_strips_diacritics(client):
    """LinkedIn génère ses slugs en ASCII : un slug accentué fait répondre un 403
    « Insufficient permissions » trompeur (#180)."""
    _tool("linkedin_profile")(op="person", identifier="nicolas-chéhanne")
    assert client.get_profile.call_args.args[0] == "nicolas-chehanne"


def test_profile_company_is_cached(client):
    """La route société est la plus contrainte amont (~100 fiches/12h) : un 2e appel
    sur la même société ne doit PAS retaper Unipile."""
    client.get_company.return_value = {"name": "Otomata"}
    t = _tool("linkedin_profile")
    assert t(op="company", identifier="otomata") == {"name": "Otomata"}
    assert t(op="company", identifier="Otomata") == {"name": "Otomata"}
    client.get_company.assert_called_once()


# --- messagerie ---------------------------------------------------------------

@pytest.mark.parametrize("op,kwargs,method", [
    ("list", {}, "list_chats"),
    ("read", {"chat_id": "c1"}, "list_messages"),
    ("send", {"chat_id": "c1", "text": "hello"}, "send_message"),
    ("attendees", {"chat_id": "c1"}, "list_chat_attendees"),
    ("contacts", {}, "list_attendees"),
    ("update", {"chat_id": "c1", "action": "setReadStatus", "value": True}, "patch_chat"),
    ("react", {"message_id": "m1", "reaction": "👍", "chat_id": "c1"}, "react_message"),
])
def test_chat_ops_route_to_the_right_client_method(client, op, kwargs, method):
    _tool("linkedin_chat")(op=op, **kwargs)
    getattr(client, method).assert_called_once()


def test_chat_send_refuses_without_a_destination(client):
    """Ni fil ni destinataire = message qui part nulle part : refus explicite, pas un
    appel amont qui échouera plus loin avec un message opaque."""
    with pytest.raises(McpError, match="chat_id"):
        _tool("linkedin_chat")(op="send", text="hello")
    client.send_message.assert_not_called()


def test_chat_react_omits_chat_id_when_absent(client):
    """`chat_id` est requis par l'API v2 mais absent de la v1 : on ne passe le kwarg
    que s'il est fourni, pour rester compatible d'un oto-core plus ancien."""
    _tool("linkedin_chat")(op="react", message_id="m1", reaction="👍")
    assert "chat_id" not in client.react_message.call_args.kwargs


# --- publications -------------------------------------------------------------

@pytest.mark.parametrize("op,kwargs,method", [
    ("get", {"post_id": "urn:li:1"}, "get_post"),
    ("engagement", {"post_id": "urn:li:1"}, "list_comments"),
    ("create", {"text": "hello"}, "create_post"),
    ("comment", {"post_id": "urn:li:1", "text": "bravo"}, "comment_post"),
    ("react", {"post_id": "urn:li:1"}, "react_post"),
])
def test_post_ops_route_to_the_right_client_method(client, op, kwargs, method):
    _tool("linkedin_post")(op=op, **kwargs)
    getattr(client, method).assert_called_once()


def test_post_engagement_switches_on_kind(client):
    _tool("linkedin_post")(op="engagement", post_id="urn:li:1", kind="reactions")
    client.list_reactions.assert_called_once()
    client.list_comments.assert_not_called()


# --- réseau -------------------------------------------------------------------

@pytest.mark.parametrize("op,kwargs,method", [
    ("relations", {}, "list_relations"),
    ("invitations", {}, "list_invitations"),
    ("invite", {"provider_id": "ACoAA1"}, "send_invitation"),
    ("handle", {"invitation_id": "i1", "shared_secret": "s1"}, "handle_invitation"),
    ("cancel", {"invitation_id": "i1"}, "cancel_invitation"),
])
def test_network_ops_route_to_the_right_client_method(client, op, kwargs, method):
    _tool("linkedin_network")(op=op, **kwargs)
    getattr(client, method).assert_called_once()


def test_network_relations_projection(client):
    client.list_relations.return_value = {
        "items": [{"name": "X", "member_id": "1", "profile_picture_url": "…"}]}
    out = _tool("linkedin_network")(op="relations", fields=["name", "member_id"])
    assert out["items"] == [{"name": "X", "member_id": "1"}]


def test_network_handle_requires_the_shared_secret(client):
    """`invitation_id` et `shared_secret` viennent du MÊME item : sans le second,
    l'appel amont échoue — autant le dire ici."""
    with pytest.raises(McpError, match="shared_secret"):
        _tool("linkedin_network")(op="handle", invitation_id="i1")


# --- compte premium & recruiter -----------------------------------------------

@pytest.mark.parametrize("op,kwargs,method", [
    ("contracts", {}, "list_contracts"),
    ("select", {"contract_id": "k1"}, "select_contract"),
    ("inmail_balance", {}, "inmail_balance"),
])
def test_account_ops_route_to_the_right_client_method(client, op, kwargs, method):
    _tool("linkedin_account")(op=op, **kwargs)
    getattr(client, method).assert_called_once()


@pytest.mark.parametrize("op,kwargs,method", [
    ("postings", {}, "list_job_postings"),
    ("posting", {"job_id": "j1"}, "get_job_posting"),
    ("applicants", {"job_id": "j1"}, "list_job_applicants"),
    ("applicant", {"job_id": "j1", "applicant_id": "a1"}, "get_job_applicant"),
    ("projects", {}, "list_hiring_projects"),
])
def test_job_ops_route_to_the_right_client_method(client, op, kwargs, method):
    _tool("linkedin_job")(op=op, **kwargs)
    getattr(client, method).assert_called_once()


# --- refus ---------------------------------------------------------------------

@pytest.mark.parametrize("tool", [
    "linkedin_profile", "linkedin_chat", "linkedin_post",
    "linkedin_network", "linkedin_account", "linkedin_job",
])
def test_unknown_op_is_refused_with_the_allowed_list(client, tool):
    """Une op inconnue doit lever en nommant les ops valides — jamais retomber
    silencieusement sur le défaut (l'agent croirait sa demande honorée)."""
    with pytest.raises(McpError, match="op doit être"):
        _tool(tool)(op="nope")


@pytest.mark.parametrize("tool,op,missing", [
    ("linkedin_profile", "person", "identifier"),
    ("linkedin_profile", "endorse", "skill_endorsement_id"),
    ("linkedin_chat", "read", "chat_id"),
    ("linkedin_post", "create", "text"),
    ("linkedin_network", "invite", "provider_id"),
    ("linkedin_account", "select", "contract_id"),
    ("linkedin_job", "applicant", "applicant_id"),
])
def test_missing_required_arg_names_the_op_and_the_arg(client, tool, op, missing):
    kwargs = {"identifier": "x"} if tool == "linkedin_profile" and op == "endorse" else {}
    if (tool, op) == ("linkedin_job", "applicant"):
        kwargs = {"job_id": "j1"}
    with pytest.raises(McpError, match=missing):
        _tool(tool)(op=op, **kwargs)


# --- canaux non-LinkedIn (même connecteur, même factory) -----------------------

@pytest.mark.parametrize("channel", ["whatsapp", "telegram", "instagram",
                                     "messenger", "twitter"])
@pytest.mark.parametrize("op,kwargs,method", [
    ("list", {}, "list_chats"),
    ("read", {"chat_id": "c1"}, "list_messages"),
    ("send", {"chat_id": "c1", "text": "hello"}, "send_message"),
])
def test_channel_chat_ops_route_to_the_right_client_method(
        client, channel, op, kwargs, method):
    """Les 5 canaux passent par la MÊME factory : un seul jeu de cas les couvre
    tous, et une régression sur la factory les casserait tous ensemble."""
    from fastmcp import FastMCP
    from oto_mcp.tools import unipile as U

    m = FastMCP("t")
    U.register_messaging_tools(m, channel.upper())
    asyncio.run(m.get_tool(f"{channel}_chat")).fn(op=op, **kwargs)
    getattr(client, method).assert_called_once()


def test_channel_chat_refuses_unknown_op_and_missing_args(client):
    from fastmcp import FastMCP
    from oto_mcp.tools import unipile as U

    m = FastMCP("t")
    U.register_messaging_tools(m, "WHATSAPP")
    fn = asyncio.run(m.get_tool("whatsapp_chat")).fn
    with pytest.raises(McpError, match="op doit être"):
        fn(op="nope")
    with pytest.raises(McpError, match="chat_id"):
        fn(op="read")
    with pytest.raises(McpError, match="text"):
        fn(op="send", chat_id="c1")
    with pytest.raises(McpError, match="chat_id"):
        fn(op="send", text="hello")
    client.send_message.assert_not_called()
