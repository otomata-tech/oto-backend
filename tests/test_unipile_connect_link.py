"""Corps partagé du hosted-auth Unipile (`unipile_connect.hosted_auth_url`,
feedback #131) : gates (canal, clé, org, option, plafond) en ConnectRefused
typée, happy path = nonce posé + lien Unipile rendu. Clients/DB stubés."""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp import access, unipile_connect
from oto_mcp.unipile_connect import ConnectRefused, hosted_auth_url


class _FakeClient:
    def __init__(self, api_key=None, dsn=None, **k):
        self.api_key, self.dsn = api_key, dsn

    def hosted_auth_link(self, **kwargs):
        _FakeClient.last_kwargs = kwargs
        return "https://account.unipile.com/auth?token=xyz"


def _wire(monkeypatch, *, byo=False, option=True, org=39, existing=None, count=0, limit=None):
    monkeypatch.setattr(access, "unipile_api_key_for", lambda sub: "KEY")
    monkeypatch.setattr(access, "credential_mode_for",
                        lambda sub, prov: "org" if byo else "platform")
    monkeypatch.setattr(access, "current_org", lambda sub: org)
    monkeypatch.setattr(access, "has_option", lambda sub, opt: option)
    monkeypatch.setattr("oto_mcp.db.get_unipile_account",
                        lambda sub, org_id, prov: existing)
    monkeypatch.setattr("oto_mcp.db.get_org_unipile_limit", lambda org_id: limit)
    monkeypatch.setattr("oto_mcp.db.count_unipile_accounts_for_org", lambda org_id: count)
    pending = {}
    monkeypatch.setattr("oto_mcp.db.create_unipile_pending",
                        lambda nonce, sub, org_id, prov, platform_seat=False:
                        pending.update(nonce=nonce, sub=sub, org_id=org_id,
                                       provider=prov, platform_seat=platform_seat))
    monkeypatch.setattr("oto.tools.unipile.UnipileClient", _FakeClient)
    return pending


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_happy_path_returns_url_and_poses_nonce(monkeypatch):
    pending = _wire(monkeypatch)
    out = _run(hosted_auth_url("u1", "linkedin"))
    assert out["url"].startswith("https://account.unipile.com/")
    assert out["channel"] == "linkedin"
    # nonce posé pour la corrélation webhook, siège plateforme (mode revente)
    assert pending["provider"] == "LINKEDIN" and pending["platform_seat"] is True
    assert pending["nonce"] == _FakeClient.last_kwargs["name"]
    assert "webhook" in _FakeClient.last_kwargs["notify_url"]


def test_invalid_channel_refused(monkeypatch):
    _wire(monkeypatch)
    with pytest.raises(ConnectRefused) as e:
        _run(hosted_auth_url("u1", "pigeon"))
    assert e.value.code == "invalid_channel" and e.value.status == 400


def test_no_key_refused(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(access, "unipile_api_key_for", lambda sub: None)
    with pytest.raises(ConnectRefused) as e:
        _run(hosted_auth_url("u1"))
    assert e.value.code == "unipile_not_configured" and e.value.status == 404


def test_option_gate_on_platform_key(monkeypatch):
    _wire(monkeypatch, option=False)
    with pytest.raises(ConnectRefused) as e:
        _run(hosted_auth_url("u1"))
    assert e.value.code == "unipile_option_required" and e.value.status == 402


def test_seat_cap_blocks_new_hosted_account(monkeypatch):
    _wire(monkeypatch, existing=None, count=5, limit=5)
    with pytest.raises(ConnectRefused) as e:
        _run(hosted_auth_url("u1"))
    assert e.value.code == "unipile_account_limit_reached" and e.value.status == 429


def test_reconnect_existing_account_bypasses_cap(monkeypatch):
    # un compte déjà connecté = remplacement, pas un nouveau siège
    _wire(monkeypatch, existing={"account_id": "A1"}, count=5, limit=5)
    out = _run(hosted_auth_url("u1"))
    assert out["url"]


def test_byo_skips_option_and_cap(monkeypatch):
    _wire(monkeypatch, byo=True, option=False, count=99, limit=1)
    # resolve_credential (lookup DSN) stubé : la clé BYO porte son dsn
    class _RC:
        config = {"dsn": "api6.unipile.com:13616"}
    monkeypatch.setattr(access, "resolve_credential",
                        lambda prov, want=None, sub=None, emit_on_failure=True: _RC())
    out = _run(hosted_auth_url("u1"))
    assert out["url"]
