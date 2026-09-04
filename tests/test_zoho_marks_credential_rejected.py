"""oto#25 lot b2 — Zoho marque la ligne de coffre rejetée sur un refus du REFRESH
(`ZohoAuthError`, levée par `oto.tools.zoho.auth` — la seule exception qui vaut
« grant mort »), et RE-LÈVE toujours l'exception d'origine.

Voisin de `test_zoho_op_dispatch.py` (le routage `op=`) mais un sujet différent :
ici, le marquage. Le contrefactuel (`test_un_scope_mismatch_ne_marque_rien`) est la
garde nommée dans l'issue : `zoho_modules` distingue déjà `OAUTH_SCOPE_MISMATCH`
(`UpstreamHTTPError`) d'un grant mort — ce n'est pas un refus du refresh, il ne doit
déclencher aucun marquage.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from oto.tools.zoho import ZohoAuthError


def _tool(name: str):
    from fastmcp import FastMCP
    from oto_mcp.tools import zoho as Z

    m = FastMCP("t")
    Z.register(m)
    return asyncio.run(m.get_tool(name)).fn


class _RC:
    """Substitut de `ResolvedCredential` — une entité au palier MEMBRE (flaggable)."""
    entity_type, entity_id, account = "member", "2:sub-x", ""
    fields = {"client_id": "c", "client_secret": "s", "refresh_token": "r",
              "data_center": "eu"}


@pytest.fixture
def env(monkeypatch):
    """Client Zoho factice qui lève `ZohoAuthError` sur chaque méthode, plus un
    espion sur `credentials_store.update_meta`."""
    from oto.tools.zoho import client as zoho_client_mod
    from oto_mcp import access, credentials_store

    inst = MagicMock()
    boom = ZohoAuthError("invalid_grant: token revoked")
    inst.list_records.side_effect = boom
    inst.list_modules.side_effect = boom
    inst.list_notes.side_effect = boom
    monkeypatch.setattr(zoho_client_mod, "ZohoClient", lambda **kw: inst)
    monkeypatch.setattr(access, "resolve_credential", lambda *a, **k: _RC())

    calls = []

    def _update_meta(entity_type, entity_id, connector, account, patch, conn=None):
        calls.append((entity_type, entity_id, connector, account, patch))
        return True

    monkeypatch.setattr(credentials_store, "update_meta", _update_meta)
    return inst, calls


@pytest.mark.parametrize("tool,kwargs", [
    ("zoho_record", {"module": "Contacts"}),
    ("zoho_note", {"module": "Contacts", "record_id": "42"}),
    ("zoho_modules", {}),
])
def test_un_grant_mort_marque_la_ligne_et_relance(env, tool, kwargs):
    _inst, calls = env
    with pytest.raises(ZohoAuthError):
        _tool(tool)(**kwargs)
    assert len(calls) == 1, "le rejet doit être marqué UNE fois, sur la ligne réellement servie"
    entity_type, entity_id, connector, account, patch = calls[0]
    assert (entity_type, entity_id, connector, account) == (
        "member", "2:sub-x", "zoho", "")
    assert patch["health_ko"] is True
    assert "invalid_grant" in patch["health_reason"]


def test_un_scope_mismatch_ne_marque_rien(monkeypatch):
    """Contrefactuel : `zoho_modules` distingue déjà un `OAUTH_SCOPE_MISMATCH`
    (`UpstreamHTTPError`, token authentifié mais sans le scope settings) d'un grant
    mort — pas un refus du refresh, aucun marquage ne doit en résulter."""
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.zoho import client as zoho_client_mod
    from oto_mcp import access, credentials_store
    from oto_mcp.mcp_errors import McpError

    inst = MagicMock()
    inst.list_modules.side_effect = UpstreamHTTPError(403, "OAUTH_SCOPE_MISMATCH")
    monkeypatch.setattr(zoho_client_mod, "ZohoClient", lambda **kw: inst)
    monkeypatch.setattr(access, "resolve_credential", lambda *a, **k: _RC())

    calls = []
    monkeypatch.setattr(credentials_store, "update_meta",
                        lambda *a, **k: calls.append(a) or True)

    with pytest.raises(McpError):
        _tool("zoho_modules")()
    assert calls == [], "un scope mismatch n'est pas un grant mort : rien à marquer"
