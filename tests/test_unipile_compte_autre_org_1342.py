"""#1342 — un compte hébergé lié dans une AUTRE org du même utilisateur.

Décision du 27/10/2026 (option C, « cloisonnement strict + reprise ») :

1. rien n'est servi hors de son org sans reprise : un appel épinglé `_account=` sur un
   compte propre lié à une autre org est REFUSÉ (il passait, pendant que la liste et le
   refus sans épingle disaient « aucun compte ») — les prêts et le compte de l'org de
   contexte restent servis ;
2. le refus (épinglé ou non), la liste `oto_identity op=list` et le statut nomment le
   compte disponible ailleurs et le geste exact pour le reprendre ici
   (`unipile_connect_start`, qui l'adopte sans nouvel appairage) — jamais « connecte ».

Seams de domaine stubés, aucune base (convention du dépôt).
"""
import asyncio

import pytest

from oto_mcp import access, db, session_org
from oto_mcp.access import ResolvedCredential
from oto_mcp.capabilities._types import ResolvedCtx
from oto_mcp.capabilities.connectors import identities as CI
from oto_mcp.connectors import identities as ci
from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import unipile as unipile_tool

ICI, LA_BAS = 3, 2
COMPTE = {"provider": "WHATSAPP", "account_id": "acc_wa", "account_name": "Mon WhatsApp",
          "org_id": LA_BAS, "platform_seat": True, "connected_at": "2026-10-01"}
AILLEURS = {"whatsapp": {"account_id": "acc_wa", "account_name": "Mon WhatsApp",
                         "org_id": LA_BAS}}


class _FakeClient:
    def __init__(self, api_key=None, account_id=None, dsn=None, provider=None):
        self.account_id, self.provider = account_id, provider


@pytest.fixture
def monde(monkeypatch):
    """Le compte WhatsApp du sub vit dans l'org 2 ; le contexte est l'org 3, où
    l'option est ouverte (donc le compte y est adoptable)."""
    etat = {"comptes": [dict(COMPTE)], "ailleurs": dict(AILLEURS)}
    monkeypatch.setattr(access, "current_org", lambda sub: ICI)
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: "u1")
    monkeypatch.setattr(access, "project_pinned_identity", lambda c: None)
    monkeypatch.setattr(access, "resolve_credential",
                        lambda p, want="auto", **k: ResolvedCredential(
                            "unipile", "PK", True, "platform"))
    monkeypatch.setattr(db, "list_unipile_accounts", lambda sub: list(etat["comptes"]))
    monkeypatch.setattr(db, "get_unipile_account_id",
                        lambda sub, org, prov: next(
                            (a["account_id"] for a in etat["comptes"]
                             if a["org_id"] == org and a["provider"] == prov), None))
    monkeypatch.setattr(unipile_tool, "status_for",
                        lambda sub, **k: {"elsewhere": dict(etat["ailleurs"])})
    monkeypatch.setattr("oto.tools.unipile.UnipileClient", _FakeClient)
    from oto_mcp import unipile_connect
    monkeypatch.setattr(unipile_connect, "connections_page", lambda sub, org: None)
    return etat


def _epingle(compte):
    tok = session_org.set_call_account(compte)
    return tok


# --- 1. l'appel épinglé sur un compte d'une autre org est refusé, et NOMMÉ -----------

def test_epingle_sur_un_compte_d_une_autre_org_est_refuse_avec_un_code_nomme(monde):
    tok = _epingle("acc_wa")
    try:
        with pytest.raises(ci.CompteDansUneAutreOrg) as e:
            ci.resolve_operated_account_id("u1", "WHATSAPP")
    finally:
        session_org.reset_call_account(tok)
    assert e.value.code == "account_in_other_org"
    msg = str(e.value)
    assert "acc_wa" in msg and f"org {LA_BAS}" in msg
    assert "unipile_connect_start(channel='whatsapp')" in msg
    assert "Connect your" not in msg


def test_epingle_le_refus_atteint_l_appel_comme_un_McpError_code(monde):
    tok = _epingle("acc_wa")
    try:
        with pytest.raises(McpError) as e:
            unipile_tool.unipile_client("WHATSAPP")
    finally:
        session_org.reset_call_account(tok)
    assert e.value.error.data == {"code": "account_in_other_org", "retryable": False}
    assert "unipile_connect_start(channel='whatsapp')" in e.value.error.message


def test_epingle_non_adoptable_nomme_l_org_sans_promettre_la_reprise(monde):
    """Option fermée ici (ou clé propre de l'autre org) : pas de reprise promise, mais
    le compte et son org sont nommés, et `_org=` dit d'où l'utiliser."""
    monde["ailleurs"] = {}
    tok = _epingle("acc_wa")
    try:
        with pytest.raises(ci.CompteDansUneAutreOrg) as e:
            ci.resolve_operated_account_id("u1", "WHATSAPP")
    finally:
        session_org.reset_call_account(tok)
    assert "unipile_connect_start" not in str(e.value)
    assert f"_org={LA_BAS}" in str(e.value)


def test_epingle_sur_le_compte_de_l_org_de_contexte_passe(monde):
    monde["comptes"] = [dict(COMPTE, org_id=ICI)]
    tok = _epingle("acc_wa")
    try:
        assert ci.resolve_operated_account_id("u1", "WHATSAPP") == "acc_wa"
    finally:
        session_org.reset_call_account(tok)


def test_epingle_sur_un_compte_adopte_ici_ET_lie_ailleurs_passe(monde):
    """Après la reprise, le même compte a deux liaisons : celle d'ici suffit."""
    monde["comptes"] = [dict(COMPTE), dict(COMPTE, org_id=ICI)]
    tok = _epingle("acc_wa")
    try:
        assert ci.resolve_operated_account_id("u1", "WHATSAPP") == "acc_wa"
    finally:
        session_org.reset_call_account(tok)


def test_epingle_le_compte_byo_d_une_autre_org_ne_suit_plus_la_personne(monde):
    """#172 retiré (#1499) : ni la clé ni le compte ne suivent une personne d'une org à
    l'autre — son compte d'une autre org épinglé reçoit le refus NOMMÉ, jamais servi."""
    tok = _epingle("acc_wa")
    try:
        with pytest.raises(ci.CompteDansUneAutreOrg):
            ci.resolve_operated_account_id("u1", "WHATSAPP")
    finally:
        session_org.reset_call_account(tok)


# --- 2. le refus sans épingle nomme le compte et le geste ---------------------------

def test_sans_epingle_le_refus_nomme_le_compte_ailleurs_et_la_reprise(monde):
    with pytest.raises(McpError) as e:
        unipile_tool.unipile_client("WHATSAPP")
    assert e.value.error.data == {"code": "account_in_other_org", "retryable": False}
    msg = e.value.error.message
    assert "Mon WhatsApp" in msg and "unipile_connect_start(channel='whatsapp')" in msg
    assert "Connect your" not in msg


def test_sans_epingle_et_rien_ailleurs_le_refus_reste_connecte_ton_compte(monde):
    monde["comptes"], monde["ailleurs"] = [], {}
    with pytest.raises(McpError, match="Connect your Whatsapp account"):
        unipile_tool.unipile_client("WHATSAPP")


# --- 3. la liste nomme le compte disponible ailleurs --------------------------------

def _liste(monkeypatch, connector, identites):
    monkeypatch.setattr(CI.connector_identities, "list_identities",
                        lambda sub, name, scope="member": list(identites))
    monkeypatch.setattr(access, "account_noun", lambda name: "compte")
    monkeypatch.setattr(CI, "_why_empty", lambda ctx, con, noun: {
        "reason": "no_identity_connected", "next_step": "Connect your WhatsApp account"})
    return asyncio.run(CI._list(ResolvedCtx(sub="u1", org_id=ICI),
                                CI.IdentitiesInput(connector=connector)))


def test_liste_vide_dit_que_le_compte_est_dans_une_autre_org(monde, monkeypatch):
    out = _liste(monkeypatch, "whatsapp", [])
    assert out["identities"] == []
    assert out["reason"] == "account_in_other_org"
    assert "unipile_connect_start(channel='whatsapp')" in out["next_step"]
    assert [(e["channel"], e["account_id"], e["org_id"]) for e in out["elsewhere"]] == [
        ("WHATSAPP", "acc_wa", LA_BAS)]
    CI.ConnectorIdentities(**out)            # le contrat de sortie l'accepte


def test_liste_d_un_autre_canal_ne_le_montre_pas(monde, monkeypatch):
    out = _liste(monkeypatch, "telegram", [])
    assert "elsewhere" not in out and out["reason"] == "no_identity_connected"


def test_liste_du_porteur_unipile_le_montre_a_cote_des_comptes_d_ici(monde, monkeypatch):
    ici = [{"id": "acc_li", "is_default": True, "channel": "LINKEDIN"}]
    out = _liste(monkeypatch, "unipile", ici)
    assert out["identities"] == ici and "reason" not in out
    assert [e["channel"] for e in out["elsewhere"]] == ["WHATSAPP"]
