"""Console connecteurs consolidée (ADR 0047 B1, *_op) : routage des op/scope vers
les handlers de domaine réutilisés + champs requis par op + autz déclarée par
(op, scope) via `BY_OP` + bascule des bindings MCP (console ON, origine OFF).

Même modèle que `test_admin_console.py` : on monkeypatch les handlers de domaine
pour vérifier le routage, jamais la logique métier (couverte par leurs tests).
"""
import asyncio

import pytest

from oto_mcp.capabilities.connectors import console as cc
from oto_mcp.capabilities.connectors import activation as connectors_activation, force as connectors_force, identities as connectors_identities, instances as connectors_instances, selection as connectors_selection, sharing as connectors_sharing, transfert as connectors_transfert, verify as connectors_verify
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


# ── oto_connector ───────────────────────────────────────────────────────────
def test_connector_routes(monkeypatch):
    for h in ("_me", "_select", "_pause", "_unselect", "_recommend"):
        monkeypatch.setattr(connectors_selection, h, _tag(h))
    monkeypatch.setattr(connectors_force, "_force_connector", _atag("_force"))
    C = cc.ConnectorInput
    run = lambda inp: asyncio.run(cc._connector(CTX, inp))
    out = run(C(op="list", verbose=True, state="active"))
    assert out["called"] == "_me" and out["inp"].verbose is True and out["inp"].state == "active"
    # #326 : `name` était déclaré sur l'outil mais perdu ici sur op=list (seuls
    # select/pause/… le lisaient) → le filtre doit atteindre `_me`, pas la poubelle.
    assert run(C(op="list", name="zohodesk"))["inp"].name == "zohodesk"
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
    for nom in ("_list_rules", "_set_rule", "_remove_rule", "_share_events"):
        monkeypatch.setattr(connectors_sharing, nom, _tag(nom))
    monkeypatch.setattr(connectors_transfert, "_transfer", _tag("_transfer"))
    monkeypatch.setattr(connectors_verify, "_verify", _atag("_verify"))
    I = cc.InstanceInput
    run = lambda inp: asyncio.run(cc._instance(CTX, inp))
    out = run(I(op="list", connector="folk", level="org"))
    assert out["called"] == "_list" and out["inp"].level == "org"
    out = run(I(op="rules", instance="inst:12"))
    assert out["called"] == "_list_rules" and out["inp"].instance_id == 12
    out = run(I(op="set_rule", instance="12", to="bob", level="write",
                services=["outlook"]))
    assert out["called"] == "_set_rule"
    assert (out["inp"].instance_id, out["inp"].to, out["inp"].level,
            out["inp"].services) == (12, "bob", "write", ["outlook"])
    out = run(I(op="remove_rule", instance="inst:12", to_group=3))
    assert out["called"] == "_remove_rule" and out["inp"].to_group == 3
    out = run(I(op="share_events", instance="inst:12", limit=5))
    assert out["called"] == "_share_events" and out["inp"].limit == 5
    out = run(I(op="transfer", instance="inst:12", to_org=True))
    assert out["called"] == "_transfer" and out["inp"].to_org is True
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
        run(I(op="set_rule", instance="inst:12", to="bob", level="admin"))
    assert e.value.code == "invalid_level"
    with pytest.raises(AuthzDenied) as e:
        run(I(op="rules"))
    assert e.value.code == "missing_instance"
    with pytest.raises(AuthzDenied) as e:
        run(I(op="rules", instance="member:8:x:zoho"))
    assert e.value.code == "invalid_instance"


def test_le_pret_est_retire_sans_alias():
    import typing
    assert "lend" not in typing.get_args(cc.InstanceInput.model_fields["op"].annotation)
    from oto_mcp.capabilities.registry import CAPABILITIES
    cles = {c.key for c in CAPABILITIES}
    assert not cles & {"connectors.lend_instance", "connectors.lend_state", "org.loans.list"}


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


# ── oto_account_access : retiré (#1499) ────────────────────────────────────
def test_account_access_is_gone_and_its_name_refuses_naming_the_rules():
    """Plus de prêt de compte : ni capacité, ni route REST, ni outil MCP. Le nom
    refuse en nommant le geste qui aboutit (une règle sur le compte, le nommage)."""
    from oto_mcp import outils_retires
    from oto_mcp.capabilities.registry import CAPABILITIES
    assert not [c.key for c in CAPABILITIES
                if "account_grants" in c.key or c.key.endswith(".account_access")
                or c.mcp == "oto_account_access"]
    assert not [b for c in CAPABILITIES for b in c.rest_bindings()
                if "/connector-accounts" in str(b)]
    refus = outils_retires.retrait("oto_account_access")
    assert refus is not None
    assert "op=set_rule" in refus.message and "_account=" in refus.message


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
        "connectors.console.connector": "oto_connector",
        "connectors.console.instance": "oto_instance",
        "connectors.console.identity": "oto_identity",
    }
    for key, mcp in expected.items():
        assert caps[key].mcp == mcp, key
    # Les capacités d'origine ne portent PLUS de binding MCP (REST intact).
    demoted = [
        "connectors.activation.org_list", "connectors.activation.set_org",
        "connectors.activation.clear_org", "connectors.activation.group_list",
        "connectors.activation.set_group", "connectors.activation.clear_group",
        "connectors.me", "connectors.select", "connectors.pause", "connectors.unselect",
        "connectors.recommend", "connectors.force.member", "connectors.instances.list",
        "connectors.identities", "connectors.set_default_identity",
        "connectors.verify",
    ]
    for key in demoted:
        assert caps[key].mcp is None, key
        assert caps[key].rest_bindings(), key                 # la face REST reste
