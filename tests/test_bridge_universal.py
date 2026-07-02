"""Connecteur bridge universel (ADR 0034) : namespace fixe `bridge_*`, credential =
champs standard du coffre (base_url/token/label), plus de `meta.base_url`.

Les tools s'enregistrent inconditionnellement (la visibilité suit le régime commun)
et l'exécution lève une erreur ACTIONNABLE sans credential.
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


def test_bridge_tools_registered():
    mcp = _FakeMCP()
    remote.register(mcp)
    assert mcp.registered == ["bridge_describe", "bridge_call"]


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


def test_bridge_credential_resolution_error_is_actionable(monkeypatch):
    # resolve_credential_fields peut lever (pas de credential dans la cascade) →
    # traduit en erreur actionnable, jamais une exception brute.
    monkeypatch.setattr(remote, "current_user_sub_from_token", lambda: "sub123")
    monkeypatch.setattr(remote.access, "resolve_credential_fields",
                        lambda p: (_ for _ in ()).throw(RuntimeError("no cred")))
    with pytest.raises(McpError):
        remote._bridge_credential()


def test_bridge_stdio_local_refused(monkeypatch):
    monkeypatch.setattr(remote, "current_user_sub_from_token", lambda: None)
    with pytest.raises(McpError):
        remote._bridge_credential()
