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
    def probe(fields, config=None):  # noqa: ARG001
        return None
    connector_verify.register("_ut_x", probe)
    assert connector_verify.supports("_ut_x")
    assert connector_verify.probe_for("_ut_x") is probe
    assert not connector_verify.supports("_ut_absent")
    assert connector_verify.probe_for("_ut_absent") is None


# --- capacité : ok / échec / non supporté / message McpError ------------------

def _ctx():
    return ResolvedCtx(sub="user-1", org_id=42)


def _run(inp, *, fields=None, config=None, monkeypatch=None):
    """Exécute le handler avec _fields_and_config_for court-circuité (level=auto)."""
    if fields is not None:
        monkeypatch.setattr(
            cv.access, "resolve_credential",
            lambda *a, **k: types.SimpleNamespace(fields=fields, config=config or {}),
        )
    return asyncio.run(cv._verify(_ctx(), inp))


def test_verify_ok(monkeypatch):
    connector_verify.register("_ut_ok", lambda fields, config=None: None)
    res = _run(cv.VerifyInput(provider="_ut_ok"), fields={"k": "v"}, monkeypatch=monkeypatch)
    assert res["ok"] is True
    assert res["provider"] == "_ut_ok"
    assert "elapsed_ms" in res


def test_verify_threads_config_to_probe(monkeypatch):
    """La config non-secrète (dsn) est passée à la sonde (#194) : une sonde vers un
    endpoint dont l'hôte dépend de la clé (unipile, tenant BYO) doit la lire, sinon
    elle teste la clé contre le mauvais tenant."""
    seen = {}

    def probe(fields, config=None):  # noqa: ARG001
        seen["config"] = config
    connector_verify.register("_ut_cfg", probe)
    res = _run(cv.VerifyInput(provider="_ut_cfg"), fields={"k": "v"},
               config={"dsn": "api.unipile.com"},
               monkeypatch=monkeypatch)
    assert res["ok"] is True
    assert seen["config"] == {"dsn": "api.unipile.com"}


def test_verify_failure_returns_error_not_500(monkeypatch):
    def bad(fields, config=None):  # noqa: ARG001
        raise ValueError("clé refusée par le provider")
    connector_verify.register("_ut_bad", bad)
    res = _run(cv.VerifyInput(provider="_ut_bad"), fields={"k": "v"}, monkeypatch=monkeypatch)
    assert res["ok"] is False
    assert res["error"] == "clé refusée par le provider"


def test_verify_mcperror_message_is_extracted(monkeypatch):
    def bad(fields, config=None):  # noqa: ARG001
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
    async def aprobe(fields, config=None):  # noqa: ARG001
        return None
    connector_verify.register("_ut_async", aprobe)
    res = _run(cv.VerifyInput(provider="_ut_async"), fields={"k": "v"}, monkeypatch=monkeypatch)
    assert res["ok"] is True


def test_verify_records_health_on_member_key(monkeypatch):
    """La sonde alimente le flag santé (`meta.health_ko`) de la clé MEMBRE : échec → KO
    + raison, succès → rétabli. Lu ensuite par status_for, rendu terra au verdict."""
    written = {}
    monkeypatch.setattr(cv.access, "resolve_credential",
                        lambda *a, **k: types.SimpleNamespace(fields={"k": "v"}, config={}, mode="user"))
    monkeypatch.setattr(cv.credentials_store, "member_id", lambda org, sub: f"{org}:{sub}")
    monkeypatch.setattr(cv.credentials_store, "update_meta",
                        lambda et, eid, conn, acct, patch: written.update(scope=(et, eid), patch=patch) or True)

    def bad(fields, config=None):  # noqa: ARG001
        raise ValueError("session expirée")
    connector_verify.register("_ut_health_ko", bad)
    asyncio.run(cv._verify(_ctx(), cv.VerifyInput(provider="_ut_health_ko")))
    assert written["scope"] == ("member", "42:user-1")
    assert written["patch"]["health_ko"] is True
    assert "session" in (written["patch"]["health_reason"] or "")

    written.clear()
    connector_verify.register("_ut_health_ok", lambda f, config=None: None)  # noqa: ARG005
    asyncio.run(cv._verify(_ctx(), cv.VerifyInput(provider="_ut_health_ok")))
    assert written["patch"]["health_ko"] is False


# --- M4 : « Effet pour : [membre] » (org admin rejoue le verdict d'un membre) ---------

def test_effect_for_member_replays_member_status(monkeypatch):
    monkeypatch.setattr("oto_mcp.roles.is_org_member", lambda sub, org: sub == "m1" and org == 42)
    seen = {}
    monkeypatch.setattr(cv.access, "status_for",
                        lambda sub, org=None: seen.update(sub=sub, org=org)
                        or {"providers": {"zoho": {"mode": "user", "health_ko": True}}})
    out = cv._effect_for_member(cv.ResolvedCtx(sub="admin", org_id=42),
                                cv.EffectForMemberInput(provider="zoho", member="m1"))
    # org passée EXPLICITEMENT (ADR 0023), statut du MEMBRE (pas de l'admin).
    assert seen == {"sub": "m1", "org": 42}
    assert out["member"] == "m1" and out["status"]["health_ko"] is True


def test_effect_for_member_rejects_non_member(monkeypatch):
    monkeypatch.setattr("oto_mcp.roles.is_org_member", lambda sub, org: False)
    with pytest.raises(AuthzDenied) as ei:
        cv._effect_for_member(cv.ResolvedCtx(sub="admin", org_id=42),
                              cv.EffectForMemberInput(provider="zoho", member="x"))
    assert ei.value.code == "not_a_member"


# --- sonde Zoho : refresh token, message actionnable --------------------------

def _zoho_fields(**over):
    f = {"client_id": "1000.x", "client_secret": "sec", "refresh_token": "1000.a.b",
         "data_center": "eu"}
    f.update(over)
    return f


class _FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _patch_zoho(monkeypatch, *, refresh, reads=None):
    """refresh = corps JSON du POST token. reads = suite de résultats pour les appels
    `ZohoClient.list_records` : soit un dict (succès), soit une str (message d'exception,
    ex. 'OAUTH_SCOPE_MISMATCH'). Épuisée → dernier résultat répété."""
    monkeypatch.setattr(zoho.requests, "post",
                        lambda *a, **k: _FakeResp(200, refresh))
    seq = list(reads or [])

    def _list_records(self, module, *a, **k):
        res = seq.pop(0) if seq else (reads[-1] if reads else {"data": []})
        if isinstance(res, str):
            raise RuntimeError(f"zoho HTTP 401: {{'code': '{res}'}}")
        return res

    monkeypatch.setattr("oto.tools.zoho.client.ZohoClient.list_records", _list_records)


def test_zoho_verify_ok(monkeypatch):
    # auth OK + 1er module lisible → passe.
    _patch_zoho(monkeypatch, refresh={"access_token": "t", "scope": "ZohoCRM.modules.ALL"},
                reads=[{"data": [{"id": "1"}]}])
    assert zoho._verify(_zoho_fields()) is None


def test_zoho_verify_ok_empty_module(monkeypatch):
    # module vide (0 record) mais scope présent → utilisable.
    _patch_zoho(monkeypatch, refresh={"access_token": "t"}, reads=[{"data": []}])
    assert zoho._verify(_zoho_fields()) is None


def test_zoho_verify_ok_second_module(monkeypatch):
    # 1er module en scope-mismatch mais 2e lisible → passe (scope partiel suffit).
    _patch_zoho(monkeypatch, refresh={"access_token": "t"},
                reads=["OAUTH_SCOPE_MISMATCH", {"data": []}])
    assert zoho._verify(_zoho_fields()) is None


def test_zoho_verify_invalid_client_hint(monkeypatch):
    _patch_zoho(monkeypatch, refresh={"error": "invalid_client"})
    with pytest.raises(ValueError) as ei:
        zoho._verify(_zoho_fields())
    assert "data center" in str(ei.value)


def test_zoho_verify_expired_refresh_hint(monkeypatch):
    _patch_zoho(monkeypatch, refresh={"error": "invalid_code"})
    with pytest.raises(ValueError) as ei:
        zoho._verify(_zoho_fields())
    assert "refresh token" in str(ei.value).lower()


def test_zoho_verify_scope_mismatch_reports_granted_scope(monkeypatch):
    # LE cas vécu : clé Analytics posée sur le CRM — auth OK, aucun scope CRM.
    _patch_zoho(monkeypatch,
                refresh={"access_token": "t", "scope": "ZohoAnalytics.fullaccess.all"},
                reads=["OAUTH_SCOPE_MISMATCH"] * 4)
    with pytest.raises(ValueError) as ei:
        zoho._verify(_zoho_fields())
    msg = str(ei.value)
    assert "aucun scope de lecture CRM" in msg
    assert "ZohoAnalytics.fullaccess.all" in msg  # scope réel remonté = diagnostic direct


def test_zoho_verify_missing_data_center_is_mcperror():
    # data center absent → _resolve_dc_domains lève une McpError (avant tout HTTP), la
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
