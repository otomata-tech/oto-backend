"""Masquage des bridges remote (ADR 0031) : un outil remote n'apparaît qu'à l'org
qui détient son credential remote — règle générique dans `compute_hidden_tools`,
indépendante de l'ex-concept `grant_only`. Sécurité : un bridge ne doit pas fuiter
aux orgs qui n'ont pas son credential.
"""
import pytest

from oto_mcp import session_visibility as sv


class _FakeMCP:
    def __init__(self, names):
        self._names = names

    async def list_tools(self, run_middleware=False):
        return [type("T", (), {"name": n})() for n in self._names]


class _FakeCtx:
    def __init__(self, names):
        self.fastmcp = _FakeMCP(names)


@pytest.fixture
def _neutral(monkeypatch):
    """Neutralise toutes les sources de masquage SAUF la règle remote."""
    monkeypatch.setattr(sv.db, "list_user_disabled_tools", lambda sub, org: [])
    monkeypatch.setattr(sv.db, "list_user_enabled_tools", lambda sub, org: [])
    monkeypatch.setattr(sv.db, "org_restricted_connectors", lambda org: set())
    monkeypatch.setattr(sv.access, "is_super_admin", lambda sub: False)
    monkeypatch.setattr(sv.access, "current_group", lambda sub: None)
    monkeypatch.setattr(sv.connector_activation, "exposed_connectors", lambda org: set())
    monkeypatch.setattr(sv.connectors, "connector_for_namespace", lambda ns: None)
    monkeypatch.setattr(sv.org_store, "get_org_default_tools", lambda org: None)
    yield


def _wire_remote(monkeypatch, *, active_org, remote_all, org_remote):
    monkeypatch.setattr(sv.access, "current_org", lambda sub: active_org)
    monkeypatch.setattr(sv.credentials_store, "list_remote_namespaces", lambda: remote_all)
    monkeypatch.setattr(sv.credentials_store, "org_remote_namespaces", lambda org: org_remote)


@pytest.mark.asyncio
async def test_remote_hidden_without_credential(_neutral, monkeypatch):
    # Org sans credential du bridge `mm` → ses outils sont masqués.
    _wire_remote(monkeypatch, active_org=99, remote_all={"mm"}, org_remote=set())
    hidden = await sv.compute_hidden_tools(_FakeCtx(["mm_call", "mm_describe", "fr_search"]), "u")
    assert {"mm_call", "mm_describe"} <= hidden
    assert "fr_search" not in hidden  # tool non-remote : visible


@pytest.mark.asyncio
async def test_remote_visible_with_credential(_neutral, monkeypatch):
    # Org qui détient le credential `mm` → ses outils sont visibles.
    _wire_remote(monkeypatch, active_org=35, remote_all={"mm"}, org_remote={"mm"})
    hidden = await sv.compute_hidden_tools(_FakeCtx(["mm_call", "mm_describe", "fr_search"]), "u")
    assert "mm_call" not in hidden and "mm_describe" not in hidden
