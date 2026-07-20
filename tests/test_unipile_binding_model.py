"""Modèle binding-par-org des comptes hébergés (remplace le fallback cross-org #221).

Le binding est un ACTE par org : `channels` ne montre que les bindings VIVANTS de
l'org courante ; un siège plateforme connecté ailleurs se PROPOSE (`elsewhere`) et
s'adopte au connect (écriture explicite du binding) — jamais de fallback silencieux
(il rendait le disconnect incohérent : retiré ici, ré-affiché via une autre org).
Le disconnect est SOFT et par-org : la ligne survit (preuve de propriété → rebind
déterministe à la reconnexion, Unipile réutilisant le même account_id)."""
import asyncio

import pytest

from oto_mcp import access, connector_identities as ci, connectors, db, unipile_connect as uc
from oto_mcp.tools import unipile

SEAT = {"provider": "LINKEDIN", "account_id": "acc_seat", "account_name": "Moi",
        "org_id": 35, "platform_seat": True, "connected_at": "2026-07-17 10:00"}


def _status_env(monkeypatch, *, org=168, mode="platform", subscribed=True, rows=None):
    monkeypatch.setattr(access, "current_org", lambda s: org)
    monkeypatch.setattr(access, "credential_mode_for", lambda s, p, **k: mode)
    monkeypatch.setattr(access, "option_open", lambda s, p, **k: subscribed)
    monkeypatch.setattr(db, "list_unipile_accounts", lambda s: rows or [])


# ---- status : proposition, jamais décision --------------------------------

def test_status_not_connected_but_proposes_elsewhere(monkeypatch):
    _status_env(monkeypatch, rows=[SEAT])
    st = unipile.status_for("sub1")
    assert st["channels"]["linkedin"]["connected"] is False  # pas lié ICI
    assert st["elsewhere"]["linkedin"]["account_id"] == "acc_seat"
    assert st["elsewhere"]["linkedin"]["org_id"] == 35


def test_status_no_proposal_without_option(monkeypatch):
    _status_env(monkeypatch, subscribed=False, rows=[SEAT])
    assert unipile.status_for("sub1")["elsewhere"] == {}


def test_status_no_proposal_under_byo_key(monkeypatch):
    # une autre clé résout ici → l'account_id d'ailleurs n'y existe pas
    _status_env(monkeypatch, mode="org", rows=[SEAT])
    assert unipile.status_for("sub1")["elsewhere"] == {}


def test_status_bound_here_wins_over_elsewhere(monkeypatch):
    here = {**SEAT, "org_id": 168, "account_id": "acc_here"}
    _status_env(monkeypatch, rows=[here, SEAT])
    st = unipile.status_for("sub1")
    assert st["channels"]["linkedin"]["account_id"] == "acc_here"
    assert st["elsewhere"] == {}


# ---- résolution d'exécution : binding de l'org courante, sans fallback ----

def test_own_account_no_platform_fallback(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda s: 168)
    monkeypatch.setattr(db, "get_unipile_account_id", lambda s, o, p: None)
    monkeypatch.setattr(connectors, "is_personal_cross_org", lambda p: True)
    monkeypatch.setattr(access, "personal_instance_org",
                        lambda s, p, exclude_org=None: None)
    assert ci._own_unipile_account_id("sub1", "LINKEDIN") is None


# ---- adoption au connect --------------------------------------------------

def _connect_env(monkeypatch, *, seat_elsewhere=SEAT, byo_rows=None, alive=True,
                 link_calls=None):
    monkeypatch.setattr(access, "unipile_api_key_for", lambda s: "K")
    monkeypatch.setattr(access, "credential_mode_for", lambda s, p, **k: "platform")
    monkeypatch.setattr(access, "current_org", lambda s: 168)
    monkeypatch.setattr(access, "has_option", lambda s, o, **k: True)
    monkeypatch.setattr(db, "get_unipile_account", lambda s, o, p: None)
    monkeypatch.setattr(db, "get_org_unipile_limit", lambda o: None)
    monkeypatch.setattr(db, "count_unipile_accounts_for_org", lambda o: 0)
    monkeypatch.setattr(db, "seat_binding_elsewhere",
                        lambda s, p, exclude_org=None: seat_elsewhere)
    monkeypatch.setattr(db, "list_unipile_accounts", lambda s: byo_rows or [])
    monkeypatch.setattr(db, "create_unipile_pending", lambda *a, **k: None)
    # Client hébergé stubé : `account_alive` gouverne l'adoption (vivant) vs la
    # reconnexion (mort) ; `hosted_auth_link` enregistre ses kwargs (reconnect_account).
    def _link(self, **kw):
        if link_calls is not None:
            link_calls.append(kw)
        return "https://auth.unipile.com/?t=x"
    import oto.tools.unipile as core
    monkeypatch.setattr(core, "make_unipile_client", lambda **k: type(
        "C", (), {"account_alive": lambda self, aid: alive, "hosted_auth_link": _link})())
    written = []
    monkeypatch.setattr(db, "set_unipile_account",
                        lambda *a, **k: written.append((a, k)))
    return written


def test_connect_adopts_seat_from_other_org(monkeypatch):
    written = _connect_env(monkeypatch)
    out = asyncio.run(uc.hosted_auth_url("sub1", "linkedin"))
    assert out["adopted"] is True and out["account_name"] == "Moi"
    (args, kwargs) = written[0]
    assert args[:2] == ("sub1", "acc_seat") and kwargs["org_id"] == 168


def test_connect_dead_seat_reconnects_not_adopt(monkeypatch):
    # Siège d'une autre org MORT (401) → PAS d'adoption du cadavre : wizard de
    # RECONNEXION du même account_id (type=reconnect, pas un doublon). Vécu Alexandra.
    links = []
    written = _connect_env(monkeypatch, alive=False, link_calls=links)
    out = asyncio.run(uc.hosted_auth_url("sub1", "linkedin"))
    assert "url" in out and "adopted" not in out  # login, pas ré-adoption
    assert written == []                            # aucun binding vers le mort
    assert links[0]["reconnect_account"] == "acc_seat"  # reconnecte CE compte


def test_connect_premium_skips_adoption(monkeypatch):
    # demander un produit premium = reconnexion volontaire → wizard, pas d'adoption
    written = _connect_env(monkeypatch)
    monkeypatch.setattr(db, "create_unipile_pending", lambda *a, **k: None)
    import oto.tools.unipile as core
    monkeypatch.setattr(core, "make_unipile_client", lambda **k: type(
        "C", (), {"hosted_auth_link": lambda self, **kw: "https://auth.unipile.com/?t=x"})())
    out = asyncio.run(uc.hosted_auth_url("sub1", "linkedin", premium="recruiter"))
    assert "url" in out and written == []


def test_connect_byo_elsewhere_still_409(monkeypatch):
    byo = {**SEAT, "platform_seat": False}
    _connect_env(monkeypatch, seat_elsewhere=None, byo_rows=[byo])
    with pytest.raises(uc.ConnectRefused) as e:
        asyncio.run(uc.hosted_auth_url("sub1", "linkedin"))
    assert e.value.code == "unipile_already_connected_elsewhere"
