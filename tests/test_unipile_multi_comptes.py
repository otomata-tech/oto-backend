"""Plusieurs comptes par canal : ce que sert un appel qui ne nomme pas de compte, ce que
rend la carte, et ce que fait la demande d'ajout.

Les invariants (un membre à un seul compte ne voit RIEN changer) :
- sans `_account=` : le compte choisi par défaut, sinon le principal — jamais d'office un
  compte ajouté, même le seul qui reste (décision du 18/11/2026) ;
- des comptes et rien qui en désigne un : refus nommé, jamais un choix au hasard
  (un message envoyé sous la mauvaise identité ne se reprend pas) ;
- ajouter un compte sous la clé plateforme exige le plafond PROPRE de l'org ;
- un compte ajouté ne devient jamais le défaut tout seul ;
- `_account=` désigne n'importe lequel de MES comptes d'ici, pour un appel.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from oto_mcp import access, db, session_org
from oto_mcp.access import ResolvedCredential
from oto_mcp.connectors import identities
from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import unipile as unipile_tool

ORG = 39


def _compte(aid, slot="", org=ORG, provider="LINKEDIN", nom=None):
    return {"provider": provider, "account_id": aid, "account_name": nom or aid,
            "org_id": org, "platform_seat": True, "connected_at": "2026-11-18 10:00:00",
            "slot": slot}


@pytest.fixture
def etat(monkeypatch):
    e = {"comptes": [], "pointeur": None, "pin": None}

    def _live(sub, org, prov="LINKEDIN"):
        return [a for a in e["comptes"] if a["org_id"] == org and a["provider"] == prov]

    monkeypatch.setattr(access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: "u1")
    monkeypatch.setattr(db, "list_unipile_accounts", lambda sub: list(e["comptes"]))
    monkeypatch.setattr(db, "get_unipile_account_id", lambda sub, org, prov="LINKEDIN": (
        (db.default_unipile_account(_live(sub, org, prov)) or {}).get("account_id")))
    monkeypatch.setattr(db, "get_operated_account", lambda sub, prov: e["pointeur"])
    monkeypatch.setattr(db, "granted_accounts_for", lambda sub, prov: {})
    monkeypatch.setattr(db, "suspended_lenders_for", lambda sub, prov: {})
    monkeypatch.setattr(session_org, "current_call_account", lambda: e["pin"])
    monkeypatch.setattr("oto_mcp.providers.is_personal_cross_org", lambda name: False)
    monkeypatch.setattr(db, "slots_ouverts", lambda: e.get("ouverts", True))
    monkeypatch.setattr(db, "list_account_grants_to", lambda sub: [])
    return e


# --- Résolution ------------------------------------------------------------------

def test_un_seul_compte_rien_ne_change(etat):
    etat["comptes"] = [_compte("A")]
    assert identities.resolve_operated_account_id("u1", "LINKEDIN") == "A"


def test_un_compte_ajoute_ne_devient_pas_le_defaut(etat):
    etat["comptes"] = [_compte("A"), _compte("B", slot="B")]
    assert identities.resolve_operated_account_id("u1", "LINKEDIN") == "A"


def test_l_epingle_designe_un_compte_ajoute(etat):
    etat["comptes"] = [_compte("A"), _compte("B", slot="B")]
    etat["pin"] = "B"
    assert identities.resolve_operated_account_id("u1", "LINKEDIN") == "B"


def test_le_defaut_choisi_sert_tant_qu_il_est_ici(etat):
    etat["comptes"] = [_compte("A"), _compte("B", slot="B")]
    etat["pointeur"] = {"account_id": "B", "owner_sub": "u1"}
    assert identities.resolve_operated_account_id("u1", "LINKEDIN") == "B"
    # Déconnecté (ou d'une autre org) : le choix ne s'applique plus, le principal sert —
    # jamais un refus qui couperait le canal, jamais le compte d'un tiers.
    etat["comptes"] = [_compte("A")]
    assert identities.resolve_operated_account_id("u1", "LINKEDIN") == "A"


def test_sans_principal_le_seul_restant_est_refuse_en_le_nommant(etat):
    # Le principal déconnecté, le compte ajouté qui reste ne sert jamais d'office : il
    # n'a pas été choisi. Refus nommé (un seul compte : `no_default_account`).
    etat["comptes"] = [_compte("B", slot="B", nom="Jane Sales")]
    with pytest.raises(identities.SansCompteParDefaut) as e:
        identities.resolve_operated_account_id("u1", "LINKEDIN")
    assert not isinstance(e.value, identities.PlusieursComptesSansDefaut)
    assert e.value.code == "no_default_account"
    assert "Jane Sales" in str(e.value) and "`B`" in str(e.value)
    assert "_account=" in str(e.value) and "oto_identity(op='set')" in str(e.value)


def test_sans_principal_le_seul_restant_sert_une_fois_choisi(etat, monkeypatch):
    poses = []
    monkeypatch.setattr(db, "set_operated_account",
                        lambda sub, prov, aid, owner: poses.append((sub, prov, aid, owner)))
    monkeypatch.setattr(db, "clear_operated_account",
                        lambda sub, prov: pytest.fail("le choix ne s'efface pas"))
    etat["comptes"] = [_compte("B", slot="B")]
    asyncio.run(identities._unipile_select("u1", "B", "LINKEDIN"))
    assert poses == [("u1", "LINKEDIN", "B", "u1")]
    etat["pointeur"] = {"account_id": "B", "owner_sub": "u1"}
    assert identities.resolve_operated_account_id("u1", "LINKEDIN") == "B"
    etat["pointeur"], etat["pin"] = None, "B"          # ou nommé pour l'appel
    assert identities.resolve_operated_account_id("u1", "LINKEDIN") == "B"


def test_plusieurs_sans_defaut_refuse_en_les_nommant(etat):
    etat["comptes"] = [_compte("B", slot="B", nom="Jane Sales"),
                       _compte("C", slot="C", nom="Jane Recruiting")]
    with pytest.raises(identities.PlusieursComptesSansDefaut) as e:
        identities.resolve_operated_account_id("u1", "LINKEDIN")
    assert e.value.code == "several_accounts_no_default"
    assert "Jane Sales" in str(e.value) and "`C`" in str(e.value)
    assert "_account=" in str(e.value)


def test_plusieurs_sans_defaut_ne_retombe_pas_sur_l_autre_org(etat, monkeypatch):
    # Clé propre au membre (BYO qui le suit d'org en org) : son compte de l'org de la clé
    # ne sert ICI que s'il n'a aucun compte ici. Deux comptes ici sans défaut = refus
    # nommé, jamais le compte d'une autre org envoyé en silence.
    etat["comptes"] = [_compte("B", slot="B"), _compte("C", slot="C"), _compte("Z", org=77)]
    monkeypatch.setattr("oto_mcp.providers.is_personal_cross_org", lambda name: True)
    monkeypatch.setattr(access, "personal_instance_org", lambda sub, c, exclude_org=None: 77)
    with pytest.raises(identities.PlusieursComptesSansDefaut):
        identities.resolve_operated_account_id("u1", "LINKEDIN")


@pytest.mark.parametrize("comptes,code", [
    (["B", "C"], "several_accounts_no_default"),
    (["B"], "no_default_account"),
])
def test_le_client_porte_le_refus_code_a_l_agent(etat, monkeypatch, comptes, code):
    rc = ResolvedCredential("unipile", "PK", True, "platform")
    monkeypatch.setattr(access, "resolve_credential", lambda p, want="auto": rc)
    monkeypatch.setattr(access, "project_pinned_identity", lambda name: None)
    etat["comptes"] = [_compte(c, slot=c) for c in comptes]
    with pytest.raises(McpError) as e:
        unipile_tool.unipile_client("LINKEDIN")
    assert e.value.error.data["code"] == code


def test_le_projet_qui_epingle_un_compte_leve_l_ambiguite(etat, monkeypatch):
    class _Client:
        def __init__(self, api_key=None, account_id=None, dsn=None, provider=None):
            self.account_id = account_id

    rc = ResolvedCredential("unipile", "PK", True, "platform")
    monkeypatch.setattr(access, "resolve_credential", lambda p, want="auto": rc)
    monkeypatch.setattr(access, "project_pinned_identity",
                        lambda name: "C" if name == "linkedin_unipile" else None)
    monkeypatch.setattr("oto.tools.unipile.UnipileClient", _Client)
    etat["comptes"] = [_compte("B", slot="B"), _compte("C", slot="C")]
    assert unipile_tool.unipile_client("LINKEDIN").account_id == "C"


# --- Choisir le défaut ---------------------------------------------------------------

def test_choisir_un_compte_ajoute_pose_le_pointeur_sur_moi(etat, monkeypatch):
    poses, effaces = [], []
    monkeypatch.setattr(db, "set_operated_account",
                        lambda sub, prov, aid, owner: poses.append((sub, prov, aid, owner)))
    monkeypatch.setattr(db, "clear_operated_account",
                        lambda sub, prov: effaces.append((sub, prov)))
    etat["comptes"] = [_compte("A"), _compte("B", slot="B")]
    asyncio.run(identities._unipile_select("u1", "B", "LINKEDIN"))
    assert poses == [("u1", "LINKEDIN", "B", "u1")] and effaces == []
    asyncio.run(identities._unipile_select("u1", "A", "LINKEDIN"))
    assert effaces == [("u1", "LINKEDIN")]          # retour au principal = pas de pointeur


# --- La carte ------------------------------------------------------------------------

def test_la_carte_liste_tous_les_comptes_et_marque_le_defaut(etat, monkeypatch):
    monkeypatch.setattr(access, "credential_mode_for", lambda *a, **k: "platform")
    monkeypatch.setattr(access, "option_open", lambda *a, **k: True)
    etat["comptes"] = [_compte("A"), _compte("B", slot="B"),
                       _compte("W", provider="WHATSAPP")]
    li = unipile_tool.status_for("u1")["channels"]["linkedin"]
    assert li["account_id"] == "A"
    assert [(a["account_id"], a["is_default"]) for a in li["accounts"]] == [
        ("A", True), ("B", False)]
    etat["pointeur"] = {"account_id": "B", "owner_sub": "u1"}
    li = unipile_tool.status_for("u1")["channels"]["linkedin"]
    assert li["account_id"] == "B"
    assert [a["is_default"] for a in li["accounts"]] == [False, True]
    wa = unipile_tool.status_for("u1")["channels"]["whatsapp"]
    assert [a["account_id"] for a in wa["accounts"]] == ["W"]
    assert unipile_tool.status_for("u1")["channels"]["telegram"]["accounts"] == []


@pytest.mark.parametrize("comptes", [["B", "C"], ["B"]])
def test_la_carte_sans_defaut_n_en_marque_aucun(etat, monkeypatch, comptes):
    monkeypatch.setattr(access, "credential_mode_for", lambda *a, **k: "platform")
    monkeypatch.setattr(access, "option_open", lambda *a, **k: True)
    etat["comptes"] = [_compte(c, slot=c) for c in comptes]
    li = unipile_tool.status_for("u1")["channels"]["linkedin"]
    assert li["connected"] is True
    assert not any(a["is_default"] for a in li["accounts"])
    # Le seul restant, une fois choisi, est marqué : le pointeur se lit aussi pour un.
    etat["pointeur"] = {"account_id": "B", "owner_sub": "u1"}
    li = unipile_tool.status_for("u1")["channels"]["linkedin"]
    assert [a["account_id"] for a in li["accounts"] if a["is_default"]] == ["B"]


# --- La demande d'ajout -------------------------------------------------------------

class _FakeClient:
    def __init__(self, *a, **k):
        pass

    def hosted_auth_link(self, **kw):
        _FakeClient.vu = kw
        return "https://unipile.test/auth"


@pytest.fixture
def connexion(monkeypatch, etat):
    from oto_mcp import unipile_connect
    monkeypatch.setattr(access, "resolve_credential", lambda *a, **k: SimpleNamespace(
        key="KEY", mode="platform", config={}))
    monkeypatch.setattr(access, "has_right", lambda sub, org_id, droit: True)
    monkeypatch.setattr(db, "live_unipile_accounts", lambda sub, org, prov="LINKEDIN": [
        a for a in etat["comptes"] if a["org_id"] == org and a["provider"] == prov])
    monkeypatch.setattr(db, "get_unipile_account", lambda sub, org, prov: (
        etat["comptes"][0] if etat["comptes"] else None))
    monkeypatch.setattr(db, "seat_binding_elsewhere", lambda *a, **k: {
        "account_id": "AILLEURS", "org_id": 7})
    # Le plafond PROPRE de l'org, que l'ajout exige (`plafond` dans l'état).
    monkeypatch.setattr(db, "get_org_unipile_limit", lambda org_id: etat.get("plafond", 10))
    monkeypatch.setattr(db, "count_unipile_accounts_for_org", lambda org_id: 1)
    monkeypatch.setattr(db, "slots_ouverts", lambda: etat.get("ouverts", True))
    pend = {}
    monkeypatch.setattr(db, "create_unipile_pending",
                        lambda nonce, sub, org, prov, platform_seat=False, add_account=False:
                        pend.update(provider=prov, add_account=add_account))
    monkeypatch.setattr(unipile_connect, "_default_limit", lambda: 5)
    monkeypatch.setattr("oto.tools.unipile.UnipileClient", _FakeClient)
    monkeypatch.setattr("oto_mcp.connectors.activation_gate.exiger_connectable",
                        lambda name, sub: None)
    return unipile_connect, pend


def test_ajouter_ouvre_un_assistant_sans_adopter_ni_remplacer(connexion, etat):
    uc, pend = connexion
    etat["comptes"] = [_compte("A")]
    out = asyncio.run(uc.hosted_auth_url("u1", "linkedin", add_account=True))
    assert out["url"] == "https://unipile.test/auth"     # pas d'adoption du compte ailleurs
    assert pend == {"provider": "LINKEDIN", "add_account": True}
    assert _FakeClient.vu.get("reconnect_account") is None


def test_ajouter_compte_dans_le_plafond(connexion, etat, monkeypatch):
    uc, _ = connexion
    etat["comptes"] = [_compte("A")]
    monkeypatch.setattr(db, "get_org_unipile_limit", lambda org_id: 1)
    with pytest.raises(uc.ConnectRefused) as e:
        asyncio.run(uc.hosted_auth_url("u1", "linkedin", add_account=True))
    assert e.value.code == "unipile_account_limit_reached"


def test_ajouter_sans_plafond_propre_refuse_et_dit_quoi_faire(connexion, etat):
    # Le défaut d'instance (5 ici) ne compte pas : l'ajout exige le plafond DE L'ORG.
    uc, pend = connexion
    etat["comptes"] = [_compte("A")]
    etat["plafond"] = None
    with pytest.raises(uc.ConnectRefused) as e:
        asyncio.run(uc.hosted_auth_url("u1", "linkedin", add_account=True))
    assert (e.value.status, e.value.code) == (403, "unipile_seat_cap_required")
    assert f"/api/admin/orgs/{ORG}/unipile-limit" in e.value.message
    assert pend == {}


def test_ajouter_avec_un_plafond_propre_sans_limite_passe(connexion, etat):
    # `0` posé = « sans plafond » DÉCIDÉ pour l'org : c'est un plafond posé.
    uc, pend = connexion
    etat["comptes"] = [_compte("A")]
    etat["plafond"] = 0
    out = asyncio.run(uc.hosted_auth_url("u1", "linkedin", add_account=True))
    assert out["url"] and pend == {"provider": "LINKEDIN", "add_account": True}


def test_connexion_ordinaire_sans_principal_exige_aussi_le_plafond(connexion, etat):
    # Requalifiée en ajout (il ne reste que des comptes ajoutés) : même garde.
    uc, pend = connexion
    etat["comptes"] = [_compte("B", slot="B")]
    etat["plafond"] = None
    with pytest.raises(uc.ConnectRefused) as e:
        asyncio.run(uc.hosted_auth_url("u1", "linkedin"))
    assert e.value.code == "unipile_seat_cap_required" and pend == {}


def test_remplacer_ou_premiere_connexion_sans_plafond_propre_passe(connexion, etat,
                                                                   monkeypatch):
    # Hors ajout, rien ne change : le défaut d'instance garde la première connexion.
    uc, pend = connexion
    etat["plafond"] = None
    monkeypatch.setattr(db, "seat_binding_elsewhere", lambda *a, **k: None)
    out = asyncio.run(uc.hosted_auth_url("u1", "whatsapp"))
    assert out["url"] and pend == {"provider": "WHATSAPP", "add_account": False}


def test_ajouter_sous_cle_byo_n_exige_pas_de_plafond(connexion, etat, monkeypatch):
    # Clé du client : pas de siège plateforme, pas de garde de plafond.
    uc, pend = connexion
    monkeypatch.setattr(access, "resolve_credential", lambda *a, **k: SimpleNamespace(
        key="KEY", mode="org", config={"dsn": "api.example.test"}))
    etat["comptes"] = [_compte("A")]
    etat["plafond"] = None
    out = asyncio.run(uc.hosted_auth_url("u1", "linkedin", add_account=True))
    assert out["url"] and pend == {"provider": "LINKEDIN", "add_account": True}


def test_ajouter_avant_la_revision_refuse_et_ne_touche_a_rien(connexion, etat):
    uc, pend = connexion
    etat["comptes"] = [_compte("A")]
    etat["ouverts"] = False
    with pytest.raises(uc.ConnectRefused) as e:
        asyncio.run(uc.hosted_auth_url("u1", "linkedin", add_account=True))
    assert (e.value.status, e.value.code) == (409, "multi_account_unavailable")
    assert pend == {}


def test_ajouter_sans_compte_est_une_premiere_connexion(connexion, etat, monkeypatch):
    uc, pend = connexion
    monkeypatch.setattr(db, "seat_binding_elsewhere", lambda *a, **k: None)
    etat["ouverts"] = False          # la garde ne s'applique pas : rien à quoi ajouter
    out = asyncio.run(uc.hosted_auth_url("u1", "whatsapp", add_account=True))
    assert out["url"] and pend == {"provider": "WHATSAPP", "add_account": False}


def test_premium_ne_se_combine_pas_avec_l_ajout(connexion, etat):
    uc, _ = connexion
    etat["comptes"] = [_compte("A")]
    with pytest.raises(uc.ConnectRefused) as e:
        asyncio.run(uc.hosted_auth_url("u1", "linkedin", premium="recruiter",
                                       add_account=True))
    assert e.value.code == "premium_on_add"


# --- La réconciliation lie la demande d'ajout dans un emplacement à lui -------------

class _Inventaire:
    def __init__(self, *a, **k):
        pass

    def list_accounts(self):
        return [{"id": "NEUF", "type": "LINKEDIN", "name": "Jane 2",
                 "created_at": "2099-01-01T00:00:00Z"}]

    def account_alive(self, aid):
        return True


def _demande(nonce, ajout):
    return {"nonce": nonce, "org_id": ORG, "provider": "LINKEDIN", "platform_seat": True,
            "add_account": ajout, "created_at": "2026-11-18T10:00:00Z"}


@pytest.mark.parametrize("demandes,slot", [
    ([("n1", True)], "NEUF"),
    ([("n1", False)], ""),
    # Un lien ordinaire abandonné, PUIS « Ajouter un autre compte » : l'intention qui
    # ne détruit rien l'emporte — le principal n'est pas écrasé par le compte ajouté.
    ([("n1", False), ("n2", True)], "NEUF"),
])
def test_la_reconciliation_lie_dans_l_emplacement_demande(monkeypatch, demandes, slot):
    from oto_mcp import unipile_connect
    monkeypatch.setattr(db, "list_unipile_pending_for_sub",
                        lambda sub: [_demande(n, a) for n, a in demandes])
    monkeypatch.setattr(access, "resolve_credential", lambda *a, **k: SimpleNamespace(
        key="K", is_platform=True, config={}))
    monkeypatch.setattr("oto.tools.unipile.make_unipile_client", lambda **k: _Inventaire())
    monkeypatch.setattr(db, "bound_unipile_account_ids", lambda: {"A"})
    monkeypatch.setattr(db, "foreign_unipile_account_ids", lambda sub: set())
    monkeypatch.setattr(db, "dead_unipile_account_ids_for", lambda sub, prov: set())
    monkeypatch.setattr(db, "resolve_unipile_pending", lambda nonce: None)
    ecrit = {}
    monkeypatch.setattr(db, "set_unipile_account",
                        lambda sub, aid, **k: ecrit.update(aid=aid, **k))
    out = unipile_connect.reconcile_pending("u1", account_id="NEUF")
    assert out["bound"] is True
    assert (ecrit["aid"], ecrit["slot"]) == ("NEUF", slot)


def test_connexion_ordinaire_sans_principal_est_un_ajout(connexion, etat):
    """Il ne reste que des comptes ajoutés : une connexion ordinaire en est un de plus —
    elle ne prend pas l'emplacement principal (donc le défaut) en silence, et passe
    par le plafond."""
    uc, pend = connexion
    etat["comptes"] = [_compte("B", slot="B")]
    asyncio.run(uc.hosted_auth_url("u1", "linkedin"))
    assert pend == {"provider": "LINKEDIN", "add_account": True}


def test_le_statut_dit_si_l_ajout_est_ouvert(etat, monkeypatch):
    monkeypatch.setattr(access, "credential_mode_for", lambda *a, **k: "platform")
    monkeypatch.setattr(access, "option_open", lambda *a, **k: True)
    assert unipile_tool.status_for("u1")["multi_account"] is True
    etat["ouverts"] = False
    assert unipile_tool.status_for("u1")["multi_account"] is False
    etat["ouverts"] = True
    monkeypatch.setattr(access, "option_open", lambda *a, **k: False)
    assert unipile_tool.status_for("u1")["multi_account"] is False
