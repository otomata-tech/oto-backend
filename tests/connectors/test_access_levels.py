"""Niveaux d'accès d'un connecteur OAuth : la déclaration, la couverture, le réglage.

Conception validée le 14/10/2026 (dépôt connectors, `docs/conception/niveaux-d-acces.md`).
Bancs PURS : le réglage est lu par `access_levels.settings`, doublé ici ; la requête elle-même
(`connector_settings.scoped_settings_for_key`) est éprouvée contre PostgreSQL par
`test_access_level_live.py`.
"""
from __future__ import annotations

import pytest

from oto_mcp.auth import zoho as zoho_oauth
from oto_mcp.connectors import access_levels as al

CRM, DESK = al.DECLARED["zoho"], al.DECLARED["zohodesk"]


# --- full == le consentement d'avant, à l'octet ----------------------------------

def test_full_est_exactement_le_consentement_d_avant():
    """CLIQUET. `full` ne filtre rien et demande ce que le consentement demandait avant
    les niveaux : ces tuples sont les valeurs de `auth/zoho.SCOPES` avant le 14/10/2026,
    recopiées ici exprès — c'est la seule liste écrite à la main de ces bancs."""
    assert zoho_oauth.SCOPES == {
        "zoho": ("ZohoCRM.modules.ALL", "ZohoCRM.settings.modules.READ",
                 "ZohoCRM.settings.fields.READ", "ZohoCRM.users.READ"),
        "zohodesk": ("Desk.articles.READ", "Desk.basic.READ", "Desk.search.READ",
                     "Desk.tickets.READ", "Desk.contacts.READ"),
        "zohoanalytics": ("ZohoAnalytics.data.read", "ZohoAnalytics.metadata.read"),
        # Né avec ses niveaux : pas de consentement d'avant, son `full` est celui déclaré.
        "zohosign": ("ZohoSign.documents.READ", "ZohoSign.documents.CREATE",
                     "ZohoSign.documents.UPDATE", "ZohoSign.templates.READ"),
    }
    # Une seule source : les scopes `full` des OUTILS sont ceux de la déclaration ; le
    # consentement y ajoute les scopes de vérification (cf. le banc figé ci-dessous).
    for c in ("zoho", "zohodesk", "zohosign"):
        assert zoho_oauth.SCOPES[c] is al.DECLARED[c].levels[al.FULL]
        assert zoho_oauth.consent_scopes(c, al.FULL)[:len(zoho_oauth.SCOPES[c])] \
            == zoho_oauth.SCOPES[c]


def test_les_scopes_du_consentement_sont_figes():
    """CLIQUET. Ce que l'écran de consentement de Zoho demande, connecteur par connecteur
    et niveau par niveau, est ce qui est ANNONCÉ à l'admin d'un client — rien de plus. Les
    scopes de vérification de l'organisation consentie (`VERIFICATION_SCOPES`) :
    `ZohoCRM.org.READ` aux deux niveaux du CRM, `Desk.basic.READ` au niveau `read` de Desk
    (`full` l'a déjà), aucun pour Analytics (`metadata.read` liste ses orgs). Toute
    évolution de cette liste passe par ce banc, et par une annonce aux admins."""
    assert {(c, n): zoho_oauth.consent_scopes(c, n) for c, n in (
        ("zoho", "read"), ("zoho", "full"), ("zohodesk", "read"), ("zohodesk", "full"),
        ("zohoanalytics", "full"), ("zohosign", "read"), ("zohosign", "full"))} == {
        ("zoho", "read"): ("ZohoCRM.modules.READ", "ZohoCRM.settings.modules.READ",
                           "ZohoCRM.org.READ"),
        ("zoho", "full"): ("ZohoCRM.modules.ALL", "ZohoCRM.settings.modules.READ",
                           "ZohoCRM.settings.fields.READ", "ZohoCRM.users.READ",
                           "ZohoCRM.org.READ"),
        ("zohodesk", "read"): ("Desk.articles.READ", "Desk.search.READ",
                               "Desk.basic.READ"),
        ("zohodesk", "full"): ("Desk.articles.READ", "Desk.basic.READ", "Desk.search.READ",
                               "Desk.tickets.READ", "Desk.contacts.READ"),
        ("zohoanalytics", "full"): ("ZohoAnalytics.data.read",
                                    "ZohoAnalytics.metadata.read"),
        # Zoho Sign : aucun scope de vérification (aucune lecture d'organisation documentée).
        ("zohosign", "read"): ("ZohoSign.documents.READ", "ZohoSign.templates.READ"),
        ("zohosign", "full"): ("ZohoSign.documents.READ", "ZohoSign.documents.CREATE",
                               "ZohoSign.documents.UPDATE", "ZohoSign.templates.READ"),
    }


def test_zohosign_read_ne_sert_que_les_lectures():
    """`read` sert les six lectures et aucune action ; `full` couvre tout, sans `ALL` ni
    suppression, et aucune implication n'est devinée."""
    sign = al.DECLARED["zohosign"]
    assert sign.covered_ops("read", "zohosign") == set(sign.tools["zohosign"])
    assert sign.covered_ops("read", "zohosign_act") == set()
    assert sign.covered_ops(al.FULL, "zohosign_act") == {"send_from_template", "remind",
                                                         "cancel"}
    assert sign.scope_implies == {}
    assert not any(s.endswith((".ALL", ".DELETE")) for s in sign.levels[al.FULL])


def test_la_verification_n_ouvre_aucun_outil():
    """Les scopes de vérification restent HORS des niveaux : `Desk.basic.READ` dans
    `read` y servirait `zohodesk_departments`. Le niveau `read` de Desk ne le porte pas."""
    assert "Desk.basic.READ" not in al.DECLARED["zohodesk"].levels["read"]
    assert "ZohoCRM.org.READ" not in al.DECLARED["zoho"].levels["read"]
    assert "ZohoCRM.org.READ" not in al.DECLARED["zoho"].levels[al.FULL]


def test_analytics_n_a_pas_de_niveaux():
    assert al.declared("zohoanalytics") is None
    assert zoho_oauth.consent_scopes("zohoanalytics", al.FULL) == zoho_oauth.SCOPES["zohoanalytics"]
    with pytest.raises(zoho_oauth.ZohoOAuthError):
        zoho_oauth.consent_scopes("zohoanalytics", "read")


# --- la couverture ----------------------------------------------------------------

def test_l_implication_est_une_donnee_declaree():
    """`ZohoCRM.modules.ALL` couvre les quatre verbes PARCE QUE la déclaration le dit."""
    assert {"ZohoCRM.modules.READ", "ZohoCRM.modules.CREATE", "ZohoCRM.modules.UPDATE",
            "ZohoCRM.modules.DELETE"} <= CRM.closure(al.FULL)
    assert CRM.closure("read") == {"ZohoCRM.modules.READ", "ZohoCRM.settings.modules.READ"}


def test_read_ne_couvre_que_la_lecture_du_crm():
    assert CRM.covered_ops("read", "zoho_record") == {"list", "get", "search"}
    assert CRM.covered_ops("read", "zoho_note") == {"list"}
    assert CRM.covers("read", "zoho_modules")
    assert CRM.covers("read", "zoho_connect")      # le geste de consentement, toujours
    assert CRM.covered_ops(al.FULL, "zoho_record") == {
        "list", "get", "search", "create", "update", "delete"}


def test_read_du_desk_ne_sert_que_les_articles():
    """`Desk.search.READ` n'est pas borné aux articles : au niveau `read`, aucun outil
    de tickets, recherche comprise."""
    couverts = {t for t in DESK.tools if DESK.covers("read", t)}
    assert couverts == {"zohodesk_articles", "zohodesk_article", "zohodesk_search_articles"}
    assert not DESK.covers("read", "zohodesk_search_tickets")
    assert DESK.covers(al.FULL, "zohodesk_search_tickets")


def test_une_op_ou_un_outil_non_declare_n_est_pas_couvert():
    """Fail-closed : ce qui n'est pas déclaré n'est jamais servi sous un niveau restreint."""
    assert not CRM.covers(al.FULL, "zoho_record", "purge")
    assert not CRM.covers(al.FULL, "zoho_record", None)
    assert not CRM.covers(al.FULL, "zoho_inconnu")


def test_la_monotonie_se_lit_sur_les_scopes():
    assert CRM.narrower_or_equal("read", al.FULL)
    assert not CRM.narrower_or_equal(al.FULL, "read")
    assert CRM.narrower_or_equal("read", "read")


# --- la validation d'une déclaration -------------------------------------------

@pytest.mark.parametrize("data", [
    {"levels": {"read": ("A",)}, "tools": {}},                       # pas de full
    {"levels": {"full": ()}, "tools": {}},                           # niveau vide
    {"levels": {"full": ("A",)}, "tools": {"t": {}}},                # outil sans op
    {"levels": {"full": ["A"]}, "tools": {}},                        # pas un tuple
    {"levels": {"full": ("A",)}, "tools": {}, "scopes": ("A",)},     # clé inconnue
])
def test_une_declaration_mal_formee_leve(data):
    with pytest.raises(ValueError):
        al._valider("x", data)


# --- la résolution : équipe > org > full, et l'org plafonne ------------------------

def _reglages(monkeypatch, **par_connecteur):
    monkeypatch.setattr(al, "settings", lambda org, group: par_connecteur)


def test_sans_reglage_c_est_full(monkeypatch):
    _reglages(monkeypatch)
    assert al.resolve("zoho", 1, 2) == (al.FULL, "default")


def test_l_org_puis_l_equipe(monkeypatch):
    _reglages(monkeypatch, zoho={"org": "read"})
    assert al.resolve("zoho", 1, 2) == ("read", "org")
    _reglages(monkeypatch, zoho={"group": "read"})
    assert al.resolve("zoho", 1, 2) == ("read", "group")


def test_une_equipe_plus_large_que_son_org_ne_s_applique_pas(monkeypatch):
    """L'org a restreint APRÈS que l'équipe a posé `full` : l'org est le plafond."""
    _reglages(monkeypatch, zoho={"org": "read", "group": "full"})
    assert al.resolve("zoho", 1, 2) == ("read", "org")


def test_un_connecteur_sans_niveaux_est_full():
    assert al.resolve("serper", 1, 2) == (al.FULL, "default")


def test_une_valeur_inconnue_en_base_leve():
    with pytest.raises(ValueError):
        al._stored("zoho", "org", "admin")


def test_poser_un_niveau_non_declare_est_refuse():
    with pytest.raises(al.AccessLevelError) as e:
        al.set_level("org", 1, "zoho", "admin", "sub")
    assert e.value.code == "unknown_access_level"
    with pytest.raises(al.AccessLevelError) as e:
        al.set_level("org", 1, "serper", "read", "sub")
    assert e.value.code == "no_access_levels"


def test_une_equipe_ne_peut_pas_elargir(monkeypatch):
    from oto_mcp.db import connector_settings
    ecrit = []
    monkeypatch.setattr(connector_settings, "set_connector_setting",
                        lambda *a: ecrit.append(a))
    _reglages(monkeypatch, zoho={"org": "read"})
    with pytest.raises(al.AccessLevelError) as e:
        al.set_level("group", 7, "zoho", al.FULL, "sub", org_id=1)
    assert e.value.code == "access_level_wider_than_org"
    assert not ecrit, "un refus n'écrit rien"
    al.set_level("group", 7, "zoho", "read", "sub", org_id=1)
    assert ecrit == [("group", "7", "zoho", al.KEY, "read", "sub")]


# --- un niveau par défaut autre que `full` : Search Console, lue dans sa description ----

GSC = "google_search_console"
LECTURE_GSC = "https://www.googleapis.com/auth/webmasters.readonly"


def test_la_declaration_de_search_console_vient_de_sa_description():
    """Ni `ACCESS_LEVELS` écrit à la main ni liste recopiée : les niveaux, l'implication,
    les scopes de chaque op et le défaut sont ceux de la description servie."""
    from oto_mcp.connectors import descriptions
    decl = al.DECLARED[GSC]
    assert decl.default == "read" and decl.widening_needs_consent is False
    auth = descriptions.charger(GSC)["connector"]["auth"]
    assert {n: list(s) for n, s in decl.levels.items()} == {
        n: v["scopes"] for n, v in auth["access_levels"].items()}
    assert decl.covered_ops("read", GSC) == set(decl.tools[GSC])
    assert decl.covered_ops("read", "google_search_console_act") == set()
    assert decl.covered_ops(al.FULL, "google_search_console_act") == {"add_sitemap"}
    assert LECTURE_GSC in decl.closure(al.FULL)


def test_sans_reglage_search_console_reste_en_lecture(monkeypatch):
    """Les clés posées avant les niveaux gardent leur lecture, sans le geste de personne."""
    _reglages(monkeypatch)
    assert al.resolve(GSC, 1, 2) == ("read", "default")
    _reglages(monkeypatch, google_search_console={"org": "full"})
    assert al.resolve(GSC, 1, 2) == (al.FULL, "org")


def test_une_equipe_ne_depasse_pas_le_defaut_de_son_org(monkeypatch):
    from oto_mcp.db import connector_settings
    monkeypatch.setattr(connector_settings, "set_connector_setting", lambda *a: None)
    _reglages(monkeypatch)
    with pytest.raises(al.AccessLevelError) as e:
        al.set_level("group", 7, GSC, al.FULL, "sub", org_id=1)
    assert e.value.code == "access_level_wider_than_org"
    _reglages(monkeypatch, google_search_console={"org": "full", "group": "full"})
    assert al.resolve(GSC, 1, 2) == (al.FULL, "group")


@pytest.mark.parametrize("data", [
    {"levels": {"full": ("A",)}, "tools": {}, "default": "read"},           # défaut non déclaré
    {"levels": {"full": ("A",)}, "tools": {}, "widening_needs_consent": 1},  # pas un bool
])
def test_un_defaut_mal_forme_leve(data):
    with pytest.raises(ValueError):
        al._valider("x", data)
