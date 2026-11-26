"""Lecture et envoi dans des outils DIFFÉRENTS (#1189, décision du 09/10/2026 ; paires des
messageries et acteur LinkedIn unique le 21/10/2026).

Un client MCP (claude.ai, ChatGPT) autorise des OUTILS, pas des opérations : « toujours
autoriser » `whatsapp_chat` pour lire autorisait aussi l'envoi à un tiers — jusqu'au
21/10/2026, par son ancienne `op="send"` encore servie. Ce que ce fichier tient :

- les écrivains déclarent `destructiveHint` + `openWorldHint` ;
- l'écrivain vit sous le MÊME connecteur que son lecteur (même activation, même ACL,
  même compte opéré `_account=`) ;
- le lecteur sert ses lectures, et une op inconnue reste un refus de valeur ;
- un lecteur (`whatsapp`, `linkedin_unipile`, `linkedin_unipile_post`…) est `LECTURE` et
  ne porte aucune op d'envoi : la transition jusqu'au 10/12/2026 a pris fin le 21/10/2026 ;
  l'ancienne op d'un lecteur qui reste refuse en nommant le nouvel appel.
"""
from __future__ import annotations

import asyncio
import typing
from unittest.mock import MagicMock

import pytest
from fastmcp import FastMCP
from pydantic import TypeAdapter

from oto_mcp import call_axes, error_taxonomy
from oto_mcp.tool_visibility import namespace_of
from oto_mcp.tools import unipile as U
from oto_mcp.tools.lecture import en_lecture

CANAUX = ("whatsapp", "telegram", "instagram")

#: lecteur → (écrivain, ops de l'écrivain). La table de la décision, écrite une fois.
DECOUPAGE = {
    **{c: (f"{c}_act", ("send",)) for c in CANAUX},
    "linkedin_unipile": ("linkedin_unipile_act", ("send", "update", "react_message")),
    "linkedin_unipile_network": ("linkedin_unipile_act", (
        "invite", "accept_invitation", "decline_invitation", "cancel_invitation")),
    "linkedin_unipile_post": ("linkedin_unipile_act", ("publish_post", "comment_post",
                                                       "react_post")),
    "linkedin_unipile_profile": ("linkedin_unipile_act", ("endorse", "update_member")),
}

#: Ce qui reste au lecteur.
OPS_LUES = {
    **{c: ["list", "read"] for c in CANAUX},
    "linkedin_unipile": ["list", "read", "attendees", "contacts"],
    "linkedin_unipile_network": ["relations", "invitations"],
    "linkedin_unipile_post": ["feed", "get", "engagement"],
    "linkedin_unipile_profile": ["person", "company", "me", "posts", "comments",
                                 "reactions", "followers", "following"],
}

#: Les anciennes ops d'envoi des lecteurs qui restent : refusées en nommant l'acteur.
ANCIENNES_OPS = {
    "linkedin_unipile_network": ("invite", "handle", "cancel"),
    "linkedin_unipile_post": ("create", "comment", "react"),
    "linkedin_unipile_profile": ("endorse", "action"),
}


def _monte() -> FastMCP:
    m = FastMCP("t")
    U.register(m)
    for c in CANAUX:
        U.register_messaging_tools(m, c.upper())
    return m


@pytest.fixture
def m():
    return _monte()


@pytest.fixture
def client(monkeypatch):
    inst = MagicMock()
    appels = []
    monkeypatch.setattr(U, "unipile_client", lambda *a, **k: (appels.append(a), inst)[1])
    inst.appels = appels
    return inst


def _outil(m, nom):
    return asyncio.run(m.get_tool(nom))


@pytest.mark.parametrize("lecteur", sorted(DECOUPAGE))
def test_l_ecrivain_declare_qu_il_agit_chez_un_tiers(m, lecteur):
    ecrivain = DECOUPAGE[lecteur][0]
    a = _outil(m, ecrivain).annotations
    assert a.readOnlyHint is False and a.destructiveHint is True
    assert a.openWorldHint is True


@pytest.mark.parametrize("lecteur", sorted(DECOUPAGE))
def test_l_ecrivain_vit_sous_le_connecteur_de_son_lecteur(lecteur):
    """Même namespace ⟹ même activation, même ACL, même sélection — et le même axe
    `_account=` (comptes accordés, #55) : un envoi sous un AUTRE compte que la lecture
    serait pire que l'ancien outil."""
    ecrivain = DECOUPAGE[lecteur][0]
    assert namespace_of(ecrivain) == namespace_of(lecteur)
    assert call_axes.accepts_account_axis(ecrivain), ecrivain


def test_une_op_inconnue_reste_un_refus_de_valeur(m, client):
    """Une faute de frappe est la valeur refusée, avec les ops admises — jamais
    exécutée, jamais une clé résolue."""
    with pytest.raises(Exception) as e:
        asyncio.run(m.call_tool("whatsapp", {"op": "sned"}))
    msg = error_taxonomy.classify(e.value).message
    assert "'list'" in msg and "'read'" in msg, msg
    assert client.appels == []


@pytest.mark.parametrize("lecteur", sorted(DECOUPAGE))
def test_les_lectures_passent_la_validation(m, lecteur):
    """Chaque lecture passe la validation de la signature telle qu'elle est montée
    (annotation comprise), et figure au schéma servi."""
    assert set(OPS_LUES[lecteur]) <= set(_outil(m, lecteur).parameters["properties"]
                                         ["op"]["enum"])
    ann = typing.get_type_hints(_outil(m, lecteur).fn, include_extras=True)["op"]
    for op in OPS_LUES[lecteur]:
        assert TypeAdapter(ann).validate_python(op) == op


@pytest.mark.parametrize("lecteur", sorted(DECOUPAGE))
def test_un_lecteur_ne_sert_que_ses_lectures(m, lecteur):
    """Autoriser la lecture n'autorise plus l'envoi : le lecteur est `LECTURE` et son
    schéma ne porte aucune op de son écrivain — ni dépréciée, ni en transition."""
    t = _outil(m, lecteur)
    assert en_lecture(t), lecteur
    assert sorted(t.parameters["properties"]["op"]["enum"]) == sorted(OPS_LUES[lecteur])
    assert not {"text", "recipient_id", "message_id", "reaction", "action", "value",
                "provider_id", "invitation_id", "shared_secret", "message",
                "skill_endorsement_id", "api"} & set(t.parameters["properties"]), lecteur
    op = _outil(m, DECOUPAGE[lecteur][0]).parameters["properties"]["op"]
    assert set(DECOUPAGE[lecteur][1]) <= set(op.get("enum") or [op.get("const")])


@pytest.mark.parametrize("lecteur,op", [(l, o) for l, ops in sorted(ANCIENNES_OPS.items())
                                        for o in ops])
def test_l_ancienne_op_d_un_lecteur_refuse_en_nommant_l_acteur(m, client, lecteur, op):
    """Rien n'est fait, aucune clé n'est résolue, et le refus dit l'appel à faire."""
    with pytest.raises(Exception) as e:
        asyncio.run(m.call_tool(lecteur, {"op": op}))
    msg = error_taxonomy.classify(e.value).message
    assert '`linkedin_unipile_act(op="' in msg and "moved (21/10/2026" in msg, msg
    assert client.appels == []


@pytest.mark.parametrize("lecteur", sorted(DECOUPAGE))
def test_un_lecteur_n_envoie_jamais(m, client, lecteur):
    """`op="send"` sur le lecteur est une valeur refusée : rien n'est envoyé, aucune
    clé n'est résolue."""
    with pytest.raises(Exception):
        asyncio.run(m.call_tool(lecteur, {"op": "send", "text": "hello", "chat_id": "c1"}))
    assert client.appels == []
    client.send_message.assert_not_called()


@pytest.mark.parametrize("canal", CANAUX)
def test_l_ecrivain_d_un_canal_envoie_et_refuse_avant_toute_cle(m, client, canal):
    client.send_message.return_value = {"object": "send_message"}
    out = asyncio.run(m.call_tool(f"{canal}_act", {"op": "send", "text": "hello",
                                                   "chat_id": "c1"})).structured_content
    assert out == {"object": "send_message"}
    client.send_message.assert_called_once_with("hello", chat_id="c1", attendee_id=None)
    client.appels.clear()
    for args, manque in (({"text": "hello"}, "chat_id"), ({"chat_id": "c1"}, "text")):
        with pytest.raises(Exception) as e:
            asyncio.run(m.call_tool(f"{canal}_act", args))
        assert manque in error_taxonomy.classify(e.value).message
    assert client.appels == []


@pytest.mark.parametrize("op,args,appel", [
    ("publish_post", {"text": "t"}, ("create_post", ("t",), {})),
    ("comment_post", {"post_id": "p", "text": "t"}, ("comment_post", ("p", "t"), {})),
    ("react_post", {"post_id": "p"}, ("react_post", ("p",), {"value": "LIKE"})),
    ("react_post", {"post_id": "p", "value": "PRAISE"},
     ("react_post", ("p",), {"value": "PRAISE"})),
    ("react_message", {"message_id": "m", "reaction": "👍", "chat_id": "c"},
     ("react_message", ("m", "👍"), {"chat_id": "c"})),
    ("invite", {"provider_id": "u", "message": "hi"},
     ("send_invitation", ("u",), {"message": "hi"})),
    ("accept_invitation", {"invitation_id": "i", "shared_secret": "s"},
     ("handle_invitation", ("i", "s", "accept"), {})),
    ("decline_invitation", {"invitation_id": "i", "shared_secret": "s"},
     ("handle_invitation", ("i", "s", "decline"), {})),
    ("cancel_invitation", {"invitation_id": "i"}, ("cancel_invitation", ("i",), {})),
    ("endorse", {"identifier": "x", "skill_endorsement_id": 3},
     ("endorse_profile", ("x", 3), {})),
    ("update_member", {"identifier": "x", "api": "sales_navigator", "action": "saveLead"},
     ("member_action", ("x", "sales_navigator", "saveLead"),
      {"hiring_project_id": None, "stage": None, "list_id": None})),
])
def test_l_acteur_linkedin_porte_chaque_geste(m, client, op, args, appel):
    methode, pos, kw = appel
    getattr(client, methode).return_value = {"ok": True}
    asyncio.run(m.call_tool("linkedin_unipile_act", {"op": op, **args}))
    getattr(client, methode).assert_called_once_with(*pos, **kw)


@pytest.mark.parametrize("op,args,manque", [
    ("publish_post", {}, "text"),
    ("comment_post", {"text": "t"}, "post_id"),
    ("accept_invitation", {}, "invitation_id"),
    ("endorse", {"identifier": "x"}, "skill_endorsement_id"),
    ("update_member", {"identifier": "x", "api": "recruiter"}, "action"),
])
def test_l_acteur_linkedin_refuse_avant_toute_cle(m, client, op, args, manque):
    with pytest.raises(Exception) as e:
        asyncio.run(m.call_tool("linkedin_unipile_act", {"op": op, **args}))
    assert manque in error_taxonomy.classify(e.value).message
    assert client.appels == []
