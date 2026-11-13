"""Où un niveau d'accès MORD — garde d'appel, outils masqués, ops servies. Bancs SANS base.

Le contexte de l'appel (sub, org, équipe active) et le réglage sont doublés ; la lecture
réelle et le chemin servi sont éprouvés par `test_access_level_live.py`.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp import access, call_axes
from oto_mcp.connectors import access_level_gate as gate
from oto_mcp.connectors import access_levels as al
from oto_mcp.mcp_errors import McpError

ORG, EQUIPE = 11, 22
SCHEMA = {"type": "object", "properties": {
    "module": {"type": "string"},
    "op": {"default": "list", "enum": ["list", "get", "search", "create", "update",
                                       "delete"], "type": "string"}},
    "required": ["module"]}


@pytest.fixture
def contexte(monkeypatch):
    """Un sub dans l'org ORG, équipe active EQUIPE ; `regler(...)` pose le réglage lu."""
    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: "sub-x")
    monkeypatch.setattr(access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(access, "current_group", lambda sub: EQUIPE)
    lus = []

    def regler(**par_connecteur):
        def settings(org, group):
            lus.append((org, group))
            return par_connecteur
        monkeypatch.setattr(al, "settings", settings)
    regler()
    return regler, lus


async def _schema():
    return SCHEMA


def _appel(tool: str, args: dict):
    return asyncio.run(gate.require_covered(tool, args, _schema))


def test_au_niveau_full_tout_passe(contexte):
    _appel("zoho_record", {"module": "Leads", "op": "delete"})
    _appel("zohodesk_create_ticket", {"data": {}})


def test_un_outil_sans_niveaux_ne_lit_rien(contexte):
    _regler, lus = contexte
    _appel("serper_search", {"q": "x"})
    assert lus == [], "un connecteur sans niveaux ne coûte aucune lecture"


def test_sous_read_l_ecriture_est_refusee_nommement(contexte):
    regler, _ = contexte
    regler(zoho={"org": "read"})
    with pytest.raises(McpError) as e:
        _appel("zoho_record", {"module": "Leads", "op": "create"})
    data = e.value.error.data
    assert data["code"] == gate.CODE == "access_level_denied"
    assert (data["level"], data["tool"], data["op"], data["scope"]) == (
        "read", "zoho_record", "create", "org")
    assert data["required_scopes"] == ["ZohoCRM.modules.CREATE"]
    assert data["retryable"] is False
    _appel("zoho_record", {"module": "Leads", "op": "get"})


def test_sans_op_l_op_par_defaut_est_jugee(contexte):
    regler, _ = contexte
    regler(zoho={"org": "read"})
    _appel("zoho_record", {"module": "Leads"})               # défaut `list` : couvert

    async def _schema_create():
        return {"properties": {"op": {"default": "create", "enum": ["create"]}}}
    with pytest.raises(McpError):
        asyncio.run(gate.require_covered("zoho_note", {"module": "Leads",
                                                      "record_id": "1"}, _schema_create))


def test_un_outil_non_couvert_est_refuse(contexte):
    regler, _ = contexte
    regler(zohodesk={"group": "read"})
    with pytest.raises(McpError) as e:
        _appel("zohodesk_tickets", {})
    assert e.value.error.data["scope"] == "group"
    assert e.value.error.data["group_id"] == EQUIPE
    _appel("zohodesk_articles", {})


def test_sans_sub_rien_n_est_garde(contexte, monkeypatch):
    regler, _ = contexte
    regler(zoho={"org": "read"})
    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: None)
    _appel("zoho_record", {"module": "Leads", "op": "delete"})


# --- le contexte qui POSSÈDE la clé servie, pas seulement celui de l'appel ----------
#
# L'appel se juge sous (org, équipe active) ; or trois chemins servent la clé d'une
# AUTRE équipe que l'active : l'équipe propriétaire d'un projet partagé prêtée par
# héritage (`groupe_herite`, #480), l'instance épinglée par `_instance=`, l'instance liée
# par le projet. Sous l'org au niveau `full`, l'équipe propriétaire au niveau `read` :
# la clé de cette équipe ne doit pas écrire.

PROPRIO = 33          # l'équipe qui a restreint, et dont la clé serait servie


@pytest.fixture
def equipe_restreinte(contexte, monkeypatch):
    """L'appel se résout au niveau de l'org (aucune équipe active), org `full` ; seule
    l'équipe PROPRIO a posé `read` pour zoho."""
    from oto_mcp import group_store
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(al, "settings", lambda org, group: (
        {"zoho": {"org": None, "group": "read"}} if group == PROPRIO else {}))
    monkeypatch.setattr(group_store, "get_group",
                        lambda gid: {"id": gid, "org_id": ORG})
    _appel("zoho_record", {"module": "Leads", "op": "delete"})   # témoin : l'org passe


def _avec(setter, resetter, valeur, fn):
    tok = setter(valeur)
    try:
        return fn()
    finally:
        resetter(tok)


def _refus_d_ecriture():
    with pytest.raises(McpError) as e:
        _appel("zoho_record", {"module": "Leads", "op": "delete"})
    data = e.value.error.data
    assert (data["code"], data["scope"], data["group_id"]) == (
        "access_level_denied", "group", PROPRIO), data
    _appel("zoho_record", {"module": "Leads", "op": "get"})       # la lecture, servie


def test_la_cle_d_equipe_pretee_par_heritage_garde_son_niveau(equipe_restreinte):
    from oto_mcp import session_org
    from oto_mcp.access.heritage import ClesDuProjet
    cles = ClesDuProjet(sub="sub-x", projet=7, org=ORG, membre=True, org_heritee=False,
                        groupe_herite=PROPRIO)
    _avec(session_org.set_call_cles, session_org.reset_call_cles, cles, _refus_d_ecriture)


def test_l_instance_d_equipe_epinglee_garde_son_niveau(equipe_restreinte):
    from oto_mcp import instance_refs, session_org
    ref = instance_refs.parse_ref(f"group:{PROPRIO}:zoho")
    _avec(session_org.set_call_instance, session_org.reset_call_instance, ref,
          _refus_d_ecriture)


def test_l_instance_d_equipe_liee_au_projet_garde_son_niveau(equipe_restreinte,
                                                              monkeypatch):
    from oto_mcp import instance_refs
    ref = instance_refs.parse_ref(f"group:{PROPRIO}:zoho")
    monkeypatch.setattr(access, "project_pinned_instance",
                        lambda provider, project_id=None: ref if provider == "zoho" else None)
    _refus_d_ecriture()


# --- un compte PRÊTÉ à l'équipe restreinte : ne sert que désigné, jugé sous elle --------
#
# La prêteuse (org ORG) prête son compte zoho `jane` à l'équipe PROPRIO, qui n'est pas
# l'équipe active de l'appel. Le prêt ne sert que désigné (`_instance=` ou `_account=`) :
# la désignation, connue avant la résolution, fait juger l'équipe emprunteuse.

PRETEUSE = "preteuse"


@pytest.fixture
def pret_a_l_equipe(equipe_restreinte, monkeypatch):
    from oto_mcp import account_suspension, credentials_store, group_store, roles
    ligne = {"entity_type": credentials_store.MEMBER, "entity_id": f"{ORG}:{PRETEUSE}",
             "connector": "zoho", "account": "jane", "meta": {}}
    monkeypatch.setattr(credentials_store, "get_instance_sharing",
                        lambda et, eid, con, acc="": (None, [f"group:{PROPRIO}"]))
    monkeypatch.setattr(credentials_store, "list_shared_with",
                        lambda scopes: [ligne] if scopes == [f"group:{PROPRIO}"] else [])
    monkeypatch.setattr(group_store, "is_group_member",
                        lambda sub, gid: (sub, gid) == ("sub-x", PROPRIO))
    monkeypatch.setattr(group_store, "list_groups_for_user", lambda sub, org=None: [
        {"group_id": PROPRIO, "name": "Ventes"}] if sub == "sub-x" else [])
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: org == ORG)
    monkeypatch.setattr(account_suspension, "refus_preteur", lambda *a, **k: None)


def test_le_pret_a_l_equipe_epingle_garde_le_niveau_de_l_equipe(pret_a_l_equipe):
    from oto_mcp import instance_refs, session_org
    ref = instance_refs.parse_ref(instance_refs.make_member_ref(ORG, PRETEUSE, "zoho", "jane"))
    _avec(session_org.set_call_instance, session_org.reset_call_instance, ref,
          _refus_d_ecriture)


def test_le_pret_a_l_equipe_nomme_par_account_garde_le_niveau_de_l_equipe(pret_a_l_equipe):
    from oto_mcp import session_org
    _avec(session_org.set_call_account, session_org.reset_call_account, "jane",
          _refus_d_ecriture)


def test_un_autre_compte_nomme_ne_juge_pas_l_equipe_du_pret(pret_a_l_equipe):
    from oto_mcp import session_org
    _avec(session_org.set_call_account, session_org.reset_call_account, "siege",
          lambda: _appel("zoho_record", {"module": "Leads", "op": "delete"}))


def test_le_pret_nominatif_nomme_garde_le_niveau_de_l_org_du_preteur(contexte, monkeypatch):
    """Un prêt à une personne traverse les orgs : nommé, il est jugé sous l'org de la
    prêteuse (celle qui l'a restreint), pas seulement sous celle de l'appel."""
    from oto_mcp import credentials_store, session_org
    autre_org = 44
    monkeypatch.setattr(al, "settings", lambda org, group: (
        {"zoho": {"org": "read", "group": None}} if org == autre_org else {}))
    monkeypatch.setattr(credentials_store, "list_shared_with", lambda scopes: [
        {"entity_type": credentials_store.MEMBER, "entity_id": f"{autre_org}:{PRETEUSE}",
         "connector": "zoho", "account": "jane", "meta": {}}]
        if scopes == ["user:sub-x"] else [])
    monkeypatch.setattr(access.group_store, "list_groups_for_user", lambda sub, org=None: [])
    _appel("zoho_record", {"module": "Leads", "op": "delete"})       # sans nom : l'org passe
    with pytest.raises(McpError) as e:
        _avec(session_org.set_call_account, session_org.reset_call_account, "jane",
              lambda: _appel("zoho_record", {"module": "Leads", "op": "delete"}))
    assert (e.value.error.data["scope"], e.value.error.data["org_id"]) == ("org", autre_org)


# --- un compte PRÊTÉ à TOUTE L'ORG : jugé sous l'org du prêt et sous l'appel ------------

@pytest.fixture
def pret_a_l_org(contexte, monkeypatch):
    """La prêteuse prête son compte zoho `jane` à toute l'org ORG (`org:<id>`)."""
    from oto_mcp import account_suspension, credentials_store, org_store, roles
    ligne = {"entity_type": credentials_store.MEMBER, "entity_id": f"{ORG}:{PRETEUSE}",
             "connector": "zoho", "account": "jane", "meta": {}}
    monkeypatch.setattr(credentials_store, "get_instance_sharing",
                        lambda et, eid, con, acc="": (None, [f"org:{ORG}"]))
    monkeypatch.setattr(credentials_store, "list_shared_with",
                        lambda scopes: [ligne] if scopes == [f"org:{ORG}"] else [])
    monkeypatch.setattr(access.group_store, "list_groups_for_user", lambda sub, org=None: [])
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: org == ORG)
    monkeypatch.setattr(org_store, "get_org", lambda oid: {"id": oid, "name": "Acme"})
    monkeypatch.setattr(account_suspension, "refus_preteur", lambda *a, **k: None)


def test_le_pret_a_l_org_nomme_ne_s_ecrit_pas_sous_une_equipe_en_lecture(pret_a_l_org,
                                                                        monkeypatch):
    from oto_mcp import session_org
    monkeypatch.setattr(al, "settings", lambda org, group: (
        {"zoho": {"org": None, "group": "read"}} if group == EQUIPE else {}))
    with pytest.raises(McpError) as e:
        _avec(session_org.set_call_account, session_org.reset_call_account, "jane",
              lambda: _appel("zoho_record", {"module": "Leads", "op": "delete"}))
    assert (e.value.error.data["scope"], e.value.error.data["group_id"]) == ("group", EQUIPE)


def test_le_pret_a_l_org_epingle_d_ailleurs_garde_le_niveau_de_l_org_du_pret(pret_a_l_org,
                                                                            monkeypatch):
    """Épinglé depuis une autre org de l'emprunteuse (réglée `full`), le prêt à l'org ORG
    reste jugé sous ORG, réglée `read` : il ne s'y écrit pas."""
    from oto_mcp import instance_refs, session_org
    autre_org = 44
    monkeypatch.setattr(access, "current_org", lambda sub: autre_org)
    monkeypatch.setattr(al, "settings", lambda org, group: (
        {"zoho": {"org": "read", "group": None}} if org == ORG else {}))
    ref = instance_refs.parse_ref(instance_refs.make_member_ref(ORG, PRETEUSE, "zoho", "jane"))
    _appel("zoho_record", {"module": "Leads", "op": "delete"})      # sans pin : l'org passe
    with pytest.raises(McpError) as e:
        _avec(session_org.set_call_instance, session_org.reset_call_instance, ref,
              lambda: _appel("zoho_record", {"module": "Leads", "op": "delete"}))
    assert (e.value.error.data["scope"], e.value.error.data["org_id"]) == ("org", ORG)


def test_le_pret_a_l_org_nomme_porte_le_contexte_de_l_org_du_pret(pret_a_l_org):
    from oto_mcp import session_org
    assert _avec(session_org.set_call_account, session_org.reset_call_account, "jane",
                 lambda: gate._loan_contexts("sub-x", "zoho", "zoho", None)) == [(ORG, None)]


def test_une_instance_d_un_autre_connecteur_ne_juge_rien(equipe_restreinte):
    from oto_mcp import instance_refs, session_org
    ref = instance_refs.parse_ref(f"group:{PROPRIO}:zohodesk")
    _avec(session_org.set_call_instance, session_org.reset_call_instance, ref,
          lambda: _appel("zoho_record", {"module": "Leads", "op": "delete"}))


# --- visibilité et schéma servi -----------------------------------------------------

NOMS = {"zoho_record", "zoho_note", "zoho_modules", "zoho_connect", "zohodesk_tickets",
        "zohodesk_articles", "zohodesk_create_ticket", "serper_search", "oto_doc"}


def test_full_ne_masque_rien(contexte):
    assert gate.hidden_tools(NOMS, ORG, EQUIPE) == set()


def test_read_masque_ce_qu_il_ne_couvre_en_rien(contexte):
    regler, _ = contexte
    regler(zoho={"org": "read"}, zohodesk={"org": "read"})
    assert gate.hidden_tools(NOMS, ORG, EQUIPE) == {"zohodesk_tickets",
                                                    "zohodesk_create_ticket"}


def test_les_ops_servies_suivent_le_niveau(contexte):
    regler, _ = contexte
    assert gate.restricted_ops("sub-x", NOMS) == {}
    regler(zoho={"org": "read"})
    assert gate.restricted_ops("sub-x", NOMS) == {
        "zoho_record": {"list", "get", "search"}, "zoho_note": {"list"}}
    assert gate.restricted_ops("sub-x", {"zoho_record"}) == {
        "zoho_record": {"list", "get", "search"}}
    assert gate.restricted_ops(None, NOMS) == {}


def test_une_liste_sans_outil_a_niveaux_ne_lit_rien(contexte):
    """`tools/list` d'une boîte sans outil multiplexé d'un connecteur à niveaux : ni le
    contexte ni le réglage ne sont lus (le chemin reste pur, sans base)."""
    _regler, lus = contexte
    assert gate.restricted_ops("sub-x", {"serper_search", "oto_doc", "zohodesk_tickets"}) == {}
    assert lus == []


def test_le_schema_ne_garde_que_les_ops_couvertes():
    s = gate.restrict_schema(SCHEMA, frozenset({"list", "get"}))
    assert s["properties"]["op"]["enum"] == ["list", "get"]
    assert s["properties"]["op"]["default"] == "list"
    assert SCHEMA["properties"]["op"]["enum"][-1] == "delete", "l'original n'est pas touché"


def test_un_defaut_non_couvert_rend_op_obligatoire():
    s = gate.restrict_schema(SCHEMA, frozenset({"get"}))
    assert "default" not in s["properties"]["op"]
    assert "op" in s["required"] and "module" in s["required"]


# --- Search Console : `read` par défaut, sans réglage -------------------------------

def test_sans_reglage_search_console_masque_et_refuse_l_envoi(contexte):
    """Le défaut de la description (`read`) mord comme un réglage : l'outil d'envoi sort de
    la boîte, son appel est refusé, et le refus dit le geste (passer l'org en `full`) sans
    promettre un nouveau consentement qu'un compte de service ne demande pas."""
    assert gate.hidden_tools({"google_search_console", "google_search_console_act"}, ORG,
                             EQUIPE) == {"google_search_console_act"}
    _appel("google_search_console", {"op": "list_sites"})
    with pytest.raises(McpError) as e:
        _appel("google_search_console_act", {"op": "add_sitemap"})
    data = e.value.error.data
    assert (data["code"], data["level"], data["scope"]) == ("access_level_denied", "read",
                                                            "default")
    message = e.value.error.message
    assert "level='full'" in message and f"org_id={ORG}" in message
    assert "no new consent" in message and "needs a new consent" not in message


def test_en_full_l_envoi_est_servi(contexte):
    regler, _ = contexte
    regler(google_search_console={"org": "full"})
    assert gate.hidden_tools({"google_search_console_act"}, ORG, EQUIPE) == set()
    _appel("google_search_console_act", {"op": "add_sitemap"})
    assert gate.level_of_the_call("google_search_console") == "full"


def test_le_niveau_de_l_appel_sans_sub_est_le_defaut(monkeypatch):
    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: None)
    assert gate.level_of_the_call("google_search_console") == "read"
    assert gate.level_of_the_call("serper") == al.FULL
