"""Fusion `oto_admin_namespace_access` (ADR 0009, *_op) : autz op-aware + routage.

Couvre le combinateur `ADMIN_BY_OP` (autz déclarée, choisie par op) et le handler
consolidé qui route entitlement-org vs grant-user selon `scope`, sans redescendre
l'autz dans le handler.
"""
import types

import pytest
from pydantic import ValidationError

from oto_mcp.capabilities import namespace_access as na
from oto_mcp.capabilities._authz import ADMIN_BY_OP
from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx


# ── ADMIN_BY_OP : la règle d'autz dépend de op, refus net hors map ───────────
def test_admin_by_op_selects_rule_per_op():
    calls = []
    super_rule = lambda raw, inp: calls.append("super") or ResolvedCtx(sub="s")
    platform_rule = lambda raw, inp: calls.append("platform") or ResolvedCtx(sub="s")
    rule = ADMIN_BY_OP({"grant": super_rule, "list": platform_rule})

    rule(RawCtx(sub="s"), types.SimpleNamespace(op="grant"))
    rule(RawCtx(sub="s"), types.SimpleNamespace(op="list"))
    assert calls == ["super", "platform"]


def test_admin_by_op_refuses_unknown_op():
    rule = ADMIN_BY_OP({"grant": lambda r, i: ResolvedCtx(sub="s")})
    with pytest.raises(AuthzDenied) as e:
        rule(RawCtx(sub="s"), types.SimpleNamespace(op="revoke"))
    assert e.value.code == "unsupported_op"


# ── Input : op/scope sont des enums fermés ───────────────────────────────────
def test_input_rejects_unknown_op_and_scope():
    with pytest.raises(ValidationError):
        na.NamespaceAccessInput(op="purge")
    with pytest.raises(ValidationError):
        na.NamespaceAccessInput(op="grant", scope="planet")


# ── Routage du handler : org → entitlement, user → grant ─────────────────────
@pytest.fixture
def stores(monkeypatch):
    rec = {"calls": []}
    fake_org = types.SimpleNamespace(
        get_org=lambda oid: {"id": oid},
        grant_org_entitlement=lambda oid, ns, granted_by=None: rec["calls"].append(("org_grant", oid, ns)),
        revoke_org_entitlement=lambda oid, ns: rec["calls"].append(("org_revoke", oid, ns)) or True,
        list_org_entitlements=lambda oid: rec["calls"].append(("org_list", oid)) or [{"namespace": "mm"}],
    )
    fake_db = types.SimpleNamespace(
        get_user_by_email=lambda e: {"sub": "sub_from_email"},
        grant_namespace=lambda sub, ns, granted_by=None: rec["calls"].append(("user_grant", sub, ns)),
        revoke_namespace=lambda sub, ns: rec["calls"].append(("user_revoke", sub, ns)) or True,
        list_namespace_grants=lambda ns=None: rec["calls"].append(("user_list", ns)) or [],
    )
    monkeypatch.setattr(na, "org_store", fake_org)
    monkeypatch.setattr(na, "db", fake_db)
    monkeypatch.setattr(na, "ADMIN_GRANT_ONLY_NAMESPACES", frozenset({"mm", "gocardless"}))
    return rec


_CTX = ResolvedCtx(sub="admin_sub")


def _run(**kw):
    return na._namespace_access(_CTX, na.NamespaceAccessInput(**kw))


def test_grant_org_routes_to_entitlement(stores):
    out = _run(op="grant", scope="org", target="42", namespace="mm")
    assert ("org_grant", 42, "mm") in stores["calls"]
    assert out["granted"] is True and out["org_id"] == 42


def test_grant_user_routes_to_namespace_grant(stores):
    out = _run(op="grant", scope="user", target="sub_x", namespace="mm")
    assert ("user_grant", "sub_x", "mm") in stores["calls"]
    assert out["target"] == "sub_x"


def test_grant_user_resolves_email(stores):
    _run(op="grant", scope="user", target="a@b.co", namespace="mm")
    assert ("user_grant", "sub_from_email", "mm") in stores["calls"]


def test_list_user_is_global_with_optional_filter(stores):
    _run(op="list", scope="user")
    assert ("user_list", None) in stores["calls"]


def test_list_org_requires_target(stores):
    with pytest.raises(AuthzDenied) as e:
        _run(op="list", scope="org")
    assert e.value.code == "missing_target"


def test_uncontrolled_namespace_refused(stores):
    with pytest.raises(AuthzDenied) as e:
        _run(op="grant", scope="org", target="42", namespace="bogus")
    assert e.value.code == "namespace_not_controlled"


def test_grant_requires_target(stores):
    with pytest.raises(AuthzDenied) as e:
        _run(op="grant", scope="org", namespace="mm")
    assert e.value.code == "missing_target"
