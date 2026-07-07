"""Consultation d'une org tierce EN LECTURE par un opérateur plateforme (refonte admin
orgs). `effective_org_role` accorde un accès LECTEUR (`org_member`, jamais admin) à un
opérateur qui consulte ACTIVEMENT l'org (header `X-Oto-Org` REST → `session_org.current_view_org`),
borné à cette org. C'est le seam unique lu par `is_org_member` (autz `ORG_MEMBER`) ET
`ownership.can_access` (contenu org) — donc les deux couches de lecture le respectent.
Le middleware REST impose EN PLUS le GET-only (double garde read-only) et le MCP ne pose
jamais ce contextvar.
"""
import pytest

from oto_mcp import roles, org_store, access, session_org


@pytest.fixture(autouse=True)
def _not_super(monkeypatch):
    # Personne n'est super_admin ici : l'escalade masquerait la logique « opérateur ».
    monkeypatch.setattr(access, "is_super_admin", lambda sub: False)


def _real_role(monkeypatch, role):
    monkeypatch.setattr(org_store, "get_org_role", lambda org_id, sub: role)


def test_operator_consulting_org_is_reader(monkeypatch):
    _real_role(monkeypatch, None)  # aucun rôle réel dans l'org
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: True)
    tok = session_org.set_view_org(172)
    try:
        assert roles.effective_org_role("op", 172) == roles.ORG_MEMBER
        assert roles.is_org_member("op", 172) is True   # lecture autorisée
        assert roles.is_org_admin("op", 172) is False   # jamais admin → pas d'écriture
    finally:
        session_org.reset_view_org(tok)


def test_operator_without_consultation_has_no_access(monkeypatch):
    _real_role(monkeypatch, None)
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: True)
    # Aucun view_org posé → pas d'accès (pas d'effet de bord global hors consultation).
    assert roles.effective_org_role("op", 172) is None
    assert roles.is_org_member("op", 172) is False


def test_operator_consulting_other_org_not_granted(monkeypatch):
    _real_role(monkeypatch, None)
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: True)
    tok = session_org.set_view_org(999)  # consulte 999, PAS 172
    try:
        assert roles.effective_org_role("op", 172) is None  # borné à l'org consultée
    finally:
        session_org.reset_view_org(tok)


def test_non_operator_consulting_denied(monkeypatch):
    _real_role(monkeypatch, None)
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: False)
    tok = session_org.set_view_org(172)
    try:
        assert roles.effective_org_role("u", 172) is None
    finally:
        session_org.reset_view_org(tok)


def test_real_member_role_wins(monkeypatch):
    _real_role(monkeypatch, roles.ORG_ADMIN)  # membre réel (admin d'org)
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: True)
    # Le rôle réel prime, indépendamment de toute consultation.
    assert roles.effective_org_role("m", 172) == roles.ORG_ADMIN
    assert roles.is_org_admin("m", 172) is True
