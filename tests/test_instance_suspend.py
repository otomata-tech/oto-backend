"""Suspension d'instance (lot 2 / ADR 0044 §KeyStack).

Une clé membre `suspended` n'est PAS servie, et ne passe plus la main à la clé
d'équipe ou d'org (#1499) : la résolution la refuse en la nommant et dit le geste
(la réactiver, ou nommer un autre compte). Elle reste listée et réactivable. Trois
plans testés :
- la cascade (`walk_cascade`) garde le barreau membre suspendu : la marche ne descend
  pas à l'org à sa place (ce que le statut annonce = ce que la résolution juge) ;
- la RÉSOLUTION RÉELLE (`resolve_credential` → `_member_fetch`, #401) lève le refus
  nommé `account_suspended`, jamais la clé d'org ;
- la capacité d'écriture (`_suspend_instance`) pose/lève le flag, garde SUB_ONLY,
  et 404 si aucune clé membre.

Logique pure : les sondes de cascade sont injectées, la couche SQL de `update_meta`
est stubbée (vérifiée au déploiement).
"""
import pytest

from oto_mcp import access
from oto_mcp.capabilities.connectors import instances as ci
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx


def _stub_member_and_org(monkeypatch, *, suspended):
    """Sondes RÉELLES : clé membre ET clé d'org présentes (serper), la membre
    suspendue ou non. La suspension est repliée DANS les sondes (PRESENCE/FETCH),
    pas dans le walker → on stube les fonctions DB, pas la sonde."""
    monkeypatch.setattr(access.db, "has_member_api_key", lambda s, o, p, *a, **k: True)
    monkeypatch.setattr(access.db, "get_member_api_key", lambda s, o, p, *a, **k: "MK")
    monkeypatch.setattr(access.db, "member_instance_suspended",
                        lambda s, o, p, *a, **k: suspended)
    monkeypatch.setattr(access.group_store, "has_group_secret", lambda g, p: False)
    monkeypatch.setattr(access.group_store, "get_group_secret", lambda g, p: None)
    monkeypatch.setattr(access.org_store, "has_org_secret", lambda o, p: True)
    monkeypatch.setattr(access.org_store, "get_org_secret", lambda o, p: "OK")


@pytest.mark.parametrize("probe", [access.PRESENCE_PROBE, access.FETCH_PROBE])
def test_suspended_member_rung_does_not_hand_over(monkeypatch, probe):
    _stub_member_and_org(monkeypatch, suspended=True)
    win = access.cascade_winner("u1", "serper", org=1, group=None, probe=probe)
    # la marche s'arrête à la clé membre (que la résolution refusera par son nom) :
    # l'org ne la remplace pas
    assert win is not None and win.mode == "user"


@pytest.mark.parametrize("probe", [access.PRESENCE_PROBE, access.FETCH_PROBE])
def test_active_member_rung_wins(monkeypatch, probe):
    _stub_member_and_org(monkeypatch, suspended=False)
    win = access.cascade_winner("u1", "serper", org=1, group=None, probe=probe)
    assert win is not None and win.mode == "user"


def test_walk_keeps_suspended_rung_first_in_status(monkeypatch):
    """Statut (walk complet) : le barreau suspendu reste en tête — la résolution le
    juge (et le refuse) avant l'org, qui n'est jamais servie à sa place."""
    _stub_member_and_org(monkeypatch, suspended=True)
    # zoho = byo-only (pas de barreau plateforme → pas d'I/O plateforme)
    modes = [r.mode for r in access.walk_cascade(
        "u1", "zoho", org=1, group=None, probe=access.PRESENCE_PROBE, want="byo")]
    assert modes == ["user", "org"]


# --- résolution réelle (#401) : resolve_credential de bout en bout -----------
# La cascade n'est PAS recopiée : on traverse le montage réel
# `resolve_credential` → `_resolve_credential_impl` → `_member_fetch` → walker,
# seule la couche db/coffre est stubbée (jamais la sonde — c'est précisément le
# stub de sonde qui avait masqué le bug).

def _wire_real_resolution(monkeypatch, *, suspended, org=1):
    monkeypatch.setattr(access, "current_org", lambda sub: org)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access.db, "get_member_api_key",
                        lambda s, o, p, *a, **k: "MK")
    monkeypatch.setattr(access.db, "member_instance_suspended",
                        lambda s, o, p, *a, **k: suspended)
    # Multi-compte par défaut (reprise #399) : la sélection liste les comptes
    # avant de lire la clé ; aucun compte nommé ici → ligne legacy `account=''`.
    monkeypatch.setattr(access.credentials_store, "list_accounts",
                        lambda *a, **k: [])
    monkeypatch.setattr(access.group_store, "get_group_secret",
                        lambda g, p, account="": None)
    monkeypatch.setattr(access.org_store, "get_org_secret",
                        lambda o, p, account="": "OK")
    monkeypatch.setattr(access.db, "insert_tool_call", lambda payload: None)


def _refus_suspendu(provider):
    from oto_mcp.mcp_errors import McpError
    with pytest.raises(McpError) as e:
        access.resolve_credential(provider, sub="u1", emit_on_failure=False)
    return e.value.error


def test_resolution_refuses_suspended_member_key_by_name(monkeypatch):
    """Clé membre suspendue en coffre → refus NOMMÉ, qui dit quoi faire ; la clé d'org
    présente n'est pas servie à sa place."""
    _wire_real_resolution(monkeypatch, suspended=True)
    err = _refus_suspendu("serper")
    assert err.data["code"] == access.niveaux.CODE_COMPTE_SUSPENDU
    assert "`serper` key is suspended" in err.message
    assert "oto_instance op='resume'" in err.message and "_account=" in err.message
    assert "OK" not in err.message


def test_resolution_uses_member_key_when_not_suspended(monkeypatch):
    _wire_real_resolution(monkeypatch, suspended=False)
    rc = access.resolve_credential("serper", sub="u1")
    assert rc.mode == "user" and rc.key == "MK"


def test_resolution_multi_account_refuses_suspended_account(monkeypatch):
    """Multi-compte (zoho) : le compte auto-sélectionné (unique) suspendu est refusé
    par son nom aussi — la suspension se pose PAR compte (`SuspendInstanceInput.account`)."""
    _wire_real_resolution(monkeypatch, suspended=False)
    monkeypatch.setattr(access.credentials_store, "list_accounts",
                        lambda et, eid, p: [{"account": "a1", "meta": {}}])
    monkeypatch.setattr(access.db, "member_instance_suspended",
                        lambda s, o, p, account="", *a, **k: account == "a1")
    err = _refus_suspendu("zoho")
    assert err.data["code"] == access.niveaux.CODE_COMPTE_SUSPENDU
    assert "`zoho` key `a1` is suspended" in err.message
    assert "oto_instance op='resume'" in err.message


# --- capacité d'écriture -----------------------------------------------------

def _ctx(org=1, sub="u1"):
    return ResolvedCtx(sub=sub, org_id=org)


def test_suspend_writes_flag(monkeypatch):
    calls = {}

    def _upd(et, eid, connector, account, patch, *a, **k):
        calls.update(et=et, eid=eid, connector=connector,
                     account=account, patch=patch)
        return True

    monkeypatch.setattr(ci.credentials_store, "update_meta", _upd)
    out = ci._suspend_instance(_ctx(),
                               ci.SuspendInstanceInput(connector="serper"))
    assert out == {"connector": "serper", "account": None, "suspended": True}
    assert calls["et"] == ci.credentials_store.MEMBER
    assert calls["eid"] == ci.credentials_store.member_id(1, "u1")
    assert calls["connector"] == "serper"
    assert calls["patch"] == {"suspended": True}


def test_reactivate_writes_false(monkeypatch):
    monkeypatch.setattr(ci.credentials_store, "update_meta",
                        lambda *a, **k: True)
    out = ci._suspend_instance(
        _ctx(), ci.SuspendInstanceInput(connector="serper", suspended=False))
    assert out["suspended"] is False


def test_suspend_404_when_no_instance(monkeypatch):
    monkeypatch.setattr(ci.credentials_store, "update_meta",
                        lambda *a, **k: False)
    with pytest.raises(AuthzDenied) as e:
        ci._suspend_instance(_ctx(),
                             ci.SuspendInstanceInput(connector="serper"))
    assert e.value.code == "no_instance"


def test_suspend_requires_active_org():
    with pytest.raises(AuthzDenied) as e:
        ci._suspend_instance(_ctx(org=None),
                             ci.SuspendInstanceInput(connector="serper"))
    assert e.value.code == "no_active_org"
