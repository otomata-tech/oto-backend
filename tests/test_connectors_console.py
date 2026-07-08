"""Console connecteurs consolidée (ADR 0047 B1, *_op) : routage des op/scope vers
les handlers de domaine réutilisés + champs requis par op + autz déclarée par
(op, scope) via `BY_OP` + bascule des bindings MCP (console ON, origine OFF).

Même modèle que `test_admin_console.py` : on monkeypatch les handlers de domaine
pour vérifier le routage, jamais la logique métier (couverte par leurs tests).
"""
import asyncio

import pytest

from oto_mcp.capabilities import connectors_console as cc
from oto_mcp.capabilities import (
    connectors_account_grants,
    connectors_acl,
    connectors_activation,
    connectors_force,
    connectors_identities,
    connectors_instances,
    connectors_selection,
    connectors_sharing,
    connectors_verify,
)
from oto_mcp.capabilities._authz import BY_OP
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

CTX = ResolvedCtx(sub="alice", org_id=1)


def _tag(name):
    return lambda ctx, inp: {"called": name, "inp": inp}


def _atag(name):
    async def h(ctx, inp):
        return {"called": name, "inp": inp}
    return h


# ── oto_connector_activation ────────────────────────────────────────────────
def test_activation_routes(monkeypatch):
    for h in ("_org_list", "_org_set", "_org_clear", "_group_list", "_group_set", "_group_clear"):
        monkeypatch.setattr(connectors_activation, h, _tag(h))
    A = cc.ActivationInput
    assert cc._activation(CTX, A(op="list", org_id=1))["called"] == "_org_list"
    out = cc._activation(CTX, A(op="set", org_id=1, name="folk", enabled=True))
    assert out["called"] == "_org_set" and out["inp"].enabled is True
    assert cc._activation(CTX, A(op="clear", org_id=1, name="folk"))["called"] == "_org_clear"
    assert cc._activation(CTX, A(op="list", scope="group", group_id=3))["called"] == "_group_list"
    # enabled=False (couper) doit passer — le garde-fou anti-None ne confond pas False.
    out = cc._activation(CTX, A(op="set", scope="group", group_id=3, name="folk", enabled=False))
    assert out["called"] == "_group_set" and out["inp"].enabled is False
    assert cc._activation(CTX, A(op="clear", scope="group", group_id=3, name="folk"))["called"] == "_group_clear"


def test_activation_required_fields():
    A = cc.ActivationInput
    with pytest.raises(AuthzDenied) as e:
        cc._activation(CTX, A(op="list"))                       # scope=org sans org_id
    assert e.value.code == "missing_org"
    with pytest.raises(AuthzDenied) as e:
        cc._activation(CTX, A(op="set", org_id=1))              # pas de name
    assert e.value.code == "missing_name"
    with pytest.raises(AuthzDenied) as e:
        cc._activation(CTX, A(op="set", org_id=1, name="folk"))  # pas d'enabled
    assert e.value.code == "missing_enabled"
    with pytest.raises(AuthzDenied) as e:
        cc._activation(CTX, A(op="list", scope="group"))        # scope=group sans group_id
    assert e.value.code == "missing_group"


# ── oto_connector_access ────────────────────────────────────────────────────
def test_access_routes(monkeypatch):
    for h in ("_list_acl", "_grant", "_revoke", "_group_list_acl", "_group_grant", "_group_revoke"):
        monkeypatch.setattr(connectors_acl, h, _tag(h))
    A = cc.AccessInput
    assert cc._access(CTX, A(op="list", org_id=1))["called"] == "_list_acl"
    out = cc._access(CTX, A(op="grant", org_id=1, connector="folk",
                            principal_type="group", principal_id="7"))
    assert out["called"] == "_grant" and out["inp"].principal_id == "7"
    assert cc._access(CTX, A(op="revoke", org_id=1, connector="folk",
                             principal_type="user", principal_id="s"))["called"] == "_revoke"
    assert cc._access(CTX, A(op="list", scope="group", group_id=3))["called"] == "_group_list_acl"
    out = cc._access(CTX, A(op="grant", scope="group", group_id=3, connector="folk", member="s"))
    assert out["called"] == "_group_grant" and out["inp"].member == "s"
    assert cc._access(CTX, A(op="revoke", scope="group", group_id=3, connector="folk",
                             member="s"))["called"] == "_group_revoke"


def test_access_required_fields():
    A = cc.AccessInput
    with pytest.raises(AuthzDenied) as e:
        cc._access(CTX, A(op="grant", org_id=1))
    assert e.value.code == "missing_connector"
    with pytest.raises(AuthzDenied) as e:
        cc._access(CTX, A(op="grant", org_id=1, connector="folk"))
    assert e.value.code == "missing_principal"
    with pytest.raises(AuthzDenied) as e:
        cc._access(CTX, A(op="grant", scope="group", group_id=3, connector="folk"))
    assert e.value.code == "missing_member"


# ── oto_connector ───────────────────────────────────────────────────────────
def test_connector_routes(monkeypatch):
    for h in ("_me", "_select", "_pause", "_unselect", "_recommend"):
        monkeypatch.setattr(connectors_selection, h, _tag(h))
    monkeypatch.setattr(connectors_force, "_force_connector", _atag("_force"))
    C = cc.ConnectorInput
    run = lambda inp: asyncio.run(cc._connector(CTX, inp))
    out = run(C(op="list", verbose=True, state="active"))
    assert out["called"] == "_me" and out["inp"].verbose is True and out["inp"].state == "active"
    assert run(C(op="select", name="folk"))["called"] == "_select"
    assert run(C(op="pause", name="folk"))["called"] == "_pause"
    assert run(C(op="unselect", name="folk"))["called"] == "_unselect"
    out = run(C(op="force", org_id=1, name="folk", member="bob"))
    assert out["called"] == "_force" and out["inp"].member == "bob"
    out = run(C(op="recommend", org_id=1, connectors=[]))     # [] efface = valide
    assert out["called"] == "_recommend" and out["inp"].connectors == []


def test_connector_required_fields():
    C = cc.ConnectorInput
    run = lambda inp: asyncio.run(cc._connector(CTX, inp))
    with pytest.raises(AuthzDenied) as e:
        run(C(op="select"))
    assert e.value.code == "missing_name"
    with pytest.raises(AuthzDenied) as e:
        run(C(op="force", org_id=1, name="folk"))
    assert e.value.code == "missing_member"
    with pytest.raises(AuthzDenied) as e:
        run(C(op="recommend", org_id=1))                      # connectors absent ≠ []
    assert e.value.code == "missing_connectors"


# ── oto_instance ────────────────────────────────────────────────────────────
def test_instance_routes(monkeypatch):
    monkeypatch.setattr(connectors_instances, "_list_instances", _tag("_list"))
    monkeypatch.setattr(connectors_sharing, "_lend_instance", _tag("_lend"))
    monkeypatch.setattr(connectors_verify, "_verify", _atag("_verify"))
    I = cc.InstanceInput
    run = lambda inp: asyncio.run(cc._instance(CTX, inp))
    out = run(I(op="list", connector="folk", level="org"))
    assert out["called"] == "_list" and out["inp"].level == "org"
    out = run(I(op="lend", connector="folk", to="bob", revoke=True))
    assert out["called"] == "_lend" and out["inp"].revoke is True
    out = run(I(op="verify", connector="folk"))               # level défaut = auto
    assert out["called"] == "_verify" and out["inp"].level == "auto"
    assert out["inp"].provider == "folk"                      # mapping connector→provider


def test_instance_level_validated_per_op():
    I = cc.InstanceInput
    run = lambda inp: asyncio.run(cc._instance(CTX, inp))
    with pytest.raises(AuthzDenied) as e:
        run(I(op="list", level="auto"))                       # vocabulaire de verify
    assert e.value.code == "invalid_level"
    with pytest.raises(AuthzDenied) as e:
        run(I(op="verify", connector="folk", level="member"))  # vocabulaire de list
    assert e.value.code == "invalid_level"
    with pytest.raises(AuthzDenied) as e:
        run(I(op="lend", connector="folk"))
    assert e.value.code == "missing_to"


# ── oto_identity ────────────────────────────────────────────────────────────
def test_identity_routes(monkeypatch):
    monkeypatch.setattr(connectors_identities, "_list", _atag("_list"))
    monkeypatch.setattr(connectors_identities, "_set_default", _atag("_set"))
    run = lambda inp: asyncio.run(cc._identity(CTX, inp))
    assert run(cc.IdentityInput(op="list", connector="google"))["called"] == "_list"
    out = run(cc.IdentityInput(op="set", connector="google", identity_id="acc1"))
    assert out["called"] == "_set" and out["inp"].identity_id == "acc1"
    with pytest.raises(AuthzDenied) as e:
        run(cc.IdentityInput(op="set", connector="google"))
    assert e.value.code == "missing_identity"


# ── oto_account_access ──────────────────────────────────────────────────────
def test_account_access_routes(monkeypatch):
    monkeypatch.setattr(connectors_account_grants, "_list", _tag("_list"))
    monkeypatch.setattr(connectors_account_grants, "_grant", _tag("_grant"))
    monkeypatch.setattr(connectors_account_grants, "_revoke", _tag("_revoke"))
    A = cc.AccountAccessInput
    assert cc._account_access(CTX, A(op="list"))["called"] == "_list"
    out = cc._account_access(CTX, A(op="grant", channel="linkedin", grantee="b@c.co"))
    assert out["called"] == "_grant" and out["inp"].channel == "linkedin"
    assert cc._account_access(CTX, A(op="revoke", channel="linkedin",
                                     grantee="b@c.co"))["called"] == "_revoke"
    with pytest.raises(AuthzDenied) as e:
        cc._account_access(CTX, A(op="grant", grantee="b@c.co"))
    assert e.value.code == "missing_channel"


# ── BY_OP : dispatch (op, scope) + clé inconnue = refus net ─────────────────
def test_by_op_tuple_dispatch():
    from oto_mcp.capabilities._types import RawCtx
    hits = []

    def rule_a(raw, inp):
        hits.append("a")
        return CTX

    def rule_b(raw, inp):
        hits.append("b")
        return CTX

    rule = BY_OP({("list", "org"): rule_a, ("set", "group"): rule_b},
                 fields=("op", "scope"))
    raw = RawCtx(sub="alice")
    rule(raw, cc.ActivationInput(op="list", org_id=1))
    rule(raw, cc.ActivationInput(op="set", scope="group", group_id=3, name="x", enabled=False))
    assert hits == ["a", "b"]
    with pytest.raises(AuthzDenied) as e:
        rule(raw, cc.ActivationInput(op="clear", org_id=1))   # (clear, org) hors map
    assert e.value.code == "unsupported_op"


# ── Bascule des bindings MCP (ADR 0047 B1) ──────────────────────────────────
def test_console_carries_the_mcp_surface():
    from oto_mcp.capabilities.registry import CAPABILITIES
    caps = {c.key: c for c in CAPABILITIES}
    expected = {
        "connectors.console.activation": "oto_connector_activation",
        "connectors.console.access": "oto_connector_access",
        "connectors.console.connector": "oto_connector",
        "connectors.console.instance": "oto_instance",
        "connectors.console.identity": "oto_identity",
        "connectors.console.account_access": "oto_account_access",
    }
    for key, mcp in expected.items():
        assert caps[key].mcp == mcp, key
    # Les capacités d'origine ne portent PLUS de binding MCP (REST intact).
    demoted = [
        "connectors.activation.org_list", "connectors.activation.set_org",
        "connectors.activation.clear_org", "connectors.activation.group_list",
        "connectors.activation.set_group", "connectors.activation.clear_group",
        "connectors.acl.list", "connectors.acl.grant", "connectors.acl.revoke",
        "connectors.acl.group_list", "connectors.acl.group_grant", "connectors.acl.group_revoke",
        "connectors.me", "connectors.select", "connectors.pause", "connectors.unselect",
        "connectors.recommend", "connectors.force.member", "connectors.instances.list",
        "connectors.identities", "connectors.set_default_identity",
        "connectors.lend_instance", "connectors.verify",
        "connectors.account_grants.list", "connectors.account_grants.grant",
        "connectors.account_grants.revoke",
    ]
    for key in demoted:
        assert caps[key].mcp is None, key
        assert caps[key].rest_bindings(), key                 # la face REST reste
