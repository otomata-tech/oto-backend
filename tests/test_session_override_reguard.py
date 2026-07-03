"""R0 — re-garde des overrides de session group/project à la RÉSOLUTION.

Complément du correctif #108 (org=, cf. test_meta_active_org) sur les deux axes
restants : un Mcp-Session-Id réutilisé par un AUTRE compte ne doit pas hériter du
groupe / projet de session du compte précédent. `current_group` re-garde via
`roles.can_read_group` ; `current_project` via `ownership.can_access(..,'project',
..,'read')` (privacy-by-default ADR 0030). On exerce le vrai chemin de résolution
(pas de stub des seams eux-mêmes) — leçon row-shape/fail-open (CLAUDE.md).
"""
import pytest

from oto_mcp import access, auth_hooks, group_store, ownership, roles, session_org


@pytest.fixture(autouse=True)
def _neutralise_branches_annexes(monkeypatch):
    # Isole le re-garde : pas de sous-domaine, pas de view-as, pas d'override d'org.
    monkeypatch.setattr(session_org, "current_subdomain_candidate", lambda: None)
    monkeypatch.setattr(session_org, "current_view_group", lambda: None)
    monkeypatch.setattr(session_org, "current_view_org", lambda: None)
    monkeypatch.setattr(session_org, "current_override", lambda: (False, None))
    yield


# ── current_group ────────────────────────────────────────────────────────────
def test_group_override_honored_when_reader(monkeypatch):
    monkeypatch.setattr(session_org, "current_group_override", lambda: (True, 5))
    monkeypatch.setattr(roles, "can_read_group", lambda sub, g: True)
    assert access.current_group("u") == 5


def test_group_override_ignored_when_not_reader(monkeypatch):
    # session_id réutilisé : override vers 5 mais le caller n'est pas lecteur de 5.
    monkeypatch.setattr(session_org, "current_group_override", lambda: (True, 5))
    monkeypatch.setattr(roles, "can_read_group", lambda sub, g: False)
    monkeypatch.setattr(group_store, "get_active_group", lambda sub: 99)
    assert access.current_group("u") == 99  # repli maison (groupe PROPRE au caller)


# ── current_project ──────────────────────────────────────────────────────────
def test_project_override_honored_when_can_access(monkeypatch):
    monkeypatch.setattr(session_org, "current_project_override", lambda: 7)
    monkeypatch.setattr(auth_hooks, "current_user_sub_from_token", lambda: "u")
    monkeypatch.setattr(ownership, "can_access", lambda sub, rt, rid, want="read": True)
    assert access.current_project() == 7


def test_project_override_ignored_when_no_access(monkeypatch):
    # session_id réutilisé par « other » : le projet 7 ne lui est pas accessible.
    monkeypatch.setattr(session_org, "current_project_override", lambda: 7)
    monkeypatch.setattr(auth_hooks, "current_user_sub_from_token", lambda: "other")
    monkeypatch.setattr(ownership, "can_access", lambda sub, rt, rid, want="read": False)
    assert access.current_project() is None  # hors projet, pas de fuite


def test_project_override_honored_without_sub(monkeypatch):
    # stdio/tests (sub non identifiable, hors surface authentifiée) : pas de régression.
    monkeypatch.setattr(session_org, "current_project_override", lambda: 7)
    monkeypatch.setattr(auth_hooks, "current_user_sub_from_token", lambda: None)
    assert access.current_project() == 7


def test_no_project_override_is_none(monkeypatch):
    monkeypatch.setattr(session_org, "current_project_override", lambda: None)
    assert access.current_project() is None
