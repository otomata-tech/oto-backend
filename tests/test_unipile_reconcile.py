"""Réconciliation poll-and-bind Unipile (webhook hosted-auth v2 non livré).

Verrouille : on lie le compte le plus RÉCENT, NON déjà lié, du bon provider, créé
APRÈS le pending du sub (le floor évite de rebinder un siège pré-existant)."""
import types
from datetime import datetime, timezone

from oto_mcp import unipile_connect as uc

PEND_TS = datetime(2026, 7, 16, 12, 41, tzinfo=timezone.utc)


def _pend(nonce="N", org=2, provider="LINKEDIN", seat=True, ts=PEND_TS):
    return {"nonce": nonce, "org_id": org, "provider": provider,
            "platform_seat": seat, "created_at": ts}


def _acc(aid, name, provider="linkedin", created="2026-07-16 12:45:00+00"):
    return {"id": aid, "name": name, "provider": provider, "created_at": created}


def _setup(monkeypatch, pendings, accounts, bound=None, dead=None, alive_ids=None):
    monkeypatch.setattr(uc.db, "list_unipile_pending_for_sub", lambda s: pendings)
    monkeypatch.setattr(uc.db, "bound_unipile_account_ids", lambda: set(bound or []))
    monkeypatch.setattr(uc.db, "dead_unipile_account_ids_for",
                        lambda s, p="LINKEDIN": set(dead or []))
    monkeypatch.setattr(uc.access, "resolve_credential",
                        lambda *a, **k: types.SimpleNamespace(key="K", is_platform=True, config={}))
    import oto.tools.unipile as core
    # account_alive : par défaut TOUS vivants ; `alive_ids` restreint à ceux-là
    alive = (lambda aid: aid in alive_ids) if alive_ids is not None else (lambda aid: True)
    monkeypatch.setattr(core, "make_unipile_client",
                        lambda **k: types.SimpleNamespace(
                            list_accounts=lambda: accounts, account_alive=alive))
    calls = {"set": [], "resolved": []}
    monkeypatch.setattr(uc.db, "set_unipile_account",
                        lambda *a, **k: calls["set"].append((a, k)))
    monkeypatch.setattr(uc.db, "resolve_unipile_pending",
                        lambda n: calls["resolved"].append(n))
    return calls


def test_binds_newest_after_pending(monkeypatch):
    accounts = [_acc("acc_old", "Seat", created="2026-07-16 11:00:00+00"),
                _acc("acc_new", "Me", created="2026-07-16 12:45:00+00")]
    calls = _setup(monkeypatch, [_pend()], accounts)
    out = uc.reconcile_pending("sub1")
    assert out["bound"] is True
    assert out["accounts"][0]["account_id"] == "acc_new"
    assert calls["set"][0][0][:2] == ("sub1", "acc_new")  # (sub, account_id)
    assert calls["resolved"] == ["N"]


def test_excludes_already_bound(monkeypatch):
    calls = _setup(monkeypatch, [_pend()], [_acc("acc_new", "Me")], bound={"acc_new"})
    out = uc.reconcile_pending("sub1")
    assert out["bound"] is False and calls["set"] == []


def test_skips_dead_session_prefers_alive(monkeypatch):
    # deux candidats après le pending : le plus récent est MORT (401) → on prend le vivant
    accounts = [_acc("acc_alive", "Sain", created="2026-07-16 12:45:00+00"),
                _acc("acc_dead", "MortNé", created="2026-07-16 12:50:00+00")]
    calls = _setup(monkeypatch, [_pend()], accounts, alive_ids={"acc_alive"})
    out = uc.reconcile_pending("sub1")
    assert out["accounts"][0]["account_id"] == "acc_alive"


def test_binds_nothing_when_all_dead(monkeypatch):
    accounts = [_acc("acc_dead", "MortNé", created="2026-07-16 12:50:00+00")]
    calls = _setup(monkeypatch, [_pend()], accounts, alive_ids=set())
    out = uc.reconcile_pending("sub1")
    assert out["bound"] is False and calls["set"] == []


def test_excludes_account_before_floor(monkeypatch):
    # seul compte dispo est ANTÉRIEUR au pending (>5 min) → jamais rebindé (siège tiers)
    calls = _setup(monkeypatch, [_pend()], [_acc("acc_old", "Seat", created="2026-07-16 11:00:00+00")])
    out = uc.reconcile_pending("sub1")
    assert out["bound"] is False


def test_rebinds_own_dead_account_despite_floor(monkeypatch):
    # reconnexion : Unipile RÉUTILISE le compte (antérieur au pending) — la ligne
    # soft-déconnectée du sub est la preuve de propriété → rebind déterministe,
    # même si l'account_id figure dans bound (les morts y sont, anti-tiers).
    calls = _setup(monkeypatch, [_pend()],
                   [_acc("acc_mine", "Moi", created="2026-07-16 11:00:00+00")],
                   bound={"acc_mine"}, dead={"acc_mine"})
    out = uc.reconcile_pending("sub1")
    assert out["bound"] is True
    assert out["accounts"][0]["account_id"] == "acc_mine"


def test_never_rebinds_dead_account_of_third_party(monkeypatch):
    # ligne morte d'un TIERS (dans bound, pas dans MES morts) + antérieure → intouchable
    calls = _setup(monkeypatch, [_pend()],
                   [_acc("acc_tiers", "Autre", created="2026-07-16 11:00:00+00")],
                   bound={"acc_tiers"}, dead=set())
    assert uc.reconcile_pending("sub1")["bound"] is False


def test_provider_mismatch_ignored(monkeypatch):
    calls = _setup(monkeypatch, [_pend(provider="LINKEDIN")],
                   [_acc("acc_wa", "WA", provider="whatsapp")])
    out = uc.reconcile_pending("sub1")
    assert out["bound"] is False


def test_no_pending_is_noop(monkeypatch):
    _setup(monkeypatch, [], [])
    assert uc.reconcile_pending("sub1") == {"bound": False, "accounts": []}
