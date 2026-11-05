"""Tools `teams_*` du connecteur Teams (service du porteur `microsoft`).

Ce que ce fichier verrouille :
- la SURFACE (la paire `teams` / `teams_act` : lire et écrire sont deux outils, #1231,
  21/10/2026), le jeton du service `teams`, le refus nommé d'un compte qui
  ne l'a pas autorisé ;
- le dispatch vers les VRAIES méthodes de `TeamsClient` (autospec), le lieu : un canal
  (`team_id` + `channel_id`) OU une conversation (`chat_id`), jamais les deux ;
- le corps markdown → HTML, la vue resserrée (texte du message), le brut sur `full` ;
- ⚠️ le PALIER ADMIN : lire les messages d'un canal exige `scopes.TEAMS_ADMIN`. Sans
  lui : un renouvellement forcé (l'approbation n'est vue qu'au renouvellement), puis
  refus qui dit « requires your Microsoft admin's approval » et rend le lien
  d'approbation du porteur (services=["teams"], annuaire du compte) ; avec lui mais un
  403 de Graph (jeton d'avant l'approbation) : UN renouvellement forcé, puis une seule
  nouvelle tentative — jamais de boucle. Les conversations et la publication n'en
  ont pas besoin.
"""
import asyncio
from unittest.mock import create_autospec

import pytest
from oto_mcp.mcp_errors import McpError

ADMIN = ("ChannelMessage.Read.All Team.ReadBasic.All Channel.ReadBasic.All "
         "ChannelMessage.Send Chat.ReadWrite User.Read offline_access")
SANS_ADMIN = ("Team.ReadBasic.All Channel.ReadBasic.All ChannelMessage.Send Chat.ReadWrite "
              "User.Read offline_access")


@pytest.fixture
def env(monkeypatch):
    """Le faux `TeamsClient` (autospec), les jetons demandés (`renew` compris), le meta
    du compte que l'appel désigne — que le renouvellement peut faire évoluer, comme le
    fait `access_token_for` en relisant `grant.scope`."""
    import types

    import oto.tools.microsoft as coeur

    inst = create_autospec(coeur.TeamsClient, instance=True)
    etat = types.SimpleNamespace(client=inst, jetons=[], meta={"scopes": SANS_ADMIN},
                                 apres_renouvellement=None, liens=[])
    monkeypatch.setattr(coeur, "TeamsClient", lambda jeton: etat.jetons.append(jeton) or inst)
    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: "sub-1")

    def jeton(sub, service, renew=False):
        assert service == "teams"
        if renew and etat.apres_renouvellement is not None:
            etat.meta = {"scopes": etat.apres_renouvellement, **{
                k: v for k, v in etat.meta.items() if k != "scopes"}}
        return "AT-neuf" if renew else "AT-cache"

    def compte(sub, service, account=None):
        assert service == "teams"
        return object(), dict(etat.meta)

    def lien(sub, services, tenant=None, return_app="", connector=None):
        etat.liens.append((services, tenant, connector))
        return "https://login.example/adminconsent?x=1"

    monkeypatch.setattr("oto_mcp.auth.microsoft.access_token_for", jeton)
    monkeypatch.setattr("oto_mcp.auth.microsoft.resolve_account", compte)
    monkeypatch.setattr("oto_mcp.auth.microsoft.admin_consent_url", lien)
    return etat


def _mcp():
    from fastmcp import FastMCP
    from oto_mcp.tools import teams as X

    m = FastMCP("t")
    X.register(m)
    return m


def _tool(name):
    return asyncio.run(_mcp().get_tool(name)).fn


def _upstream(status):
    from oto.tools.common.errors import UpstreamHTTPError
    return UpstreamHTTPError(status, {"error": {"message": "Forbidden"}}, service="microsoft")


MSG = {"id": "1700", "from": {"user": {"displayName": "Jane"}},
       "createdDateTime": "2026-10-08T09:00:00Z",
       "body": {"contentType": "html", "content": "<p>Bonjour &amp; <b>merci</b></p>"},
       "attachments": [{}], "reactions": ["lourd"]}


# --- surface, jeton, lieux ------------------------------------------------------

def test_surface(env):
    assert {t.name for t in asyncio.run(_mcp().list_tools())} == {
        "teams", "teams_act"}


def test_un_compte_sans_teams_recoit_le_refus_du_porteur(env, monkeypatch):
    def refus(sub, service, renew=False):
        raise RuntimeError("The Microsoft account j@x has not yet authorized Teams")

    monkeypatch.setattr("oto_mcp.auth.microsoft.access_token_for", refus)
    with pytest.raises(McpError, match="has not yet authorized Teams"):
        _tool("teams")()
    assert env.jetons == []


def test_espaces(env):
    c = env.client
    c.list_joined_teams.return_value = [{"id": "t1", "displayName": "Ventes", "x": 1}]
    assert _tool("teams")()["teams"] == [{"id": "t1", "displayName": "Ventes",
                                                 "description": None}]
    c.list_channels.return_value = [{"id": "19:a@thread.tacv2", "displayName": "Général"}]
    out = _tool("teams")(op="channels", team_id="t1")
    c.list_channels.assert_called_once_with("t1")
    assert out["channels"][0]["id"] == "19:a@thread.tacv2"
    c.list_chats.return_value = []
    _tool("teams")(op="chats", limit=7)
    c.list_chats.assert_called_once_with(limit=7)
    with pytest.raises(McpError, match="requires `team_id`"):
        _tool("teams")(op="channels")


@pytest.mark.parametrize("lieu", [{}, {"team_id": "t1", "chat_id": "c1"},
                                  {"team_id": "t1", "channel_id": "ch", "chat_id": "c1"}])
def test_un_lieu_et_un_seul(env, lieu):
    with pytest.raises(McpError, match="exactly one"):
        _tool("teams")(op="list", **lieu)


def test_un_canal_a_besoin_de_son_equipe(env):
    with pytest.raises(McpError, match="requires `team_id`"):
        _tool("teams")(op="list", channel_id="ch")


# --- conversations et publication : sans palier admin ----------------------------

def test_lire_une_conversation_sans_palier_admin(env):
    env.client.list_chat_messages.return_value = [dict(MSG)]
    out = _tool("teams")(op="list", chat_id="19:c@unq.gbl.spaces", limit=5)
    env.client.list_chat_messages.assert_called_once_with("19:c@unq.gbl.spaces", limit=5)
    m = out["messages"][0]
    assert m["text"] == "Bonjour & merci" and m["from"] == "Jane" and m["attachments"] == 1
    assert "reactions" not in m
    assert _tool("teams")(op="list", chat_id="c", full=True)["messages"] == [MSG]
    assert env.jetons == ["AT-cache", "AT-cache"] and env.liens == []


def test_publier_dans_un_canal_et_une_conversation(env):
    c = env.client
    c.post_channel_message.return_value = {"id": "m1"}
    _tool("teams_act")(team_id="t1", channel_id="ch", body="**Point** à 14 h")
    args, kw = c.post_channel_message.call_args
    assert args == ("t1", "ch") and "<strong>Point</strong>" in kw["html"]
    c.post_chat_message.return_value = {"id": "m2"}
    _tool("teams_act")(op="post", chat_id="c1", body="ok")
    assert c.post_chat_message.call_args.args == ("c1",)
    c.reply_channel_message.return_value = {"id": "m3"}
    _tool("teams_act")(op="reply", team_id="t1", channel_id="ch", message_id="1700",
                        body="vu")
    assert c.reply_channel_message.call_args.args == ("t1", "ch", "1700")
    assert env.liens == [], "publier n'a pas besoin du palier admin"


def test_une_conversation_n_a_pas_de_fil(env):
    with pytest.raises(McpError, match="no threads"):
        _tool("teams")(op="replies", chat_id="c1", message_id="1")
    with pytest.raises(McpError, match="no threads"):
        _tool("teams_act")(op="reply", chat_id="c1", message_id="1", body="x")
    with pytest.raises(McpError, match="requires `body`"):
        _tool("teams_act")(op="post", chat_id="c1")
    assert env.jetons == [], "un envoi refusé ne résout aucun jeton"


@pytest.mark.parametrize("lieu", [{}, {"team_id": "t1", "chat_id": "c1"},
                                  {"channel_id": "ch"}])
def test_l_ecrivain_refuse_un_lieu_ambigu_avant_tout_jeton(env, lieu):
    with pytest.raises(McpError, match="exactly one|requires `team_id`"):
        _tool("teams_act")(body="x", **lieu)
    assert env.jetons == []


def test_une_op_inconnue_du_lecteur_nomme_l_ecrivain(env):
    """Une op inconnue du lecteur — même `send` — est refusée en nommant l'outil qui
    agit, avant tout jeton : jamais un envoi."""
    with pytest.raises(McpError, match="teams_act"):
        _tool("teams")(op="send", chat_id="c1")
    assert env.jetons == []


# --- le palier admin ------------------------------------------------------------

def test_lire_un_canal_sans_palier_admin_refus_avec_le_lien(env):
    env.meta = {"scopes": SANS_ADMIN, "tenant": "contoso.onmicrosoft.com"}
    with pytest.raises(McpError) as e:
        _tool("teams")(op="list", team_id="t1", channel_id="ch")
    assert "requires your Microsoft admin's approval" in str(e.value)
    assert "https://login.example/adminconsent?x=1" in str(e.value)
    assert env.liens == [(("teams",), "contoso.onmicrosoft.com", "teams")]
    assert env.jetons == ["AT-neuf"], "UN renouvellement forcé avant de refuser"
    env.client.list_channel_messages.assert_not_called()
    with pytest.raises(McpError, match="admin's approval"):
        _tool("teams")(op="replies", team_id="t1", channel_id="ch",
                               message_id="1700")
    env.client.list_replies.assert_not_called()


def test_une_approbation_donnee_depuis_est_vue_au_renouvellement(env):
    env.apres_renouvellement = ADMIN
    env.client.list_channel_messages.return_value = [dict(MSG)]
    out = _tool("teams")(op="list", team_id="t1", channel_id="ch", limit=3)
    env.client.list_channel_messages.assert_called_once_with("t1", "ch", limit=3)
    assert env.jetons == ["AT-neuf"] and env.liens == [] and out["count"] == 1


def test_palier_accorde_jeton_d_avant_l_approbation_renouvele_une_fois(env):
    env.meta = {"scopes": ADMIN}
    env.client.list_replies.side_effect = [_upstream(403), [dict(MSG)]]
    out = _tool("teams")(op="replies", team_id="t1", channel_id="ch",
                                 message_id="1700")
    assert env.jetons == ["AT-cache", "AT-neuf"] and out["count"] == 1
    assert env.client.list_replies.call_count == 2


def test_jamais_plus_d_un_renouvellement(env):
    env.meta = {"scopes": ADMIN}
    env.client.list_channel_messages.side_effect = _upstream(403)
    with pytest.raises(McpError, match="HTTP 403"):
        _tool("teams")(op="list", team_id="t1", channel_id="ch")
    assert env.jetons == ["AT-cache", "AT-neuf"]
    assert env.client.list_channel_messages.call_count == 2
    # Déjà renouvelé pour lire le palier : un 403 ensuite ne renouvelle plus.
    env.meta, env.apres_renouvellement, env.jetons[:] = {"scopes": SANS_ADMIN}, ADMIN, []
    env.client.list_channel_messages.reset_mock()
    env.client.list_channel_messages.side_effect = _upstream(403)
    with pytest.raises(McpError, match="HTTP 403"):
        _tool("teams")(op="list", team_id="t1", channel_id="ch")
    assert env.jetons == ["AT-neuf"] and env.client.list_channel_messages.call_count == 1


def test_un_autre_refus_n_est_pas_un_renouvellement(env):
    env.meta = {"scopes": ADMIN}
    env.client.list_channel_messages.side_effect = _upstream(404)
    with pytest.raises(McpError, match="not found"):
        _tool("teams")(op="list", team_id="t1", channel_id="ch")
    assert env.jetons == ["AT-cache"]


def test_les_ops_de_lieu_refusent_les_arguments_d_un_message(env):
    """`teams(op="teams"|"channels"|"chats")` ne prend ni canal, ni conversation, ni
    message : une erreur d'intention, refusée avant tout jeton."""
    for op, extra in (("teams", {"chat_id": "c1"}), ("channels", {"team_id": "t1",
                                                                  "channel_id": "ch"}),
                      ("chats", {"message_id": "1"}), ("teams", {"team_id": "t1"})):
        with pytest.raises(McpError, match="does not use"):
            _tool("teams")(op=op, **extra)
    assert env.jetons == []


# --- images collées : listées par le message, vues par op=image ----------------------

IMG_CHAT = "/chats/19:x@unq.gbl.spaces/messages/1700/hostedContents/aWQ9/$value"
IMG_CANAL = "/teams/t1/channels/19:c@thread.tacv2/messages/1700/hostedContents/aWQ9/$value"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def test_un_message_nomme_ses_images_collees(env):
    corps = {"contentType": "html",
             "content": f'<p>Le blocage :</p><img src="https://graph.microsoft.com/v1.0{IMG_CHAT}">'}
    env.client.list_chat_messages.return_value = [{**MSG, "body": corps}, MSG]
    out = _tool("teams")(op="list", chat_id="19:x@unq.gbl.spaces")
    assert out["messages"][0]["images"] == [IMG_CHAT]
    assert out["messages"][0]["text"] == "Le blocage :"
    assert "images" not in out["messages"][1]


def test_une_image_de_conversation_se_voit(env):
    from mcp.types import ImageContent

    env.client.get_hosted_content.return_value = PNG
    out = _tool("teams")(op="image", image=IMG_CHAT)
    env.client.get_hosted_content.assert_called_once_with(IMG_CHAT)
    texte, image = out.content
    assert "png" in texte.text and isinstance(image, ImageContent)
    assert image.mimeType == "image/png"


def test_une_image_de_canal_passe_par_le_palier_admin(env):
    env.client.get_hosted_content.return_value = PNG
    with pytest.raises(McpError, match="admin's approval"):
        _tool("teams")(op="image", image=IMG_CANAL)
    env.client.get_hosted_content.assert_not_called()
    env.meta = {"scopes": ADMIN}
    _tool("teams")(op="image", image=IMG_CANAL)
    env.client.get_hosted_content.assert_called_once_with(IMG_CANAL)


def test_une_grosse_image_part_en_url(env, monkeypatch):
    env.client.get_hosted_content.return_value = PNG + b"0" * (3 * 1024 * 1024)
    vus = []
    monkeypatch.setattr("oto_mcp.file_content.render_for_agent",
                        lambda data, filename, mime, **kw: vus.append((filename, mime, kw))
                        or {"encoding": "url", "url": "https://s3.example/x"})
    out = _tool("teams")(op="image", image=IMG_CHAT)
    assert out["image"]["url"] == "https://s3.example/x" and "3 MB" in out["note"]
    assert vus[0][:2] == ("teams-image.png", "image/png") and vus[0][2]["prefix"] == "teams-images"


@pytest.mark.parametrize("kw", [{"chat_id": "c"}, {"message_id": "m"}, {"full": True}])
def test_op_image_refuse_le_reste(env, kw):
    with pytest.raises(McpError):
        _tool("teams")(op="image", image=IMG_CHAT, **kw)


def test_image_hors_op_image_refusee(env):
    with pytest.raises(McpError, match="image"):
        _tool("teams")(op="chats", image=IMG_CHAT)
