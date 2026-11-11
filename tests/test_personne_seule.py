"""Les comptes et l'org sont des gestes de PERSONNE : sous un jeton de délégation (un agent
hébergé), `accounts_human_only` et `org_human_only` (`capabilities/_personne_seule.py`).

Les agents (`agents_human_only`, `chain_widens_tools`) ont leurs bancs dans
`test_runner_triggers.py` et `test_chaine_outils_live.py`, les campagnes dans
`test_chaine_outils_live.py`. Ici, la console des connecteurs et celle de l'org : chaque
geste réservé est refusé sous le jeton, passe à une personne (le handler de domaine est
doublé : il a ses bancs), et les lectures restent ouvertes à un agent."""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp import verrou_org
from oto_mcp.capabilities import org_console as O
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.connectors import console as C

CTX = ResolvedCtx(sub="porteur", org_id=2)


@pytest.fixture
def agent_heberge():
    jeton = verrou_org.poser(verrou_org.Verrou(sub="porteur", org_id=2, job_id=9))
    yield
    verrou_org.lever(jeton)


def _jouer(rendu):
    return asyncio.run(rendu) if asyncio.iscoroutine(rendu) else rendu


def _servi(nom):
    return lambda *a, **k: {"servi": nom}


async def _servi_async(*a, **k):
    return {"servi": "async"}


# ── Les comptes et les connecteurs ────────────────────────────────────────────

@pytest.fixture
def comptes(monkeypatch):
    """Les handlers de domaine de la console des connecteurs, doublés."""
    monkeypatch.setattr(C.connectors_sharing, "_lend_instance", _servi("lend"))
    monkeypatch.setattr(C.connectors_account_grants, "_grant", _servi("grant"))
    monkeypatch.setattr(C.connectors_account_grants, "_revoke", _servi("revoke"))
    monkeypatch.setattr(C.connectors_account_grants, "_list", _servi("grants"))
    for geste in ("_select", "_pause", "_unselect", "_recommend", "_me"):
        monkeypatch.setattr(C.connectors_selection, geste, _servi(geste))
    monkeypatch.setattr(C.connectors_force, "_force_connector", _servi_async)
    monkeypatch.setattr(C.connectors_activation, "_org_set", _servi("activation"))
    monkeypatch.setattr(C.connectors_access_level, "_org_set", _servi("level"))


_GESTES_SUR_LES_COMPTES = [
    pytest.param(lambda: C._instance(CTX, C.InstanceInput(op="lend", connector="gmail",
                                                          to_org=True)), id="instance-lend"),
    pytest.param(lambda: C._instance(CTX, C.InstanceInput(
        op="lend", connector="gmail", to="pair@acme.test", revoke=True)),
        id="instance-lend-revoke"),
    pytest.param(lambda: C._account_access(CTX, C.AccountAccessInput(
        op="grant", channel="linkedin", grantee="tiers@ailleurs.test")), id="account-grant"),
    pytest.param(lambda: C._account_access(CTX, C.AccountAccessInput(
        op="revoke", channel="linkedin", grantee="tiers@ailleurs.test")), id="account-revoke"),
    pytest.param(lambda: C._connector(CTX, C.ConnectorInput(op="select", name="slack")),
                 id="connector-select"),
    pytest.param(lambda: C._connector(CTX, C.ConnectorInput(op="unselect", name="slack")),
                 id="connector-unselect"),
    pytest.param(lambda: C._connector(CTX, C.ConnectorInput(
        op="force", name="slack", org_id=2, member="pair@acme.test")), id="connector-force"),
    pytest.param(lambda: C._connector(CTX, C.ConnectorInput(
        op="recommend", org_id=2, connectors=["slack"])), id="connector-recommend"),
    pytest.param(lambda: C._activation(CTX, C.ActivationInput(
        op="set", org_id=2, name="slack", enabled=True)), id="activation-set"),
    pytest.param(lambda: C._access_level(CTX, C.AccessLevelInput(
        op="set", org_id=2, name="zoho", level="full")), id="access-level-set"),
]


@pytest.mark.parametrize("geste", _GESTES_SUR_LES_COMPTES)
def test_un_agent_heberge_ne_prete_n_ouvre_ni_n_installe_aucun_compte(comptes, agent_heberge,
                                                                     geste):
    with pytest.raises(AuthzDenied) as e:
        _jouer(geste())
    assert (e.value.status, e.value.code) == (403, "accounts_human_only")


@pytest.mark.parametrize("geste", _GESTES_SUR_LES_COMPTES)
def test_une_personne_fait_ces_gestes_sur_ses_comptes(comptes, geste):
    assert "servi" in _jouer(geste())


@pytest.mark.parametrize("geste", [
    lambda: C._account_access(CTX, C.AccountAccessInput(op="list")),
    lambda: C._connector(CTX, C.ConnectorInput(op="list")),
])
def test_un_agent_heberge_lit_encore_ses_comptes(comptes, agent_heberge, geste):
    assert "servi" in _jouer(geste())


# ── L'org et ses équipes ──────────────────────────────────────────────────────

@pytest.fixture
def org(monkeypatch):
    """Les handlers de domaine de la console d'org, doublés."""
    monkeypatch.setattr(O.orgs_invites, "_invite_create", _servi("invite"))
    monkeypatch.setattr(O.orgs_invites, "_invite_accept", _servi("accept"))
    monkeypatch.setattr(O.orgs_invites, "_invite_list", _servi("invites"))
    monkeypatch.setattr(O.orgs_update, "_update_org", _servi("update"))
    monkeypatch.setattr(O.orgs, "_create_org", _servi("create"))
    monkeypatch.setattr(O.orgs_mfa, "_set_org_mfa", _servi("mfa"))
    monkeypatch.setattr(O.orgs_model_subscriptions, "_set_mode", _servi("pool"))
    monkeypatch.setattr(O.groups_members, "_add_member", _servi("add_member"))
    monkeypatch.setattr(O.groups_invites, "_invite_create", _servi("group_invite"))
    monkeypatch.setattr(O.groups, "_list_my_groups", _servi("groups"))


_GESTES_SUR_L_ORG = [
    pytest.param(lambda: O._org(CTX, O.OrgInput(op="invite", org_id=2, role="org_admin",
                                                send_email=False)), id="org-invite-admin"),
    pytest.param(lambda: O._org(CTX, O.OrgInput(op="accept_invite", token="t")),
                 id="org-accept-invite"),
    pytest.param(lambda: O._org(CTX, O.OrgInput(op="update", org_id=2, name="x")),
                 id="org-update"),
    pytest.param(lambda: O._org(CTX, O.OrgInput(op="create", name="x")), id="org-create"),
    pytest.param(lambda: O._org_settings(CTX, O.OrgSettingsInput(
        op="set", domain="mfa", org_id=2, require=False)), id="settings-mfa"),
    pytest.param(lambda: O._org_settings(CTX, O.OrgSettingsInput(
        op="set", domain="model_subscriptions", org_id=2, family="claude_subscription",
        mode="pool")), id="settings-pool"),
    pytest.param(lambda: O._group(CTX, O.GroupInput(op="add_member", group_id=4,
                                                    target="pair@acme.test")),
                 id="group-add-member"),
    pytest.param(lambda: O._group(CTX, O.GroupInput(op="invite", group_id=4,
                                                    send_email=False)), id="group-invite"),
]


@pytest.mark.parametrize("geste", _GESTES_SUR_L_ORG)
def test_un_agent_heberge_ne_gere_pas_l_org(org, agent_heberge, geste):
    with pytest.raises(AuthzDenied) as e:
        geste()
    assert (e.value.status, e.value.code) == (403, "org_human_only")


@pytest.mark.parametrize("geste", _GESTES_SUR_L_ORG)
def test_une_personne_gere_son_org(org, geste):
    assert "servi" in geste()


@pytest.mark.parametrize("geste", [
    lambda: O._org(CTX, O.OrgInput(op="list_invites", org_id=2)),
    lambda: O._group(CTX, O.GroupInput(op="list")),
])
def test_un_agent_heberge_lit_encore_l_org(org, agent_heberge, geste):
    assert "servi" in geste()


# ── Les refus sont déclarés ───────────────────────────────────────────────────

@pytest.mark.parametrize("cle, code", [
    ("connectors.console.activation", "accounts_human_only"),
    ("connectors.console.access_level", "accounts_human_only"),
    ("connectors.console.connector", "accounts_human_only"),
    ("connectors.console.instance", "accounts_human_only"),
    ("connectors.console.account_access", "accounts_human_only"),
    ("org.console", "org_human_only"),
    ("org.settings.console", "org_human_only"),
    ("group.console", "org_human_only"),
    ("runner.triggers", "agents_human_only"),
    ("runner.triggers", "chain_widens_tools"),
    ("runner.fleets", "not_from_a_run"),
    ("runner.fleets", "tools_human_only"),
])
def test_chaque_capacite_declare_son_refus(cle, code):
    from oto_mcp.capabilities import registry
    [cap] = [c for c in registry.CAPABILITIES if c.key == cle]
    assert any(e.code == code and e.status == 403 for e in cap.errors), (cle, code)
