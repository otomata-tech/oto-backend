"""Sonde de vérification du connecteur Unipile (#133) + son enregistrement.

Couvre `tools/unipile._verify` (contrat probe : lève sur échec, None sur succès)
et le fait que `register(mcp)` l'enregistre dans `connector_verify` (→ catalogue
`verifiable`). Pas de réseau : `make_unipile_client` est monkeypatché."""

import pytest

from oto_mcp import connector_verify
from oto_mcp.tools import unipile


class _FakeClient:
    def __init__(self, accounts):
        self._accounts = accounts

    def list_accounts(self):
        return self._accounts


def _patch_client(monkeypatch, accounts):
    # `_verify` importe make_unipile_client depuis oto.tools.unipile en local.
    import oto.tools.unipile as core
    monkeypatch.setattr(core, "make_unipile_client",
                        lambda **kw: _FakeClient(accounts))


def test_verify_ok_when_accounts_present(monkeypatch):
    _patch_client(monkeypatch, [{"id": "acc-1", "type": "LINKEDIN"}])
    assert unipile._verify({"api_key": "k"}) is None   # succès = ne lève pas


def test_verify_raises_without_api_key():
    with pytest.raises(ValueError) as e:
        unipile._verify({})
    assert "absente" in str(e.value)


def test_verify_raises_when_no_account_connected(monkeypatch):
    _patch_client(monkeypatch, [])
    with pytest.raises(ValueError) as e:
        unipile._verify({"api_key": "k"})
    assert "aucun compte connecté" in str(e.value)


def test_verify_propagates_provider_error(monkeypatch):
    # Une clé morte → list_accounts lève (UnipileError 401) → remonte tel quel.
    class _Boom:
        def list_accounts(self):
            raise RuntimeError("Unipile 401: invalid api key")
    import oto.tools.unipile as core
    monkeypatch.setattr(core, "make_unipile_client", lambda **kw: _Boom())
    with pytest.raises(RuntimeError) as e:
        unipile._verify({"api_key": "dead"})
    assert "401" in str(e.value)


def test_register_registers_unipile_probe():
    # `register(mcp)` doit enregistrer la sonde (→ catalogue `verifiable: true`).
    class _FakeMcp:
        def tool(self, *a, **k):
            def deco(fn):
                return fn
            return deco

    unipile.register(_FakeMcp())
    assert connector_verify.supports("unipile")
    assert connector_verify.probe_for("unipile") is unipile._verify
