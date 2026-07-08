"""ACL connecteur au grain ÉQUIPE (ADR 0012 B2, restrict-only).

Le seam `access.group_rbac_denied_connectors` (mirror de `rbac_denied_connectors` au
grain équipe) est le cœur : NARROWING de l'org — une équipe réserve un connecteur à un
sous-ensemble de SES membres, jamais elle ne débloque ce que l'org a fermé. Bypass
descendant (super_admin / org_admin parent / group_admin). Le call-time
`require_connector_access` OR les deux seams (org|équipe). Chemin SQL vérifié au
déploiement ; gardes de capacité (membre ∈ équipe) par stub.
"""
from types import SimpleNamespace

import pytest

from oto_mcp import access, roles


# ── seam RBAC d'équipe (le narrowing + bypass) ───────────────────────────────

def test_group_seam_none():
    assert access.group_rbac_denied_connectors("u", None) == set()


def test_group_seam_super_admin(monkeypatch):
    monkeypatch.setattr(access, "is_super_admin", lambda s: True)
    assert access.group_rbac_denied_connectors("u", 5) == set()


def test_group_seam_team_lead_bypass(monkeypatch):
    # chef d'équipe (ou org_admin parent) — celui qui gouverne l'ACL n'en est pas victime.
    monkeypatch.setattr(access, "is_super_admin", lambda s: False)
    monkeypatch.setattr(roles, "can_admin_group", lambda s, g: True)
    assert access.group_rbac_denied_connectors("lead", 5) == set()


def test_group_seam_denies_non_allowed(monkeypatch):
    monkeypatch.setattr(access, "is_super_admin", lambda s: False)
    monkeypatch.setattr(roles, "can_admin_group", lambda s, g: False)
    monkeypatch.setattr(access.db, "group_restricted_connectors", lambda g: {"serper", "attio"})
    monkeypatch.setattr(access.db, "group_member_allowed_connectors", lambda s, g: {"serper"})
    # attio réservé et le membre n'y est pas → refusé ; serper autorisé → pas refusé
    assert access.group_rbac_denied_connectors("u", 5) == {"attio"}


def test_group_seam_allows_member(monkeypatch):
    monkeypatch.setattr(access, "is_super_admin", lambda s: False)
    monkeypatch.setattr(roles, "can_admin_group", lambda s, g: False)
    monkeypatch.setattr(access.db, "group_restricted_connectors", lambda g: {"serper"})
    monkeypatch.setattr(access.db, "group_member_allowed_connectors", lambda s, g: {"serper"})
    assert access.group_rbac_denied_connectors("u", 5) == set()


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
