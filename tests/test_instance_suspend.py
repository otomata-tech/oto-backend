"""Suspension d'instance (lot 2 / ADR 0044 §KeyStack).

Une clé membre `suspended` est SAUTÉE par la résolution de credential (le barreau
du dessous prend le relais) mais reste listée et réactivable — décision de #1499 : comme
avant la bascule sur les règles. La réponse dit quel barreau a servi. NOMMÉE, elle est
refusée par son nom : on ne sert pas un autre compte que celui qu'on nomme. Trois plans
testés :
- la cascade (`walk_cascade`) saute le barreau membre suspendu ;
- la RÉSOLUTION RÉELLE (`resolve_credential` → `_member_fetch`, #401) rend le même
  verdict que les sondes — c'est le trou vécu : la sonde honorait la suspension,
  le montage réel non (patron « fail-open + tests stubbés » du CLAUDE.md) ;
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
def test_suspended_member_rung_is_skipped(monkeypatch, probe):
    _stub_member_and_org(monkeypatch, suspended=True)
    win = access.cascade_winner("u1", "serper", org=1, group=None, probe=probe)
    # la clé membre est mise de côté → l'org gagne
    assert win is not None and win.mode == "org"


@pytest.mark.parametrize("probe", [access.PRESENCE_PROBE, access.FETCH_PROBE])
def test_active_member_rung_wins(monkeypatch, probe):
    _stub_member_and_org(monkeypatch, suspended=False)
    win = access.cascade_winner("u1", "serper", org=1, group=None, probe=probe)
    assert win is not None and win.mode == "user"


def test_walk_omits_suspended_rung_from_status(monkeypatch):
    """Statut (walk complet) : le barreau suspendu ne figure PAS parmi les gagnants
    (la résolution ne l'utilise pas) — seul l'org reste."""
    _stub_member_and_org(monkeypatch, suspended=True)
    # zoho = byo-only (pas de barreau plateforme → pas d'I/O plateforme)
    modes = [r.mode for r in access.walk_cascade(
        "u1", "zoho", org=1, group=None, probe=access.PRESENCE_PROBE, want="byo")]
    assert modes == ["org"]


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


def test_resolution_skips_suspended_member_key(monkeypatch):
    """Clé membre suspendue en coffre → le barreau org GAGNE la résolution réelle
    (même verdict que FETCH_PROBE — c'est ce que le dashboard/KeyStack annonce)."""
    _wire_real_resolution(monkeypatch, suspended=True)
    rc = access.resolve_credential("serper", sub="u1")
    assert rc.mode == "org" and rc.key == "OK"


def test_resolution_uses_member_key_when_not_suspended(monkeypatch):
    _wire_real_resolution(monkeypatch, suspended=False)
    rc = access.resolve_credential("serper", sub="u1")
    assert rc.mode == "user" and rc.key == "MK"


def test_resolution_multi_account_skips_suspended_account(monkeypatch):
    """Multi-compte (zoho) : le compte auto-sélectionné (unique) suspendu est sauté
    aussi — la suspension se pose PAR compte (`SuspendInstanceInput.account`)."""
    _wire_real_resolution(monkeypatch, suspended=False)
    monkeypatch.setattr(access.credentials_store, "list_accounts",
                        lambda et, eid, p: [{"account": "a1", "meta": {}}])
    monkeypatch.setattr(access.db, "member_instance_suspended",
                        lambda s, o, p, account="", *a, **k: account == "a1")
    rc = access.resolve_credential("zoho", sub="u1")
    assert rc.mode == "org" and rc.key == "OK"


def test_la_reponse_dit_que_la_cle_suspendue_a_ete_sautee(monkeypatch):
    """L'écho de la réponse dit quel barreau a servi, et que sa clé suspendue a été
    sautée (`served_by`, `suspended_key_skipped`)."""
    import asyncio

    from oto_mcp import session_org
    from oto_mcp.middleware import call_context
    _wire_real_resolution(monkeypatch, suspended=True)
    trace: dict = {}
    tok = session_org.set_call_trace(trace)
    try:
        access.resolve_credential("serper", sub="u1")
        assert asyncio.run(call_context._rappel_de_l_appel()) == {
            "_account": "", "served_by": "org", "suspended_key_skipped": True}
    finally:
        session_org.reset_call_trace(tok)


def test_une_cle_nommee_suspendue_reste_refusee(monkeypatch):
    """Nommée (`_account=`), la clé suspendue est refusée par son nom — jamais un autre
    compte à sa place (avant la bascule aussi, elle ne servait pas : refus « introuvable »
    faute d'un compte du même nom plus bas)."""
    from oto_mcp.mcp_errors import McpError
    _wire_real_resolution(monkeypatch, suspended=False)
    monkeypatch.setattr(access.credentials_store, "list_accounts",
                        lambda et, eid, p: [{"account": "a1", "meta": {}}])
    monkeypatch.setattr(access.db, "member_instance_suspended",
                        lambda s, o, p, account="", *a, **k: account == "a1")
    with pytest.raises(McpError) as e:
        access.resolve_credential("zoho", sub="u1", account="a1", emit_on_failure=False)
    assert e.value.error.data["code"] == access.niveaux.CODE_COMPTE_SUSPENDU
    assert "`zoho` key `a1`" in e.value.error.message


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
