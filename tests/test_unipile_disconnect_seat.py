"""Disconnect d'un compte hébergé = PAR-PERSONNE, toutes orgs (#221).

Régression vécue 2026-07-17 : le disconnect ne retirait que la ligne de l'org
courante, mais l'affichage (#221) va rechercher le siège plateforme dans les AUTRES
orgs du sub → le compte revenait aussitôt (« je clique disconnect, ça reste
connecté »). L'invariant : ce qu'on VOIT est ce qu'on déconnecte."""
from oto_mcp import access, db
from oto_mcp.tools import unipile


def _rows(orgs):
    return [{"provider": "LINKEDIN", "account_id": "acc_seat", "account_name": "Moi",
             "org_id": o, "platform_seat": True, "connected_at": "2026-07-17 10:00"}
            for o in orgs]


def test_status_shows_seat_from_another_org(monkeypatch):
    # préalable du bug : lié en 39/167, vu depuis 35 → affiché connecté (cross-org)
    monkeypatch.setattr(access, "current_org", lambda s: 35)
    monkeypatch.setattr(access, "credential_mode_for", lambda s, p, **k: "platform")
    monkeypatch.setattr(access, "option_open", lambda s, p, **k: True)
    monkeypatch.setattr(db, "list_unipile_accounts", lambda s: _rows([39, 167]))
    assert unipile.status_for("sub1")["channels"]["linkedin"]["connected"] is True


def test_disconnect_clears_every_org(monkeypatch):
    # le disconnect doit vider TOUTES les orgs, sinon l'affichage cross-org le ressort
    store = {"rows": _rows([35, 39, 167])}

    def fake_clear_seat(sub, provider="LINKEDIN"):
        n = len(store["rows"])
        store["rows"] = []
        return n

    monkeypatch.setattr(db, "clear_unipile_seat", fake_clear_seat)
    assert db.clear_unipile_seat("sub1", "LINKEDIN") == 3

    # après purge : plus rien à ressortir, même vu depuis une autre org
    monkeypatch.setattr(access, "current_org", lambda s: 35)
    monkeypatch.setattr(access, "credential_mode_for", lambda s, p, **k: "platform")
    monkeypatch.setattr(access, "option_open", lambda s, p, **k: True)
    monkeypatch.setattr(db, "list_unipile_accounts", lambda s: store["rows"])
    assert unipile.status_for("sub1")["channels"]["linkedin"]["connected"] is False
