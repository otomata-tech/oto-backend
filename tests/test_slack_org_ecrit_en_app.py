"""Slack — une connexion de l'ORG (ou d'une équipe) écrit TOUJOURS en tant que l'app.

« Add to Slack » au palier org range deux jetons : celui du bot, et le jeton
utilisateur de la personne qui a cliqué (`authed_user.access_token`). Ce second jeton
n'est celui de personne d'autre : un message posté avec lui part au nom de
l'installateur, même depuis le run d'un autre membre ; `slack_open_dm` ouvrait SON
DM, où l'app ne peut pas poster. Signalé le 15/11/2026.

Règle : palier org / équipe / tenant → le jeton utilisateur n'est jamais servi (ni
en écriture ni en lecture) ; `author="me"` est un refus nommé ; sans `author`, l'app
écrit. La connexion PERSONNELLE d'un membre garde le choix (`test_slack_auteur.py`).
"""
import asyncio
from unittest.mock import patch

import pytest

from oto_mcp import error_taxonomy
from oto_mcp.mcp_errors import McpError

from oto_mcp.tools import slack as slack_tools


# --- la règle, seule ----------------------------------------------------------

@pytest.mark.parametrize("author", [None, "app"])
def test_partage_avec_bot_ecrit_en_app_sans_rien_demander(author):
    assert slack_tools._auteur(True, True, author, partage=True) is False


def test_partage_refuse_author_me_et_dit_ou_se_connecter_soi_meme():
    with pytest.raises(McpError) as e:
        slack_tools._auteur(True, True, "me", partage=True)
    info = error_taxonomy.classify(e.value)
    assert info.code == "invalid_input"
    assert 'author="app"' in info.message and "your own Slack" in info.message


@pytest.mark.parametrize("author", [None, "me", "app"])
def test_partage_sans_bot_refuse_toute_ecriture(author):
    """Une vieille ligne org à jeton utilisateur seul : écrire, c'est parler au nom
    de quelqu'un — refus qui dit comment poser le bot, jamais un repli."""
    with pytest.raises(McpError) as e:
        slack_tools._auteur(False, True, author, partage=True)
    assert "only writes as the app" in str(e.value)


class _RC:
    def __init__(self, entity_type, is_platform=False):
        self.entity_type = entity_type
        self.is_platform = is_platform


@pytest.mark.parametrize("entity_type,attendu", [
    ("org", True), ("group", True), ("tenant", True),
    ("member", False), ("user", False), (None, False),
])
def test_qui_est_partage(entity_type, attendu):
    assert slack_tools._partage(_RC(entity_type)) is attendu


def test_une_cle_plateforme_garde_son_routage():
    assert slack_tools._partage(_RC(None, is_platform=True)) is False


# --- sur l'outil monté --------------------------------------------------------

def _monte(monkeypatch, bot, user, entity_type):
    class _Resolu:
        fields = {"bot_token": bot, "user_token": user}
        key = None
        is_platform = False

    _Resolu.entity_type = entity_type
    monkeypatch.setattr(slack_tools.access, "resolve_credential",
                        lambda *a, **k: _Resolu())
    from fastmcp import FastMCP
    patcher = patch("oto.tools.slack.client.SlackClient")
    cls = patcher.start()
    m = FastMCP("t")
    slack_tools.register(m)
    return m, cls, patcher


def _fn(m, name):
    return asyncio.run(m.get_tool(name)).fn


def _poste(m, cls, **kw):
    client = cls.return_value
    client.post_message.return_value = {"ok": True, "ts": "1.2"}
    client.channel_info.return_value = {"ok": True, "channel": {"id": "C1"}}
    return _fn(m, "slack_post_message")(channel="C1", text="bonjour", **kw)


@pytest.mark.parametrize("entity_type", ["org", "group"])
def test_poster_sur_une_connexion_org_part_en_app_sans_jeton_utilisateur(
        monkeypatch, entity_type):
    m, cls, patcher = _monte(monkeypatch, "xoxb-1", "xoxp-installateur", entity_type)
    try:
        out = _poste(m, cls)  # sans `author` : plus d'ambiguïté à trancher
    finally:
        patcher.stop()
    kw = cls.call_args.kwargs
    assert kw["default_as_user"] is False
    assert kw["user_token"] is None  # le jeton de l'installateur n'est même pas chargé
    assert out["_author"] == "app"


def test_poster_en_me_sur_une_connexion_org_est_refuse_avant_tout_envoi(monkeypatch):
    m, cls, patcher = _monte(monkeypatch, "xoxb-1", "xoxp-installateur", "org")
    try:
        with pytest.raises(McpError):
            _poste(m, cls, author="me")
        cls.return_value.post_message.assert_not_called()
    finally:
        patcher.stop()


def test_open_dm_sur_une_connexion_org_ouvre_le_dm_de_l_app(monkeypatch):
    """Le client ouvre le DM avec le jeton utilisateur s'il en a un : sans lui, c'est
    le DM de l'APP avec la personne — celui où `author="app"` peut poster."""
    m, cls, patcher = _monte(monkeypatch, "xoxb-1", "xoxp-installateur", "org")
    try:
        cls.return_value.open_dm.return_value = {"ok": True, "channel": {"id": "D1"}}
        _fn(m, "slack_open_dm")(user="U2")
    finally:
        patcher.stop()
    kw = cls.call_args.kwargs
    assert kw["user_token"] is None and kw["default_as_user"] is False


def test_une_connexion_perso_garde_le_choix_de_l_auteur(monkeypatch):
    m, cls, patcher = _monte(monkeypatch, "xoxb-1", "xoxp-moi", "member")
    try:
        out = _poste(m, cls, author="me")
    finally:
        patcher.stop()
    assert cls.call_args.kwargs["default_as_user"] is True
    assert out["_author"] == "me"


def test_vieille_ligne_org_a_jeton_utilisateur_seul_garde_ses_lectures(monkeypatch):
    m, cls, patcher = _monte(monkeypatch, None, "xoxp-ancien", "org")
    try:
        cls.return_value.list_channels.return_value = []
        _fn(m, "slack_list_channels")()
        assert cls.call_args.kwargs["user_token"] == "xoxp-ancien"
        with pytest.raises(McpError):
            _poste(m, cls)
        cls.return_value.post_message.assert_not_called()
    finally:
        patcher.stop()
