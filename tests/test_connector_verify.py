"""Framework de sonde « tester la connexion » : registre + capacité + sonde Zoho.

La sonde est un appel SANS effet de bord qui LÈVE sur échec ; la capacité l'exécute et
transforme l'exception en `{ok:false, error}` (jamais un 500), en extrayant le message
propre d'une `McpError` (ex. data center Zoho manquant)."""
import asyncio
import types

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from oto_mcp import connector_verify
from oto_mcp.capabilities import connectors_verify as cv
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.tools import zoho


# --- registre -----------------------------------------------------------------

def test_registry_register_supports_probe_for():
    def probe(fields):  # noqa: ARG001
        return None
    connector_verify.register("_ut_x", probe)
    assert connector_verify.supports("_ut_x")
    assert connector_verify.probe_for("_ut_x") is probe
    assert not connector_verify.supports("_ut_absent")
    assert connector_verify.probe_for("_ut_absent") is None


# --- capacité : ok / échec / non supporté / message McpError ------------------

def _ctx():
    return ResolvedCtx(sub="user-1", org_id=42)


def _run(inp, *, fields=None, monkeypatch=None):
    """Exécute le handler avec _fields_for court-circuité (level=auto)."""
    if fields is not None:
        monkeypatch.setattr(
            cv.access, "resolve_credential",
            lambda *a, **k: types.SimpleNamespace(fields=fields),
        )
    return asyncio.run(cv._verify(_ctx(), inp))


def test_verify_ok(monkeypatch):
    connector_verify.register("_ut_ok", lambda fields: None)
    res = _run(cv.VerifyInput(provider="_ut_ok"), fields={"k": "v"}, monkeypatch=monkeypatch)
    assert res["ok"] is True
    assert res["provider"] == "_ut_ok"
    assert "elapsed_ms" in res


def test_verify_failure_returns_error_not_500(monkeypatch):
    def bad(fields):  # noqa: ARG001
        raise ValueError("clé refusée par le provider")
    connector_verify.register("_ut_bad", bad)
    res = _run(cv.VerifyInput(provider="_ut_bad"), fields={"k": "v"}, monkeypatch=monkeypatch)
    assert res["ok"] is False
    assert res["error"] == "clé refusée par le provider"


def test_verify_mcperror_message_is_extracted(monkeypatch):
    def bad(fields):  # noqa: ARG001
        raise McpError(ErrorData(code=INVALID_PARAMS, message="data center manquant"))
    connector_verify.register("_ut_mcp", bad)
    res = _run(cv.VerifyInput(provider="_ut_mcp"), fields={"k": "v"}, monkeypatch=monkeypatch)
    assert res["ok"] is False
    assert res["error"] == "data center manquant"  # pas le repr McpError brut


def test_verify_unsupported_provider_raises_authz():
    with pytest.raises(AuthzDenied) as ei:
        asyncio.run(cv._verify(_ctx(), cv.VerifyInput(provider="_ut_nope")))
    assert ei.value.code == "verify_unavailable"


def test_verify_async_probe_awaited(monkeypatch):
    async def aprobe(fields):  # noqa: ARG001
        return None
    connector_verify.register("_ut_async", aprobe)
    res = _run(cv.VerifyInput(provider="_ut_async"), fields={"k": "v"}, monkeypatch=monkeypatch)
    assert res["ok"] is True


# --- sonde Zoho : refresh token, message actionnable --------------------------

def _zoho_fields(**over):
    f = {"client_id": "1000.x", "client_secret": "sec", "refresh_token": "1000.a.b",
         "data_center": "eu"}
    f.update(over)
    return f


def test_zoho_verify_ok(monkeypatch):
    monkeypatch.setattr("oto.tools.zoho.client.ZohoClient._get_access_token",
                        lambda self: "tok")
    assert zoho._verify(_zoho_fields()) is None  # ne lève pas


def test_zoho_verify_invalid_client_hint(monkeypatch):
    def boom(self):
        raise ValueError("Zoho OAuth error: invalid_client")
    monkeypatch.setattr("oto.tools.zoho.client.ZohoClient._get_access_token", boom)
    with pytest.raises(ValueError) as ei:
        zoho._verify(_zoho_fields())
    assert "data center" in str(ei.value)


def test_zoho_verify_expired_refresh_hint(monkeypatch):
    def boom(self):
        raise ValueError("Zoho OAuth error: invalid_code")
    monkeypatch.setattr("oto.tools.zoho.client.ZohoClient._get_access_token", boom)
    with pytest.raises(ValueError) as ei:
        zoho._verify(_zoho_fields())
    assert "refresh token" in str(ei.value).lower()


def test_zoho_verify_missing_data_center_is_mcperror():
    # data center absent → _resolve_dc_domains lève une McpError (pas le hint), la
    # capacité en extraira le message.
    with pytest.raises(McpError):
        zoho._verify(_zoho_fields(data_center=""))


# --- catalogue : zoho devient verifiable après register -----------------------

def test_catalog_marks_zoho_verifiable():
    connector_verify.register("zoho", zoho._verify)  # register_all le fait au boot
    from oto_mcp.providers import public_catalog
    cat = {c["name"]: c for c in public_catalog()}
    assert cat["zoho"]["verifiable"] is True
    assert cat["serper"]["verifiable"] is False  # pas de sonde
