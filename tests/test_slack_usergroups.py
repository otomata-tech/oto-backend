"""Slack — les groupes d'utilisateurs (`@équipe`) : les lire, les changer.

Deux outils, jamais un `op` d'un lecteur : `slack_list_usergroups` (LECTURE) et
`slack_manage_usergroup` (ENVOI). Le contrat Slack encodé ici vient de sa
référence : `usergroups.users.update` REMPLACE toute la liste et refuse une liste
vide ; un jeton bot n'écrit que si l'espace laisse TOUT LE MONDE gérer les
groupes — l'auteur se choisit donc comme pour un message.
"""
import asyncio
from unittest.mock import patch

import pytest

from oto.tools.slack.client import SlackError

from oto_mcp import error_taxonomy
from oto_mcp.tools import slack as slack_tools
from oto_mcp.tools.lecture import en_lecture


def _monte(monkeypatch, bot="xoxb-1", user=None):
    class _RC:
        fields = {"bot_token": bot, "user_token": user}
        key = None
        is_platform = False

    monkeypatch.setattr(slack_tools.access, "resolve_credential", lambda *a, **k: _RC())
    from fastmcp import FastMCP
    patcher = patch("oto.tools.slack.client.SlackClient")
    cls = patcher.start()
    m = FastMCP("t")
    slack_tools.register(m)
    return m, cls, patcher


@pytest.fixture
def slack(monkeypatch):
    m, cls, patcher = _monte(monkeypatch)
    yield m, cls.return_value
    patcher.stop()


def _outil(m, name):
    return asyncio.run(m.get_tool(name))


def test_le_lecteur_lit_et_l_ecrivain_ecrit(slack):
    m, _ = slack
    assert en_lecture(_outil(m, "slack_list_usergroups"))
    assert not en_lecture(_outil(m, "slack_manage_usergroup"))


def test_lister_les_groupes_ou_les_membres_d_un(slack):
    m, client = slack
    client.list_usergroups.return_value = [{"id": "S1", "handle": "ventes"}]
    client.usergroup_users.return_value = ["U1", "U2"]
    fn = _outil(m, "slack_list_usergroups").fn

    assert fn() == {"usergroups": [{"id": "S1", "handle": "ventes"}]}
    assert client.list_usergroups.call_args.kwargs == {
        "include_users": False, "include_disabled": False, "include_count": True}
    assert fn(usergroup="S1") == {"usergroup": "S1", "users": ["U1", "U2"]}


def test_ajouter_lit_puis_remplace_et_rend_le_diff(slack):
    m, client = slack
    client.usergroup_users.return_value = ["U1", "U2"]
    out = _outil(m, "slack_manage_usergroup").fn(
        op="add_members", usergroup="S1", users=["U2", "U3"])
    client.set_usergroup_users.assert_called_once_with("S1", ["U1", "U2", "U3"])
    assert out["added"] == ["U3"] and out["removed"] == [] and out["changed"] is True
    assert out["_author"] == "app"


def test_retirer_et_dry_run_n_ecrit_rien(slack):
    m, client = slack
    client.usergroup_users.return_value = ["U1", "U2"]
    out = _outil(m, "slack_manage_usergroup").fn(
        op="remove_members", usergroup="S1", users=["U1"], dry_run=True)
    assert out["after"] == ["U2"] and out["removed"] == ["U1"] and out["dry_run"] is True
    client.set_usergroup_users.assert_not_called()


def test_rien_a_changer_pas_d_appel(slack):
    m, client = slack
    client.usergroup_users.return_value = ["U1"]
    out = _outil(m, "slack_manage_usergroup").fn(
        op="add_members", usergroup="S1", users=["U1"])
    assert out["changed"] is False
    client.set_usergroup_users.assert_not_called()


def test_vider_un_groupe_est_refuse_et_nomme_disable(slack):
    m, client = slack
    client.usergroup_users.return_value = ["U1"]
    with pytest.raises(Exception) as e:
        _outil(m, "slack_manage_usergroup").fn(
            op="remove_members", usergroup="S1", users=["U1"])
    info = error_taxonomy.classify(e.value)
    assert info.code == "invalid_input" and "disable" in info.message
    client.set_usergroup_users.assert_not_called()


def test_creer_puis_poser_les_membres(slack):
    m, client = slack
    client.create_usergroup.return_value = {"ok": True, "usergroup": {"id": "S9"}}
    client.set_usergroup_users.return_value = {"ok": True, "usergroup": {"id": "S9",
                                                                        "users": ["U1"]}}
    out = _outil(m, "slack_manage_usergroup").fn(
        op="create", name="Support", handle="@support", users=["U1"])
    assert client.create_usergroup.call_args.kwargs["handle"] == "support"
    client.set_usergroup_users.assert_called_once_with("S9", ["U1"])
    assert out["usergroup"]["users"] == ["U1"]


def test_creation_reussie_meme_si_les_membres_echouent(slack):
    """Le groupe EXISTE une fois créé : un échec sur les membres se nomme dans la
    réponse, il ne transforme pas la création en erreur."""
    m, client = slack
    client.create_usergroup.return_value = {"ok": True, "usergroup": {"id": "S9"}}
    client.set_usergroup_users.side_effect = SlackError("invalid_users")
    out = _outil(m, "slack_manage_usergroup").fn(op="create", name="Support", users=["B1"])
    assert out["usergroup"]["id"] == "S9" and "invalid_users" in out["members_error"]


def test_dry_run_de_mise_a_jour_rend_un_vrai_diff(slack):
    m, client = slack
    client.list_usergroups.return_value = [
        {"id": "S1", "name": "Ventes", "handle": "ventes", "description": "x",
         "prefs": {"channels": []}}]
    out = _outil(m, "slack_manage_usergroup").fn(
        op="update", usergroup="S1", name="Ventes", handle="sales", dry_run=True)
    assert out["changes"] == {"handle": {"from": "ventes", "to": "sales"}}
    client.update_usergroup.assert_not_called()


@pytest.mark.parametrize("kwargs", [
    {"op": "create"},
    {"op": "update", "usergroup": "S1"},
    {"op": "disable"},
    {"op": "set_members", "usergroup": "S1"},
])
def test_arguments_manquants_refuses_avant_tout_appel(slack, kwargs):
    m, client = slack
    with pytest.raises(Exception) as e:
        _outil(m, "slack_manage_usergroup").fn(**kwargs)
    assert error_taxonomy.classify(e.value).code == "invalid_input"
    assert not client.method_calls


def test_deux_jetons_l_auteur_est_exige_avant_toute_lecture(monkeypatch):
    m, cls, patcher = _monte(monkeypatch, bot="xoxb-1", user="xoxp-1")
    try:
        with pytest.raises(Exception):
            _outil(m, "slack_manage_usergroup").fn(
                op="add_members", usergroup="S1", users=["U1"], dry_run=True)
        cls.return_value.usergroup_users.assert_not_called()
        cls.return_value.usergroup_users.return_value = ["U1"]
        _outil(m, "slack_manage_usergroup").fn(
            op="add_members", usergroup="S1", users=["U2"], author="me")
        assert cls.call_args.kwargs["default_as_user"] is True
    finally:
        patcher.stop()


@pytest.mark.parametrize("code,attendu", [
    ("permission_denied", "everyone"),
    ("plan_upgrade_required", "paid"),
    ("no_such_subteam", "slack_list_usergroups"),
    ("handle_already_exists", "handle"),
])
def test_refus_slack_actionnables(slack, code, attendu):
    m, client = slack
    client.disable_usergroup.side_effect = SlackError(code)
    with pytest.raises(ValueError, match=attendu):
        _outil(m, "slack_manage_usergroup").fn(op="disable", usergroup="S1")
