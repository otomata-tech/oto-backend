"""Tier ÉQUIPE de l'activation de connecteur (ADR 0012, restrict-only, B1).

Cœur = l'invariant MONOTONE : une équipe ne peut que RETRANCHER de ce que l'org
expose, jamais rendre visible un connecteur coupé au-dessus. La résolution pure est
testée ici ; le chemin SQL (helpers `group_*_activation`) est vérifié au déploiement
(comme le palier org). Les gardes de capacité (refus d'`enabled=True` ; connecteur
non exposé par l'org) sont testées par stub (pas de DB).
"""
from types import SimpleNamespace

import pytest

from oto_mcp.connector_activation import effective_for_group


# ── résolution pure (l'invariant monotone) ───────────────────────────────────

def test_group_cut_narrows():
    # l'org expose a,b,c ; l'équipe coupe b → effectif = a,c
    assert effective_for_group({"a", "b", "c"}, {"b"}) == {"a", "c"}


def test_group_cannot_expose_beyond_org():
    # « couper » un connecteur que l'org n'expose pas n'expose jamais (pas d'ajout).
    assert effective_for_group({"a"}, {"x"}) == {"a"}
    # une équipe ne peut pas rendre visible ce que l'org a coupé.
    assert effective_for_group(set(), {"b"}) == set()
    assert effective_for_group(set(), set()) == set()


def test_no_cut_inherits_org():
    assert effective_for_group({"a", "b"}, set()) == {"a", "b"}


# ── gardes de capacité (stub, pas de DB) ─────────────────────────────────────

def _ctx():
    return SimpleNamespace(sub="u")


def test_capability_refuses_expose(monkeypatch):
    """set_group refuse enabled=True — une équipe ne peut que restreindre."""
    from oto_mcp.capabilities import connectors_activation as cap
    from oto_mcp.capabilities._types import AuthzDenied

    monkeypatch.setattr(cap.providers, "REGISTRY", {"serper": object()})
    inp = cap.GroupActivationSetInput(group_id=1, name="serper", enabled=True)
    with pytest.raises(AuthzDenied) as ei:
        cap._group_set(_ctx(), inp)
    assert ei.value.code == "group_cannot_expose"


def test_capability_requires_org_available(monkeypatch):
    """set_group (cut) refuse un connecteur que l'org n'expose pas (rien à couper)."""
    from oto_mcp.capabilities import connectors_activation as cap
    from oto_mcp.capabilities._types import AuthzDenied

    monkeypatch.setattr(cap.providers, "REGISTRY", {"serper": object()})
    monkeypatch.setattr(cap.group_store, "get_group", lambda gid: {"id": gid, "org_id": 42})
    monkeypatch.setattr(cap.connector_activation, "exposed_connectors", lambda org: set())
    inp = cap.GroupActivationSetInput(group_id=1, name="serper", enabled=False)
    with pytest.raises(AuthzDenied) as ei:
        cap._group_set(_ctx(), inp)
    assert ei.value.code == "org_disabled"


def test_capability_cut_ok(monkeypatch):
    """set_group (cut) d'un connecteur exposé par l'org → stocke la coupure."""
    from oto_mcp.capabilities import connectors_activation as cap

    monkeypatch.setattr(cap.providers, "REGISTRY", {"serper": object()})
    monkeypatch.setattr(cap.group_store, "get_group", lambda gid: {"id": gid, "org_id": 42})
    monkeypatch.setattr(cap.connector_activation, "exposed_connectors", lambda org: {"serper"})
    posed = {}
    monkeypatch.setattr(cap.connector_activation, "set_group_activation",
                        lambda gid, name, enabled, set_by=None: posed.update(
                            {"gid": gid, "name": name, "enabled": enabled, "by": set_by}))
    out = cap._group_set(_ctx(), cap.GroupActivationSetInput(group_id=7, name="serper", enabled=False))
    assert out["enabled"] is False
    assert posed == {"gid": 7, "name": "serper", "enabled": False, "by": "u"}
