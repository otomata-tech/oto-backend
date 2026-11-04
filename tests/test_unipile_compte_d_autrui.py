"""Le compte Unipile d'un AUTRE membre de l'org (#1499, bascule des comptes).

Plus de prêt ni de pointeur : le compte d'autrui a une sous-instance (connecteur
`unipile`, propriétaire `member` `<org>:<sub>`, compte `canal:<PROVIDER>`), et les règles
posées sur elle disent à quel niveau il sert les autres. Il ne sert que NOMMÉ par l'appel
(`_account=` ou `_instance=inst:<id>`), au niveau qu'exige l'op ; sans règle il ne sert
pas, sous le niveau il est refusé `insufficient_level`, non nommé il ne sert jamais.
Toutes les valeurs sont fictives.
"""
import pytest

from oto_mcp import access, account_suspension, instance_refs, org_store, session_org
from oto_mcp.access import ResolvedCredential
from oto_mcp.access.niveaux import Compte, Regle
from oto_mcp.connectors import identities as ci
from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import unipile as unipile_tool

ORG = 39
MOI = "u1"
PROPRIO = "proprio"


AILLEURS = 41
LOINTAIN = "lointain"


def _compte_ailleurs(*regles):
    return Compte(instance_id=88, owner_type="member", owner_id=f"{AILLEURS}:{LOINTAIN}",
                  connector="unipile", account="canal:LINKEDIN", org_id=AILLEURS,
                  regles=tuple(regles), label="LinkedIn de lointain", org_nom="Orgaille")


class _FakeClient:
    def __init__(self, api_key=None, account_id=None, dsn=None, provider=None):
        self.api_key, self.account_id, self.dsn = api_key, account_id, dsn


def _compte(*regles):
    return Compte(instance_id=77, owner_type="member", owner_id=f"{ORG}:{PROPRIO}",
                  connector="unipile", account="canal:LINKEDIN", org_id=ORG,
                  regles=tuple(regles), label="LinkedIn de proprio")


@pytest.fixture
def monde(monkeypatch):
    """Moi, mon LinkedIn `OWN` ; le LinkedIn `AUTRUI` de `proprio`, présent et éveillé.
    `etat["regles"]` = les règles de son compte ; `etat["besoin"]` = ce qu'exige l'op."""
    etat = {"regles": (), "besoin": "read"}
    rc = ResolvedCredential("unipile", "PK", True, "platform")
    monkeypatch.setattr(access, "resolve_credential", lambda p, want="auto": rc)
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: MOI)
    monkeypatch.setattr(access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access, "project_pinned_identity", lambda c: None)
    monkeypatch.setattr(access, "besoin", lambda: etat["besoin"])
    monkeypatch.setattr(
        access, "comptes_d_autrui",
        lambda connector, org, sub, account: (
            [_compte(*etat["regles"])] if (connector, org, sub, account)
            == ("unipile", ORG, MOI, "canal:LINKEDIN") else []))
    # D1 : le compte de canal d'une personne d'une AUTRE org, ouvert par une règle qui
    # me NOMME (`etat["hors_org"]` = ses règles, None = aucun).
    etat["hors_org"] = None
    monkeypatch.setattr(
        access, "comptes_hors_org",
        lambda connector, org, sub, account=None: (
            [_compte_ailleurs(*etat["hors_org"])]
            if etat["hors_org"] is not None and (connector, org, sub, account)
            == ("unipile", ORG, MOI, "canal:LINKEDIN") else []))
    monkeypatch.setattr(org_store, "get_org_role", lambda org, sub: "member")
    monkeypatch.setattr(account_suspension, "etat", lambda sub: None)
    db = unipile_tool.db
    monkeypatch.setattr(db, "get_unipile_account_id",
                        lambda sub, org, prov: "OWN" if sub == MOI else None)
    monkeypatch.setattr(db, "get_unipile_account",
                        lambda sub, org, prov: ({"account_id": "AUTRUI",
                                                 "account_name": "Avatar"}
                                                if (sub, org, prov) == (PROPRIO, ORG,
                                                                        "LINKEDIN")
                                                else {"account_id": "LOIN",
                                                      "account_name": "Lointain"}
                                                if (sub, org, prov) == (LOINTAIN, AILLEURS,
                                                                        "LINKEDIN")
                                                else None))
    monkeypatch.setattr(db, "list_unipile_accounts",
                        lambda sub: [{"account_id": "OWN", "provider": "LINKEDIN",
                                      "org_id": ORG}] if sub == MOI else [])
    monkeypatch.setattr("oto.tools.unipile.UnipileClient", _FakeClient)
    return etat


@pytest.fixture
def nomme(monkeypatch):
    """`_account=` de l'appel."""
    return lambda account: monkeypatch.setattr(session_org, "current_call_account",
                                               lambda: account)


def test_nomme_il_sert_au_niveau_de_la_regle(monde, nomme):
    monde["regles"] = (Regle(user=MOI, group=None, org=False, level="read"),)
    nomme("AUTRUI")
    assert unipile_tool.unipile_client("LINKEDIN").account_id == "AUTRUI"


def test_nomme_par_son_instance_il_sert_aussi(monde, monkeypatch):
    monde["regles"] = (Regle(user=MOI, group=None, org=False, level="sensitive"),)
    monkeypatch.setattr(ci.comptes_heberges, "ref_du_canal",
                        lambda sub, prov: instance_refs.InstanceRef(
                            level="inst", connector=None, instance_id=77))
    monde["besoin"] = "sensitive"
    assert unipile_tool.unipile_client("LINKEDIN").account_id == "AUTRUI"


def test_sans_regle_il_est_refuse(monde, nomme):
    nomme("AUTRUI")
    with pytest.raises(McpError, match="neither yours nor"):
        unipile_tool.unipile_client("LINKEDIN")


def test_une_regle_read_refuse_l_envoi_insufficient_level(monde, nomme, monkeypatch):
    monkeypatch.setattr(access, "qui_gere", lambda compte, *a, **k: "proprio@exemple.test")
    monde["regles"] = (Regle(user=MOI, group=None, org=False, level="read"),)
    monde["besoin"] = "sensitive"
    nomme("AUTRUI")
    with pytest.raises(McpError) as e:
        unipile_tool.unipile_client("LINKEDIN")
    data = e.value.error.data
    assert data["code"] == "insufficient_level"
    assert (data["level"], data["required"]) == ("read", "sensitive")
    assert data["managed_by"] == "proprio@exemple.test"


def test_non_nomme_il_ne_sert_pas(monde):
    monde["regles"] = (Regle(user=MOI, group=None, org=False, level="manage"),)
    assert unipile_tool.unipile_client("LINKEDIN").account_id == "OWN"


def test_proprietaire_en_pause_il_ne_sert_plus(monde, nomme, monkeypatch):
    monde["regles"] = (Regle(user=MOI, group=None, org=False, level="sensitive"),)
    monkeypatch.setattr(account_suspension, "etat",
                        lambda sub: {"reason": "départ"} if sub == PROPRIO else None)
    nomme("AUTRUI")
    with pytest.raises(McpError, match="neither yours nor"):
        unipile_tool.unipile_client("LINKEDIN")


@pytest.mark.asyncio
async def test_la_liste_le_montre_avec_son_niveau_jamais_par_defaut(monde, monkeypatch):
    monde["regles"] = (Regle(user=MOI, group=None, org=False, level="read"),)
    monkeypatch.setattr(ci, "_unipile_client", lambda sub: None)

    async def _carte(sub):
        return {"OWN": "OK", "AUTRUI": "OK"}
    monkeypatch.setattr(ci, "_unipile_live_status_map", _carte)
    monkeypatch.setattr(access, "libelle_de_personne", lambda sub: "proprio@exemple.test")
    ids = {i["id"]: i for i in await ci._unipile_list(MOI, "LINKEDIN")}
    assert ids["OWN"]["is_default"] is True and "level" not in ids["OWN"]
    autre = ids["AUTRUI"]
    assert autre["level"] == "read" and autre["is_default"] is False
    assert autre["owner"] == {"type": "member", "id": f"{ORG}:{PROPRIO}",
                              "label": "proprio@exemple.test"}
    assert "granted" not in autre and "shared" not in autre


@pytest.mark.asyncio
async def test_sans_regle_la_liste_ne_le_montre_pas(monde, monkeypatch):
    monkeypatch.setattr(ci, "_unipile_client", lambda sub: None)

    async def _carte(sub):
        return {"OWN": "OK"}
    monkeypatch.setattr(ci, "_unipile_live_status_map", _carte)
    assert [i["id"] for i in await ci._unipile_list(MOI, "LINKEDIN")] == ["OWN"]


@pytest.mark.asyncio
async def test_choisir_le_compte_d_autrui_par_defaut_est_refuse(monde):
    monde["regles"] = (Regle(user=MOI, group=None, org=False, level="manage"),)
    with pytest.raises(ValueError, match="never your default"):
        await ci._unipile_select(MOI, "AUTRUI", "LINKEDIN")


# ── D1 : le compte d'une personne d'une AUTRE org, ouvert par une règle qui me nomme ──

def _regle(level):
    return Regle(user=MOI, group=None, org=False, level=level)


def test_hors_org_nomme_il_sert_au_niveau_de_la_regle(monde, nomme):
    monde["hors_org"] = (_regle("read"),)
    nomme("LOIN")
    assert unipile_tool.unipile_client("LINKEDIN").account_id == "LOIN"


def test_hors_org_non_nomme_il_ne_sert_pas(monde):
    monde["hors_org"] = (_regle("manage"),)
    assert unipile_tool.unipile_client("LINKEDIN").account_id == "OWN"


def test_hors_org_sans_regle_qui_me_nomme_il_ne_sert_pas(monde, nomme):
    # Une règle d'équipe ou d'org ne traverse pas les orgs (D1).
    monde["hors_org"] = (Regle(user=None, group=None, org=True, level="manage"),)
    nomme("LOIN")
    with pytest.raises(McpError, match="neither yours nor"):
        unipile_tool.unipile_client("LINKEDIN")


@pytest.mark.asyncio
async def test_hors_org_la_liste_dit_l_org_preteuse_et_le_niveau_rien_d_autre(
        monde, monkeypatch):
    monde["hors_org"] = (_regle("read"),)
    monkeypatch.setattr(ci, "_unipile_client", lambda sub: None)

    async def _carte(sub):
        return {"OWN": "OK", "LOIN": "OK"}
    monkeypatch.setattr(ci, "_unipile_live_status_map", _carte)
    monkeypatch.setattr(access, "libelle_de_personne", lambda sub: "lointain@exemple.test")
    ids = {i["id"]: i for i in await ci._unipile_list(MOI, "LINKEDIN")}
    assert set(ids) == {"OWN", "LOIN"}          # rien d'autre de l'org prêteuse
    loin = ids["LOIN"]
    assert (loin["level"], loin["lent_by_org"], loin["is_default"]) == (
        "read", "Orgaille", False)
    assert loin["owner"] == {"type": "member", "id": f"{AILLEURS}:{LOINTAIN}",
                             "label": "lointain@exemple.test"}


def test_hors_org_coffre_la_cle_d_ailleurs_et_le_pret_se_listent_a_nommer(monkeypatch):
    """Clés du coffre : ma clé personnelle d'une autre org (#172) marquée
    `elsewhere_org`, le compte qu'une règle nominative m'ouvre marqué `lent_by_org` ;
    aucune autre ligne de ces orgs, jamais le défaut."""
    from oto_mcp import credentials_store
    monkeypatch.setattr(access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(credentials_store, "list_accounts", lambda et, eid, con: [
        {"account": "mien-ici", "meta": {"is_default": True}}])
    monkeypatch.setattr(org_store, "get_org_role", lambda org, sub: "member")
    monkeypatch.setattr(account_suspension, "etat", lambda sub: None)
    monkeypatch.setattr(access, "libelle_de_personne", lambda sub: "lointain@exemple.test")
    a_moi = Compte(instance_id=5, owner_type="member", owner_id=f"{AILLEURS}:{MOI}",
                   connector="zoho", account="mien-la-bas", org_id=AILLEURS,
                   org_nom="Orgaille")
    prete = Compte(instance_id=6, owner_type="member", owner_id=f"{AILLEURS}:{LOINTAIN}",
                   connector="zoho", account="sien", org_id=AILLEURS,
                   regles=(_regle("write"),), org_nom="Orgaille")
    ferme = Compte(instance_id=7, owner_type="member", owner_id=f"{AILLEURS}:{LOINTAIN}",
                   connector="zoho", account="ferme", org_id=AILLEURS, org_nom="Orgaille")
    monkeypatch.setattr(access, "comptes_hors_org",
                        lambda connector, org, sub, account=None: (
                            [a_moi, prete, ferme] if (connector, org, sub) == (
                                "zoho", ORG, MOI) else []))
    ids = {i["id"]: i for i in ci.list_identities(MOI, "zoho")}
    assert set(ids) == {"mien-ici", "mien-la-bas", "sien"}
    assert ids["mien-la-bas"]["elsewhere_org"] == "Orgaille"
    assert "owner" not in ids["mien-la-bas"] and "lent_by_org" not in ids["mien-la-bas"]
    assert (ids["sien"]["level"], ids["sien"]["lent_by_org"]) == ("write", "Orgaille")
    assert ids["sien"]["owner"]["label"] == "lointain@exemple.test"
    assert not ids["mien-la-bas"]["is_default"] and not ids["sien"]["is_default"]
