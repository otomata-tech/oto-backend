"""Connecteur bridge universel (ADR 0034) : namespace fixe `bridge_*`, credential =
champs standard du coffre (base_url/token/label), plus de `meta.base_url`.

B2 : les tools s'enregistrent INCONDITIONNELLEMENT (même DB indispo — la visibilité
suit le régime commun) et l'exécution lève une erreur ACTIONNABLE sans credential.
"""
import pytest
from mcp.shared.exceptions import McpError

from oto_mcp.tools import remote


class _FakeMCP:
    def __init__(self):
        self.registered = []

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.registered.append(kwargs.get("name") or fn.__name__)
            return fn
        return deco


def test_bridge_tools_registered_even_if_discovery_down(monkeypatch):
    # DB/coffre indispo au boot → le legacy data-driven se dégrade, mais le bridge
    # UNIVERSEL (registre) s'enregistre quand même.
    monkeypatch.setattr(remote.credentials_store, "list_remote_namespaces",
                        lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    mcp = _FakeMCP()
    remote.register(mcp)
    assert "bridge_describe" in mcp.registered
    assert "bridge_call" in mcp.registered


def test_bridge_namespace_never_collides_with_legacy(monkeypatch):
    # un credential legacy nommé `bridge` (meta.base_url) ne doit PAS doubler les tools
    monkeypatch.setattr(remote.credentials_store, "list_remote_namespaces",
                        lambda: {"bridge", "mm"})
    mcp = _FakeMCP()
    remote.register(mcp)
    assert mcp.registered.count("bridge_describe") == 1
    assert "mm_describe" in mcp.registered  # legacy toujours servi (jusqu'à B4)


def test_bridge_credential_missing_is_actionable(monkeypatch):
    monkeypatch.setattr(remote, "current_user_sub_from_token", lambda: "sub123")
    monkeypatch.setattr(remote.access, "resolve_credential_fields", lambda p: {})
    with pytest.raises(McpError) as e:
        remote._bridge_credential()
    assert "carte Bridge" in str(e.value)


def test_bridge_credential_resolves_fields(monkeypatch):
    monkeypatch.setattr(remote, "current_user_sub_from_token", lambda: "sub123")
    monkeypatch.setattr(remote.access, "resolve_credential_fields",
                        lambda p: {"base_url": "https://bridge.acme.com/", "token": "t0k",
                                   "label": "Back-office Acme"})
    base_url, token = remote._bridge_credential()
    assert base_url == "https://bridge.acme.com"   # slash final normalisé
    assert token == "t0k"


def test_bridge_stdio_local_refused(monkeypatch):
    monkeypatch.setattr(remote, "current_user_sub_from_token", lambda: None)
    with pytest.raises(McpError):
        remote._bridge_credential()
