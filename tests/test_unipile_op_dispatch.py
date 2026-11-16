"""Dispatch `op=` des tools `linkedin_unipile_*` (ADR 0047 §Amendement, appliqué au
connecteur unipile le 2026-08-10 : 38 tools → 8 ; l'envoi sorti des lecteurs dans trois
outils d'écriture le 09/10/2026, #1189 — la garde de ce découpage vit dans
`tests/test_unipile_lecture_envoi_1189.py`).

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
from oto_mcp.mcp_errors import McpError
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
])
def test_profile_ops_route_to_the_right_client_method(client, op, kwargs, method):
    _tool("linkedin_unipile_profile")(op=op, **kwargs)
    getattr(client, method).assert_called_once()


@pytest.mark.parametrize("op,kwargs,method", [
    ("endorse", {"identifier": "ACoAA1", "skill_endorsement_id": 7}, "endorse_profile"),
    ("update_member", {"identifier": "ACoAA1", "api": "recruiter",
                       "action": "rejectApplicant"}, "member_action"),
])
def test_member_acts_route_to_the_right_client_method(client, op, kwargs, method):
    _tool("linkedin_unipile_act")(op=op, **kwargs)
    getattr(client, method).assert_called_once()


def test_profile_person_sends_the_accented_slug_as_given(client):
    """Un slug peut porter une lettre non ASCII — il part tel quel d'abord ;
    la forme sans accents n'est qu'un repli (#180, `test_unipile_slug_accents.py`)."""
    _tool("linkedin_unipile_profile")(op="person", identifier="chloé-dupré")
    assert client.get_profile.call_args.args[0] == "chloé-dupré"


def test_profile_company_is_cached(client):
    """La route société est la plus contrainte amont (~100 fiches/12h) : un 2e appel
    sur la même société ne doit PAS retaper Unipile."""
    client.get_company.return_value = {"name": "Otomata"}
    t = _tool("linkedin_unipile_profile")
    assert t(op="company", identifier="otomata") == {"name": "Otomata"}
    assert t(op="company", identifier="Otomata") == {"name": "Otomata"}
    client.get_company.assert_called_once()


# --- messagerie ---------------------------------------------------------------

@pytest.mark.parametrize("op,kwargs,method", [
    ("list", {}, "list_chats"),
    ("read", {"chat_id": "c1"}, "list_messages"),
    ("attendees", {"chat_id": "c1"}, "list_chat_attendees"),
    ("contacts", {}, "list_attendees"),
])
def test_chat_ops_route_to_the_right_client_method(client, op, kwargs, method):
    _tool("linkedin_unipile")(op=op, **kwargs)
    getattr(client, method).assert_called_once()


@pytest.mark.parametrize("op,kwargs,method", [
    ("send", {"chat_id": "c1", "text": "hello"}, "send_message"),
    ("update", {"chat_id": "c1", "action": "setReadStatus", "value": True}, "patch_chat"),
    ("react_message", {"message_id": "m1", "reaction": "👍", "chat_id": "c1"},
     "react_message"),
])
def test_send_ops_route_to_the_right_client_method(client, op, kwargs, method):
    _tool("linkedin_unipile_act")(op=op, **kwargs)
    getattr(client, method).assert_called_once()


def test_chat_send_refuses_without_a_destination(client):
    """Ni fil ni destinataire = message qui part nulle part : refus explicite, pas un
    appel amont qui échouera plus loin avec un message opaque."""
    with pytest.raises(McpError, match="chat_id"):
        _tool("linkedin_unipile_act")(op="send", text="hello")
    client.send_message.assert_not_called()


@pytest.mark.parametrize("limit", [0, 26, 50])
def test_chat_list_refuses_a_page_linkedin_refuses_naming_the_bound(client, limit):
    """#873 — au-delà de 25, Unipile répond « 400 Invalid querystring » sans nommer la
    borne : le refus la nomme, et renvoie à `cursor`, avant tout appel amont."""
    with pytest.raises(McpError, match="1 to 25") as e:
        _tool("linkedin_unipile")(op="list", limit=limit)
    assert "cursor" in str(e.value.error.message)
    client.list_chats.assert_not_called()


def test_chat_list_accepts_the_bound_itself(client):
    """Contrôle positif : 25 passe (mesuré le 11/09/2026 sur un compte réel)."""
    _tool("linkedin_unipile")(op="list", limit=25)
    assert client.list_chats.call_args.kwargs["limit"] == 25


def test_chat_react_omits_chat_id_when_absent(client):
    """`chat_id` est requis par l'API v2 mais absent de la v1 : on ne passe le kwarg
    que s'il est fourni, pour rester compatible d'un oto-core plus ancien."""
    _tool("linkedin_unipile_act")(op="react_message", message_id="m1", reaction="👍")
    assert "chat_id" not in client.react_message.call_args.kwargs


# --- publications -------------------------------------------------------------

@pytest.mark.parametrize("op,kwargs,method", [
    ("get", {"post_id": "urn:li:1"}, "get_post"),
    ("engagement", {"post_id": "urn:li:1"}, "list_comments"),
])
def test_post_ops_route_to_the_right_client_method(client, op, kwargs, method):
    _tool("linkedin_unipile_post")(op=op, **kwargs)
    getattr(client, method).assert_called_once()


@pytest.mark.parametrize("op,kwargs,method", [
    ("publish_post", {"text": "hello"}, "create_post"),
    ("comment_post", {"post_id": "urn:li:1", "text": "bravo"}, "comment_post"),
    ("react_post", {"post_id": "urn:li:1"}, "react_post"),
])
def test_publish_ops_route_to_the_right_client_method(client, op, kwargs, method):
    _tool("linkedin_unipile_act")(op=op, **kwargs)
    getattr(client, method).assert_called_once()


def test_post_engagement_switches_on_kind(client):
    _tool("linkedin_unipile_post")(op="engagement", post_id="urn:li:1", kind="reactions")
    client.list_reactions.assert_called_once()
    client.list_comments.assert_not_called()


# --- réseau -------------------------------------------------------------------

@pytest.mark.parametrize("op,kwargs,method", [
    ("relations", {}, "list_relations"),
    ("invitations", {}, "list_invitations"),
])
def test_network_ops_route_to_the_right_client_method(client, op, kwargs, method):
    _tool("linkedin_unipile_network")(op=op, **kwargs)
    getattr(client, method).assert_called_once()


@pytest.mark.parametrize("op,kwargs,method", [
    ("invite", {"provider_id": "ACoAA1"}, "send_invitation"),
    ("accept_invitation", {"invitation_id": "i1", "shared_secret": "s1"},
     "handle_invitation"),
    ("decline_invitation", {"invitation_id": "i1", "shared_secret": "s1"},
     "handle_invitation"),
    ("cancel_invitation", {"invitation_id": "i1"}, "cancel_invitation"),
])
def test_invite_ops_route_to_the_right_client_method(client, op, kwargs, method):
    _tool("linkedin_unipile_act")(op=op, **kwargs)
    getattr(client, method).assert_called_once()


def test_network_relations_projection(client):
    client.list_relations.return_value = {
        "items": [{"name": "X", "member_id": "1", "profile_picture_url": "…"}]}
    out = _tool("linkedin_unipile_network")(op="relations", fields=["name", "member_id"])
    assert out["items"] == [{"name": "X", "member_id": "1"}]


def test_network_relations_projection_allege_vraiment_la_page(client):
    """#91 (retour 731) : `_norm` rend la liste deux fois (`data` + `items`). Projeter
    le seul `items` laissait `data` intact — rien n'était allégé. La page servie ne
    porte plus qu'UNE liste et UN curseur, projetés ensemble ; `member_id` (la clé de
    déduplication prescrite) est toujours gardé."""
    lst = [{"name": "X", "member_id": "1", "headline": "H", "profile_picture_url": "…"}]
    client.list_relations.return_value = {"data": lst, "items": lst,
                                          "next_cursor": "C", "cursor": "C"}
    out = _tool("linkedin_unipile_network")(op="relations", fields=["name"])
    assert "data" not in out and "next_cursor" not in out
    assert out["cursor"] == "C"
    assert out["items"] == [{"name": "X", "member_id": "1"}]


def test_network_relations_cle_inconnue_refusee_pas_ecartee(client):
    """Une clé que la page ne porte pas rendait des objets VIDES, lus comme une perte
    de données : elle est refusée, avec les clés présentes."""
    client.list_relations.return_value = {
        "items": [{"name": "X", "member_id": "1"}], "cursor": None}
    with pytest.raises(McpError, match="full_name"):
        _tool("linkedin_unipile_network")(op="relations", fields=["full_name"])


def test_network_handle_requires_the_shared_secret(client):
    """`invitation_id` et `shared_secret` viennent du MÊME item : sans le second,
    l'appel amont échoue — autant le dire ici."""
    with pytest.raises(McpError, match="shared_secret"):
        _tool("linkedin_unipile_act")(op="accept_invitation", invitation_id="i1")
    client.handle_invitation.assert_not_called()


# --- compte premium & recruiter -----------------------------------------------

@pytest.mark.parametrize("op,kwargs,method", [
    ("contracts", {}, "list_contracts"),
    ("select", {"contract_id": "k1"}, "select_contract"),
    ("inmail_balance", {}, "inmail_balance"),
])
def test_account_ops_route_to_the_right_client_method(client, op, kwargs, method):
    _tool("linkedin_unipile_account")(op=op, **kwargs)
    getattr(client, method).assert_called_once()


@pytest.mark.parametrize("op,kwargs,method", [
    ("postings", {}, "list_job_postings"),
    ("posting", {"job_id": "j1"}, "get_job_posting"),
    ("applicants", {"job_id": "j1"}, "list_job_applicants"),
    ("applicant", {"job_id": "j1", "applicant_id": "a1"}, "get_job_applicant"),
    ("projects", {}, "list_hiring_projects"),
])
def test_job_ops_route_to_the_right_client_method(client, op, kwargs, method):
    _tool("linkedin_unipile_job")(op=op, **kwargs)
    getattr(client, method).assert_called_once()


# --- refus ---------------------------------------------------------------------

@pytest.mark.parametrize("tool", [
    "linkedin_unipile_profile", "linkedin_unipile", "linkedin_unipile_post",
    "linkedin_unipile_network", "linkedin_unipile_account", "linkedin_unipile_job",
    "linkedin_unipile_act",
])
def test_unknown_op_is_refused_with_the_allowed_list(client, tool):
    """Une op inconnue doit lever en nommant les ops valides — jamais retomber
    silencieusement sur le défaut (l'agent croirait sa demande honorée)."""
    with pytest.raises(McpError, match="op must be"):
        _tool(tool)(op="nope")


@pytest.mark.parametrize("tool,op,missing", [
    ("linkedin_unipile_profile", "person", "identifier"),
    ("linkedin_unipile_act", "endorse", "skill_endorsement_id"),
    ("linkedin_unipile", "read", "chat_id"),
    ("linkedin_unipile_act", "publish_post", "text"),
    ("linkedin_unipile_act", "comment_post", "post_id"),
    ("linkedin_unipile_act", "update", "chat_id"),
    ("linkedin_unipile_act", "invite", "provider_id"),
    ("linkedin_unipile_account", "select", "contract_id"),
    ("linkedin_unipile_job", "applicant", "applicant_id"),
])
def test_missing_required_arg_names_the_op_and_the_arg(client, tool, op, missing):
    kwargs = {"identifier": "x"} if op == "endorse" else {}
    if (tool, op) == ("linkedin_unipile_job", "applicant"):
        kwargs = {"job_id": "j1"}
    with pytest.raises(McpError, match=missing):
        _tool(tool)(op=op, **kwargs)


# --- canaux non-LinkedIn (même connecteur, même factory) -----------------------

@pytest.mark.parametrize("channel", ["whatsapp", "telegram", "instagram"])
@pytest.mark.parametrize("op,kwargs,method", [
    ("list", {}, "list_chats"),
    ("read", {"chat_id": "c1"}, "list_messages"),
])
def test_channel_chat_ops_route_to_the_right_client_method(
        client, channel, op, kwargs, method):
    """Les canaux passent par la MÊME factory : un seul jeu de cas les couvre
    tous, et une régression sur la factory les casserait tous ensemble."""
    from fastmcp import FastMCP
    from oto_mcp.tools import unipile as U

    m = FastMCP("t")
    U.register_messaging_tools(m, channel.upper())
    asyncio.run(m.get_tool(channel)).fn(op=op, **kwargs)
    getattr(client, method).assert_called_once()


@pytest.mark.parametrize("channel", ["whatsapp", "telegram", "instagram"])
def test_channel_send_routes_to_send_message(client, channel):
    from fastmcp import FastMCP
    from oto_mcp.tools import unipile as U

    m = FastMCP("t")
    U.register_messaging_tools(m, channel.upper())
    asyncio.run(m.get_tool(f"{channel}_act")).fn(text="hello", chat_id="c1")
    client.send_message.assert_called_once_with("hello", chat_id="c1", attendee_id=None)


def test_channel_chat_refuses_unknown_op_and_missing_args(client):
    from fastmcp import FastMCP
    from oto_mcp.tools import unipile as U

    m = FastMCP("t")
    U.register_messaging_tools(m, "WHATSAPP")
    fn = asyncio.run(m.get_tool("whatsapp")).fn
    with pytest.raises(McpError, match="op must be"):
        fn(op="nope")
    with pytest.raises(McpError, match="chat_id"):
        fn(op="read")
    send = asyncio.run(m.get_tool("whatsapp_act")).fn
    with pytest.raises(McpError, match="text"):
        send(text="", chat_id="c1")
    with pytest.raises(McpError, match="chat_id"):
        send(text="hello")
    client.send_message.assert_not_called()


def test_linkedin_read_pose_la_reference_des_pieces(client):
    """#1149 : la pièce d'un message LinkedIn se récupère par sa `source`, comme sur les
    autres canaux — la page reste la page brute, la référence s'y ajoute."""
    url = "https://unipile.example/v2/acc_forme/messages/m1/attachments/a1"
    msgs = [{"id": "m1", "attachments": [{"url": url}]}]
    client.list_messages.return_value = {"data": msgs, "items": msgs}
    out = _tool("linkedin_unipile")(op="read", chat_id="c1")
    assert out["items"][0]["attachments"][0]["source"] == {
        "kind": "unipile", "channel": "linkedin", "url": url}


_FIL = {"id": "c1", "name": None, "timestamp": "2026-10-20T08:00:00Z",
        "attendee_name": "Jeanne", "unread_count": 0,
        "last_message": {"id": "m9", "sender_id": "p-2", "text": "x" * 5000,
                         "attachments": []}}


def test_chat_list_projette_et_tronque_sur_demande(client):
    """#1353 : une page de 20 fils pesait ~100 000 caractères, le texte intégral du
    dernier message compris. `fields` (clé pointée pour une sous-clé) et
    `text_max_chars` allègent la page ; `id` reste pour enchaîner `op="read"`."""
    client.list_chats.return_value = {"data": [_FIL], "items": [_FIL],
                                      "next_cursor": "k2", "cursor": "k2"}
    out = _tool("linkedin_unipile")(
        op="list", fields=["timestamp", "last_message.sender_id", "last_message.text"],
        text_max_chars=200)
    assert out["cursor"] == "k2" and "data" not in out and "next_cursor" not in out
    (fil,) = out["items"]
    assert fil == {"id": "c1", "timestamp": "2026-10-20T08:00:00Z",
                   "last_message": {"sender_id": "p-2", "text": "x" * 200 + "…"}}


def test_chat_list_tronque_sans_projeter(client):
    client.list_chats.return_value = {"items": [_FIL], "cursor": None}
    (fil,) = _tool("linkedin_unipile")(op="list", text_max_chars=100)["items"]
    assert fil["last_message"]["text_truncated"] is True
    assert len(fil["last_message"]["text"]) == 101 and fil["attendee_name"] == "Jeanne"


def test_chat_list_sans_reglage_rend_la_page_telle_quelle(client):
    page = {"items": [_FIL], "data": [_FIL], "cursor": None}
    client.list_chats.return_value = page
    assert _tool("linkedin_unipile")(op="list") is page


def test_chat_list_refuse_un_champ_absent_de_la_page(client):
    client.list_chats.return_value = {"items": [_FIL], "cursor": None}
    with pytest.raises(McpError, match="last_message.sendr"):
        _tool("linkedin_unipile")(op="list", fields=["last_message.sendr"])


def test_chat_read_tronque_les_messages(client, monkeypatch):
    from oto_mcp.tools import unipile_messagerie
    monkeypatch.setattr(unipile_messagerie, "references_des_pieces", lambda p, c: p)
    client.list_messages.return_value = {"items": [{"id": "m1", "text": "y" * 900}]}
    (msg,) = _tool("linkedin_unipile")(op="read", chat_id="c1", text_max_chars=50)["items"]
    assert msg["text"] == "y" * 50 + "…" and msg["text_truncated"] is True


def test_profil_par_url_complete_part_en_slug(client):
    """#1353 : `identifier` = URL complète → faux 403 ; ramenée au slug avant l'appel."""
    _tool("linkedin_unipile_profile")(
        op="person", identifier="https://www.linkedin.com/in/jean-dupont/?trk=x")
    assert client.get_profile.call_args.args[0] == "jean-dupont"
    _tool("linkedin_unipile_profile")(
        op="company", identifier="https://www.linkedin.com/company/acme-corp/")
    assert client.get_company.call_args.args[0] == "acme-corp"
