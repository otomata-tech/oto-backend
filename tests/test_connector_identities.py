"""Sélecteur d'identité générique (ADR 0024) : registre par-connecteur.
Google = comptes du coffre ; Unipile = identités distantes d'une clé BYO (validation
id ∈ liste = anti-binding ; BYO-only) ; Pennylane GED = backend enregistré par le
module tools (async, sociétés du cabinet = GED cibles, défaut au meta du credential)."""
import asyncio

import pytest

from oto_mcp import access, browserbase, connector_identities, credentials_store
from oto_mcp.access import ResolvedCredential


class _FakeUnipile:
    ACCOUNTS = [
        {"id": "A1", "name": "Alexandra El Hachem", "type": "LINKEDIN",
         "sources": [{"status": "OK"}]},
        {"id": "A2", "name": "Laurent Guy", "type": "LINKEDIN", "sources": [{"status": "OK"}]},
    ]

    def __init__(self, api_key=None, dsn=None, **k):
        self.api_key, self.dsn = api_key, dsn

    def list_accounts(self):
        return self.ACCOUNTS


# --- Google -----------------------------------------------------------------

def test_google_list_and_select(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda sub: 39)
    monkeypatch.setattr("oto_mcp.google_oauth.list_accounts",
                        lambda sub: [{"google_email": "a@x.io", "is_default": True},
                                     {"google_email": "b@x.io", "is_default": False}])
    ids = connector_identities.list_identities("u1", "google")
    assert {i["id"] for i in ids} == {"a@x.io", "b@x.io"}
    assert next(i for i in ids if i["id"] == "a@x.io")["is_default"]

    called = {}
    monkeypatch.setattr("oto_mcp.db.set_default_google_account",
                        lambda sub, org, acc: called.update(acc=acc, org=org) or True)
    connector_identities.select_identity("u1", "google", "b@x.io")
    assert called["acc"] == "b@x.io"


def test_google_select_unknown_raises(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda sub: 39)
    monkeypatch.setattr("oto_mcp.db.set_default_google_account", lambda sub, org, acc: False)
    with pytest.raises(ValueError):
        connector_identities.select_identity("u1", "google", "ghost@x.io")


# --- Unipile (BYO) ----------------------------------------------------------

def _wire_byo(monkeypatch, mode="org"):
    monkeypatch.setattr(access, "credential_mode_for", lambda sub, prov: mode)
    monkeypatch.setattr(access, "resolve_credential",
                        lambda prov, want="auto", sub=None: ResolvedCredential(
                            "unipile", "KEY", False, "org", "org", "39"))
    # config.dsn lu par _unipile_client
    monkeypatch.setattr(access.credentials_store, "get_credential_with_meta",
                        lambda *a, **k: {"meta": {"dsn": "api6.unipile.com:13616"}})
    monkeypatch.setattr("oto.tools.unipile.UnipileClient", _FakeUnipile)
    monkeypatch.setattr(access, "current_org", lambda sub: 39)
    # #55 : par défaut, pas de grant reçu ni de pointeur « identité opérée ».
    monkeypatch.setattr("oto_mcp.db.list_account_grants_to", lambda sub: [])
    monkeypatch.setattr("oto_mcp.db.get_operated_account", lambda sub, prov: None)
    monkeypatch.setattr("oto_mcp.db.granted_accounts_for", lambda sub, prov: {})
    monkeypatch.setattr("oto_mcp.db.list_unipile_accounts", lambda sub: [])
    monkeypatch.setattr("oto_mcp.db.clear_operated_account", lambda sub, prov: None)


def test_unipile_list_byo(monkeypatch):
    _wire_byo(monkeypatch)
    monkeypatch.setattr("oto_mcp.db.get_unipile_account_id", lambda sub, org, ch: "A2")
    ids = connector_identities.list_identities("u1", "unipile")
    assert {i["id"] for i in ids} == {"A1", "A2"}
    a2 = next(i for i in ids if i["id"] == "A2")
    assert a2["is_default"] and a2["channel"] == "LINKEDIN" and a2["label"] == "Laurent Guy"


def test_unipile_select_valid(monkeypatch):
    _wire_byo(monkeypatch)
    saved = {}
    monkeypatch.setattr("oto_mcp.db.set_unipile_account",
                        lambda sub, aid, name, org_id=None, provider="LINKEDIN",
                               platform_seat=False:
                        saved.update(aid=aid, name=name, provider=provider,
                                     org_id=org_id, platform_seat=platform_seat))
    res = connector_identities.select_identity("u1", "unipile", "A1")
    assert res["id"] == "A1" and res["channel"] == "LINKEDIN"
    # Scope membre (ADR 0033 B4) : le binding est rattaché à l'org de contexte,
    # BYO = pas un siège plateforme.
    assert saved == {"aid": "A1", "name": "Alexandra El Hachem",
                     "provider": "LINKEDIN", "org_id": 39, "platform_seat": False}


def test_unipile_select_unknown_id_raises(monkeypatch):
    _wire_byo(monkeypatch)
    monkeypatch.setattr("oto_mcp.db.set_unipile_account",
                        lambda *a, **k: pytest.fail("ne doit pas écrire un id inconnu"))
    with pytest.raises(ValueError, match="inconnu"):
        connector_identities.select_identity("u1", "unipile", "GHOST")


def test_unipile_platform_no_selector(monkeypatch):
    # clé plateforme (revente) SANS grant → liste vide, sélection refusée
    # (hosted-auth conservé, widget strictement inchangé).
    monkeypatch.setattr(access, "credential_mode_for", lambda sub, prov: "platform")
    monkeypatch.setattr("oto_mcp.db.list_account_grants_to", lambda sub: [])
    monkeypatch.setattr("oto_mcp.db.list_unipile_accounts", lambda sub: [])
    assert connector_identities.list_identities("u1", "unipile") == []
    with pytest.raises(ValueError, match="plateforme"):
        connector_identities.select_identity("u1", "unipile", "A1")


# --- Pennylane GED (backend enregistré par tools/pennylaneged, async) ---------
# L'identité = la société cliente du cabinet (= SA GED, une par client, issue #31).

_COMPANIES = [
    {"id": 239568, "name": "Fidens", "client_code": "C042"},
    {"id": 111, "name": "Autre SARL", "client_code": None},
]


def _wire_ged(monkeypatch, *, eval_result, meta=None):
    """Câble une session pennylaneged connectée + une réponse d'éval in-page."""
    import oto_mcp.tools.pennylaneged  # noqa: F401 — déclenche register()

    monkeypatch.setattr(browserbase, "is_configured", lambda: True)
    monkeypatch.setattr(
        access, "resolve_credential",
        lambda prov, want="auto", sub=None, emit_on_failure=True:
        ResolvedCredential("pennylaneged", "CTX1", False, "user", "user", "u1"))
    monkeypatch.setattr(credentials_store, "credential_status",
                        lambda *a, **k: {"set_at": "2026-07-01", "meta": meta or {}})

    calls = {}

    async def fake_eval(ctx_id, app, js, arg=None):
        calls.update(ctx_id=ctx_id, app=app, arg=arg)
        return eval_result

    monkeypatch.setattr(browserbase, "run_page_eval", fake_eval)
    return calls


def test_pennylaneged_list(monkeypatch):
    _wire_ged(monkeypatch, eval_result={"status": 200, "companies": _COMPANIES},
              meta={"default_identity_id": "239568", "default_identity_label": "Fidens"})
    ids = asyncio.run(connector_identities.list_identities("u1", "pennylaneged"))
    assert [i["id"] for i in ids] == ["239568", "111"]
    fidens = ids[0]
    assert fidens["is_default"] and fidens["label"] == "Fidens (C042)"
    assert not ids[1]["is_default"] and ids[1]["label"] == "Autre SARL"


def test_pennylaneged_list_not_connected(monkeypatch):
    import oto_mcp.tools.pennylaneged  # noqa: F401

    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData, INVALID_PARAMS

    def boom(*a, **k):
        raise McpError(ErrorData(code=INVALID_PARAMS, message="non connecté"))

    monkeypatch.setattr(browserbase, "is_configured", lambda: True)
    monkeypatch.setattr(access, "resolve_credential", boom)
    assert asyncio.run(connector_identities.list_identities("u1", "pennylaneged")) == []


def test_pennylaneged_select_valid(monkeypatch):
    calls = _wire_ged(monkeypatch, eval_result={"status": 200, "label": "Fidens"})
    saved = {}
    monkeypatch.setattr(credentials_store, "update_meta",
                        lambda et, eid, conn, acct, patch: saved.update(
                            et=et, eid=eid, conn=conn, acct=acct, patch=patch) or True)
    res = asyncio.run(connector_identities.select_identity("u1", "pennylaneged", "239568"))
    assert res["id"] == "239568" and res["is_default"] and res["label"] == "Fidens"
    assert saved["et"] == "user" and saved["eid"] == "u1" and saved["conn"] == "pennylaneged"
    assert saved["patch"] == {"default_identity_id": "239568",
                              "default_identity_label": "Fidens"}
    # la vérif anti-binding a bien visé LA société (page DMS + arg cid)
    assert calls["arg"]["cid"] == 239568 and "/239568/" in calls["app"]


def test_pennylaneged_select_inaccessible_raises(monkeypatch):
    # GED d'une société hors du cabinet (tree ≠ 200) → refus, rien n'est écrit.
    _wire_ged(monkeypatch, eval_result={"status": 404, "label": None})
    monkeypatch.setattr(credentials_store, "update_meta",
                        lambda *a, **k: pytest.fail("ne doit pas mémoriser un id refusé"))
    with pytest.raises(ValueError, match="inaccessible"):
        asyncio.run(connector_identities.select_identity("u1", "pennylaneged", "999"))


def test_pennylaneged_select_bad_id_raises(monkeypatch):
    import oto_mcp.tools.pennylaneged  # noqa: F401

    with pytest.raises(ValueError, match="invalide"):
        asyncio.run(connector_identities.select_identity("u1", "pennylaneged", "abc"))
