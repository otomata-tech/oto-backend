"""RBAC connecteur interne à l'org (ADR 0025) — backstop call-time `require_connector_access`.

On stubbe les lectures DB (org restreints / connecteurs autorisés du membre) + l'identité,
et on vérifie la règle : ouvert par défaut, restreint = deny sauf principal autorisé
(département/user), super_admin bypasse, fail-open sur erreur infra.
"""
import pytest
from mcp.shared.exceptions import McpError

from oto_mcp import access


def _wire(monkeypatch, *, restricted=frozenset(), allowed=frozenset(),
          org=42, is_admin=False):
    monkeypatch.setattr(access, "is_super_admin", lambda sub: is_admin)
    monkeypatch.setattr(access, "current_org", lambda sub: org)
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


def test_no_org_not_applicable(monkeypatch):
    _wire(monkeypatch, restricted={"pennylane"}, allowed=set(), org=None)
    access.require_connector_access("pennylane", sub="u1")  # pas d'org → restriction inapplicable


def test_stdio_local_no_sub_is_open(monkeypatch):
    monkeypatch.setattr(access, "current_user_sub_from_token", lambda: None)
    access.require_connector_access("pennylane")  # sub=None (stdio local) → accès complet


def test_fail_open_on_infra_error(monkeypatch):
    monkeypatch.setattr(access, "is_super_admin", lambda sub: False)
    monkeypatch.setattr(access, "current_org", lambda sub: 42)
    def _boom(*a, **k):
        raise RuntimeError("DB down")
    monkeypatch.setattr(access.db, "org_restricted_connectors", _boom)
    access.require_connector_access("pennylane", sub="u1")  # fail-open → ne lève pas
