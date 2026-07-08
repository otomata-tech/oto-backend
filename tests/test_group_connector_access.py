"""ACL connecteur au grain ÉQUIPE (ADR 0012 B2, restrict-only).

Cœur = l'invariant MONOTONE du gate DUR : l'équipe active ne peut qu'AJOUTER une
restriction par-dessus l'org — jamais débloquer ce que l'org autorise. Le verdict pur
`_connector_blocked` est testé ici ; le chemin SQL (helpers `group_connector_access`)
est vérifié au déploiement. Les gardes de capacité (membre ∈ équipe) par stub.
"""
from types import SimpleNamespace

import pytest

from oto_mcp.access import _connector_blocked


# ── verdict DUR pur (l'invariant) ────────────────────────────────────────────

def test_open_everywhere():
    assert _connector_blocked("x", set(), set(), set(), set()) is False


def test_org_gate():
    assert _connector_blocked("x", {"x"}, set(), set(), set()) is True      # org restreint, non autorisé
    assert _connector_blocked("x", {"x"}, {"x"}, set(), set()) is False     # org restreint, autorisé


def test_team_narrows_open_org():
    # l'org n'a pas restreint x, mais l'équipe le réserve et le membre n'y est pas → bloqué
    assert _connector_blocked("x", set(), set(), {"x"}, set()) is True
    # membre autorisé dans l'équipe → ouvert
    assert _connector_blocked("x", set(), set(), {"x"}, {"x"}) is False


def test_team_cannot_unblock_org():
    # MONOTONE : si l'org bloque, aucun état d'équipe ne peut débloquer (le terme équipe
    # est un OR — il ne peut que faire passer False→True, jamais True→False).
    assert _connector_blocked("x", {"x"}, set(), set(), set()) is True        # équipe silencieuse
    assert _connector_blocked("x", {"x"}, set(), {"x"}, {"x"}) is True        # équipe « autorise » → org bloque quand même


# ── gardes de capacité (stub, pas de DB) ─────────────────────────────────────

def test_group_acl_rejects_non_member(monkeypatch):
    from oto_mcp.capabilities import connectors_acl as cap
    from oto_mcp.capabilities._types import AuthzDenied

    monkeypatch.setattr(cap.providers, "connector_for_provider", lambda c: object())
    monkeypatch.setattr(cap.group_store, "is_group_member", lambda sub, gid: False)
    with pytest.raises(AuthzDenied) as ei:
        cap._group_grant(SimpleNamespace(sub="lead"),
                         cap.GroupAclSetInput(group_id=1, connector="serper", member="u2"))
    assert ei.value.code == "user_not_in_group"


def test_group_acl_grant_ok(monkeypatch):
    from oto_mcp.capabilities import connectors_acl as cap

    monkeypatch.setattr(cap.providers, "connector_for_provider", lambda c: object())
    monkeypatch.setattr(cap.group_store, "is_group_member", lambda sub, gid: True)
    calls = {}
    monkeypatch.setattr(cap.db, "set_group_connector_access",
                        lambda g, c, m, granted_by=None: calls.update({"g": g, "c": c, "m": m, "by": granted_by}))
    out = cap._group_grant(SimpleNamespace(sub="lead"),
                           cap.GroupAclSetInput(group_id=3, connector="serper", member="u2"))
    assert out["restricted"] is True
    assert calls == {"g": 3, "c": "serper", "m": "u2", "by": "lead"}
