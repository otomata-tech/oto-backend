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


def _wire(monkeypatch, *, byo=False, option=True, org=39, existing=None, count=0,
          limit=None, connected=None):
    monkeypatch.setattr(access, "unipile_api_key_for", lambda sub: "KEY")
    monkeypatch.setattr(access, "credential_mode_for",
                        lambda sub, prov: "org" if byo else "platform")
    monkeypatch.setattr(access, "current_org", lambda sub: org)
    monkeypatch.setattr(access, "has_option", lambda sub, opt: option)
    # Garde-fou anti-doublon cross-org (#172) : comptes déjà connectés du sub, tous
    # canaux/orgs confondus. [] par défaut ⇒ garde-fou inerte (chemins existants).
    monkeypatch.setattr("oto_mcp.db.list_unipile_accounts", lambda sub: connected or [])
    # Adoption (binding-par-org) : siège plateforme du sub dans une autre org, dérivé
    # de `connected` (platform_seat=True seulement — un BYO n'est jamais adoptable).
    def _seat_elsewhere(sub, prov="LINKEDIN", exclude_org=None):
        for a in (connected or []):
            if (a.get("provider") == prov and a.get("org_id") != exclude_org
                    and a.get("platform_seat")):
                return a
        return None
    monkeypatch.setattr("oto_mcp.db.seat_binding_elsewhere", _seat_elsewhere)
    monkeypatch.setattr("oto_mcp.db.set_unipile_account", lambda *a, **k: None)
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


# --- Garde-fou anti-doublon cross-org (#172, piste C) ------------------------

def test_refuses_when_same_channel_connected_in_another_org(monkeypatch):
    # Le sub a déjà LinkedIn connecté dans l'org 2 → connecter dans l'org 39
    # créerait un 2e account_id pour le même login (rotation du cookie). Refus 409.
    _wire(monkeypatch, org=39, connected=[
        {"provider": "LINKEDIN", "account_id": "OLD", "account_name": "laportealexis",
         "org_id": 2}])
    with pytest.raises(ConnectRefused) as e:
        _run(hosted_auth_url("u1", "linkedin"))
    assert e.value.code == "unipile_already_connected_elsewhere"
    assert e.value.status == 409 and "laportealexis" in e.value.message


def test_force_bypasses_cross_org_guard(monkeypatch):
    # force=True honore une reconnexion délibérée (compte réellement distinct).
    _wire(monkeypatch, org=39, connected=[
        {"provider": "LINKEDIN", "account_id": "OLD", "org_id": 2}])
    out = _run(hosted_auth_url("u1", "linkedin", force=True))
    assert out["url"]


def test_same_org_reconnect_not_blocked_by_guard(monkeypatch):
    # Un compte du MÊME canal dans l'org de contexte = remplacement, pas un doublon.
    _wire(monkeypatch, org=39, existing={"account_id": "A1"}, connected=[
        {"provider": "LINKEDIN", "account_id": "A1", "org_id": 39}])
    out = _run(hosted_auth_url("u1", "linkedin"))
    assert out["url"]


def test_guard_channel_scoped(monkeypatch):
    # LinkedIn connecté ailleurs ne bloque pas la connexion d'un canal DIFFÉRENT.
    _wire(monkeypatch, org=39, connected=[
        {"provider": "LINKEDIN", "account_id": "OLD", "org_id": 2}])
    out = _run(hosted_auth_url("u1", "whatsapp"))
    assert out["url"]


# --- #237 : premium reconnecte le siège existant, MÊME avec force=true --------

def test_premium_reconnects_existing_seat_even_with_force(monkeypatch):
    # L'agent passe force=true POUR dépasser l'anti-doublon (compte déjà connecté) →
    # ajouter Recruiter doit RECONNECTER le siège (rattache le produit), pas créer un
    # 2e compte (type=create qui perdait le premium et donnait le 403 Recruiter).
    _wire(monkeypatch, org=39, connected=[
        {"provider": "LINKEDIN", "account_id": "SEAT1", "org_id": 39,
         "platform_seat": True}])
    _run(hosted_auth_url("u1", "linkedin", force=True, premium="recruiter"))
    assert _FakeClient.last_kwargs["reconnect_account"] == "SEAT1"
    assert _FakeClient.last_kwargs["premium"] == "recruiter"


def test_plain_force_still_creates_new_account(monkeypatch):
    # Sans premium, force=true reste un create (compte réellement neuf) : pas de reconnect.
    _wire(monkeypatch, org=39, connected=[
        {"provider": "LINKEDIN", "account_id": "OLD", "org_id": 2, "platform_seat": True}])
    _run(hosted_auth_url("u1", "linkedin", force=True))
    assert _FakeClient.last_kwargs["reconnect_account"] is None
