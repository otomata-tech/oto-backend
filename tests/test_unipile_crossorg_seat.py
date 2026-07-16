"""#221 — un siège de la clé plateforme (compte hébergé par-personne) suit le sub
cross-org, MAIS seulement si l'org de contexte résout via la clé plateforme."""
from oto_mcp import connector_identities as ci, access, db, connectors
from oto_mcp.tools import unipile

_ROWS = [{"provider": "LINKEDIN", "account_id": "acc_seat", "account_name": "X",
          "org_id": 35, "platform_seat": True, "connected_at": "2026-07-16 20:00"}]


def _base(monkeypatch, mode, cur_org_acc=None):
    monkeypatch.setattr(access, "current_org", lambda s: 168)           # active ailleurs
    monkeypatch.setattr(db, "get_unipile_account_id", lambda s, o, p: cur_org_acc)
    monkeypatch.setattr(connectors, "is_personal_cross_org", lambda p: True)
    monkeypatch.setattr(access, "personal_instance_org",
                        lambda s, p, exclude_org=None: None)             # pas de clé membre
    monkeypatch.setattr(access, "credential_mode_for", lambda s, p, **k: mode)
    monkeypatch.setattr(db, "any_unipile_account_id",
                        lambda s, p="LINKEDIN": "acc_seat")


def test_resolve_platform_seat_cross_org(monkeypatch):
    _base(monkeypatch, mode="platform")
    assert ci._own_unipile_account_id("sub1", "LINKEDIN") == "acc_seat"


def test_resolve_gated_on_platform_mode(monkeypatch):
    _base(monkeypatch, mode="org")  # clé BYO d'org → pas de cross-org du siège plateforme
    assert ci._own_unipile_account_id("sub1", "LINKEDIN") is None


def test_resolve_current_org_wins(monkeypatch):
    _base(monkeypatch, mode="platform", cur_org_acc="acc_here")
    assert ci._own_unipile_account_id("sub1", "LINKEDIN") == "acc_here"


def test_status_shows_seat_cross_org(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda s: 168)
    monkeypatch.setattr(access, "credential_mode_for", lambda s, p, **k: "platform")
    monkeypatch.setattr(access, "option_open", lambda s, p, **k: True)
    monkeypatch.setattr(db, "list_unipile_accounts", lambda s: _ROWS)
    ln = unipile.status_for("sub1")["channels"]["linkedin"]
    assert ln["connected"] is True and ln["account_id"] == "acc_seat"


def test_status_no_seat_when_byo_mode(monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda s: 168)
    monkeypatch.setattr(access, "credential_mode_for", lambda s, p, **k: "org")
    monkeypatch.setattr(access, "option_open", lambda s, p, **k: True)
    monkeypatch.setattr(db, "list_unipile_accounts", lambda s: _ROWS)
    assert unipile.status_for("sub1")["channels"]["linkedin"]["connected"] is False


def test_status_no_seat_when_not_subscribed(monkeypatch):
    # platform mode mais option fermée → pas de faux « connecté » (carte cohérente)
    monkeypatch.setattr(access, "current_org", lambda s: 43)
    monkeypatch.setattr(access, "credential_mode_for", lambda s, p, **k: "platform")
    monkeypatch.setattr(access, "option_open", lambda s, p, **k: False)
    monkeypatch.setattr(db, "list_unipile_accounts", lambda s: _ROWS)
    assert unipile.status_for("sub1")["channels"]["linkedin"]["connected"] is False
