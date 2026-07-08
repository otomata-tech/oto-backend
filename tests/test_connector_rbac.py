"""RBAC connecteur interne à l'org (ADR 0025) — backstop call-time `require_connector_access`.

On stubbe les lectures DB (org restreints / connecteurs autorisés du membre) + l'identité,
et on vérifie la règle : ouvert par défaut, restreint = deny sauf principal autorisé
(département/user), super_admin ET org_admin bypassent (escalade descendante roles.py —
l'admin de l'org gouverne l'ACL, il n'en est jamais prisonnier), fail-open sur erreur infra.
"""
import pytest
from mcp.shared.exceptions import McpError

from oto_mcp import access, roles


def _wire(monkeypatch, *, restricted=frozenset(), allowed=frozenset(),
          org=42, is_admin=False, is_org_admin=False):
    monkeypatch.setattr(access, "is_super_admin", lambda sub: is_admin)
    monkeypatch.setattr(access, "current_org", lambda sub: org)
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, o: is_org_admin)
    monkeypatch.setattr(access.db, "org_restricted_connectors", lambda o: set(restricted))
    monkeypatch.setattr(access.db, "member_allowed_connectors", lambda s, o: set(allowed))


def test_unrestricted_connector_is_open(monkeypatch):
    _wire(monkeypatch, restricted={"silae"})  # pennylane non restreint
    access.require_connector_access("pennylane", sub="u1")  # ne lève pas


def test_restricted_and_allowed_passes(monkeypatch):
    _wire(monkeypatch, restricted={"pennylane"}, allowed={"pennylane"})
    access.require_connector_access("pennylane", sub="u1")  # autorisé (dept/user) → OK


def test_restricted_and_denied_raises(monkeypatch):
    _wire(monkeypatch, restricted={"pennylane"}, allowed=set())
    with pytest.raises(McpError, match="réservé"):
        access.require_connector_access("pennylane", sub="u1")


def test_super_admin_bypasses(monkeypatch):
    _wire(monkeypatch, restricted={"pennylane"}, allowed=set(), is_admin=True)
    access.require_connector_access("pennylane", sub="u1")  # bypass


def test_org_admin_bypasses(monkeypatch):
    # L'admin de l'org transcende la restriction (un connecteur réservé à une
    # équipe reste utilisable par l'admin — vécu Clémence/pennylane 2026-07-08).
    _wire(monkeypatch, restricted={"pennylane"}, allowed=set(), is_org_admin=True)
    access.require_connector_access("pennylane", sub="clemence")  # bypass


def test_org_admin_of_another_org_does_not_bypass(monkeypatch):
    # L'escalade est scopée à L'ORG COURANTE : être admin ailleurs ne donne rien ici.
    _wire(monkeypatch, restricted={"pennylane"}, allowed=set(), is_org_admin=False)
    with pytest.raises(McpError, match="réservé"):
        access.require_connector_access("pennylane", sub="u1")


def test_no_org_not_applicable(monkeypatch):
    _wire(monkeypatch, restricted={"pennylane"}, allowed=set(), org=None)
    access.require_connector_access("pennylane", sub="u1")  # pas d'org → restriction inapplicable


def test_stdio_local_no_sub_is_open(monkeypatch):
    monkeypatch.setattr(access, "current_user_sub_from_token", lambda: None)
    access.require_connector_access("pennylane")  # sub=None (stdio local) → accès complet


def test_fail_open_on_infra_error(monkeypatch):
    monkeypatch.setattr(access, "is_super_admin", lambda sub: False)
    monkeypatch.setattr(access, "current_org", lambda sub: 42)
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, o: False)
    def _boom(*a, **k):
        raise RuntimeError("DB down")
    monkeypatch.setattr(access.db, "org_restricted_connectors", _boom)
    access.require_connector_access("pennylane", sub="u1")  # fail-open → ne lève pas


def test_rbac_denied_connectors_shared_seam(monkeypatch):
    """Le seam `rbac_denied_connectors` (consommé par les 4 surfaces) rend le
    même verdict que le call-time : denied = restricted − allowed, vidé pour
    super_admin et org_admin."""
    _wire(monkeypatch, restricted={"pennylane", "zoho"}, allowed={"zoho"})
    assert access.rbac_denied_connectors("u1", 42) == {"pennylane"}
    _wire(monkeypatch, restricted={"pennylane"}, allowed=set(), is_org_admin=True)
    assert access.rbac_denied_connectors("clemence", 42) == set()
    assert access.rbac_denied_connectors("u1", None) == set()
