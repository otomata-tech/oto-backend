"""Search Console par connexion Google : le niveau `full` et `search_console_act`.

Ce que ce fichier verrouille :
1. la COUVERTURE — un compte qui détient `webmasters.readonly` OU `webmasters` couvre les
   lectures (les comptes connectés avant restent servis, sans reconnexion) ; seul
   `webmasters` couvre l'écriture ;
2. la BORNE de rafraîchissement — `webmasters`, une fois accordé, y reste ;
3. le CONSENTEMENT — les scopes du niveau en vigueur, jamais `webmasters` interrupteur
   d'instance éteint ; le niveau signé dans le state ;
4. l'INTERRUPTEUR d'instance `GOOGLE_SEARCH_CONSOLE_WRITE` — éteint par défaut : poser
   `full` est refusé, un `full` posé s'applique en `read` (source `instance`) ;
5. le RÉGLAGE rangé sous le nom du service, pas sous le compte Google qui le porte ;
6. l'OUTIL — PUT d'un segment encodé, refus nommés (`access_level_denied`,
   `search_console_reauthorize_required`, `owner_or_full_required`).
"""
import asyncio
import json

import pytest

from oto_mcp.auth import google as G
from oto_mcp.connectors import access_level_gate as gate
from oto_mcp.connectors import access_levels as al
from oto_mcp.mcp_errors import McpError

RO = "https://www.googleapis.com/auth/webmasters.readonly"
RW = "https://www.googleapis.com/auth/webmasters"
SC = "search_console"
SITE = "sc-domain:exemple.test"
FEED = "https://www.exemple.test/sitemap.xml"
ENV = "GOOGLE_SEARCH_CONSOLE_WRITE"


@pytest.fixture
def ecriture_ouverte(monkeypatch):
    monkeypatch.setenv(ENV, "1")


@pytest.fixture(autouse=True)
def _interrupteur_eteint(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)


def _reglages(monkeypatch, **par_connecteur):
    monkeypatch.setattr(al, "settings", lambda org, group: par_connecteur)


def _app():
    return G.OAuthApp(client_id="cid", client_secret="s", redirect_uri="https://x/cb",
                      origin="env")


# --- 1. couverture ----------------------------------------------------------------------

@pytest.mark.parametrize("scopes", [f"openid {RO}", f"openid {RW}", f"{RO} {RW}"])
def test_l_un_ou_l_autre_scope_couvre_les_lectures(scopes):
    assert SC in G.services_granted(scopes)


def test_le_tuple_du_service_reste_la_lecture():
    """La leçon de Chat : changer ce tuple ferait sortir tous les comptes existants."""
    assert G.SERVICE_SCOPES[SC] == (RO,)
    assert RW in G.KNOWN_SCOPES and G.SCOPE_IMPLIES[RW] == (RO,)


@pytest.fixture
def ligne(monkeypatch):
    """`credentials_for` jusqu'au contrôle des scopes : la ligne résolue est posée ici."""
    from oto_mcp import access
    etat = {"scopes": f"openid {RO}"}
    monkeypatch.setattr(G, "_refuse_two_names", lambda account: None)
    monkeypatch.setattr(G, "_ctx_org", lambda sub: 1)
    monkeypatch.setattr(access, "resolve_credential", lambda *a, **k: object())
    monkeypatch.setattr(G, "_resolved_row", lambda rc: {
        "google_email": "a@exemple.test", "refresh_token": "1//r",
        "scopes": etat["scopes"], "client_id": "cid"})
    monkeypatch.setattr(G, "config_dashboard", lambda sub: "https://app.exemple.test")
    return etat


def test_un_compte_en_lecture_seule_ne_couvre_pas_l_ecriture(ligne):
    with pytest.raises(G.GoogleScopeInsufficient, match="re-authorize it from the"):
        G.credentials_for("u1", service=SC, require_scopes=(RW,))


def test_un_compte_webmasters_passe_le_controle(ligne, monkeypatch):
    ligne["scopes"] = f"openid {RW}"

    class _Passe(Exception):
        pass

    def _apres(*a, **k):
        raise _Passe

    monkeypatch.setattr(G, "app_for", _apres)
    with pytest.raises(_Passe):
        G.credentials_for("u1", service=SC, require_scopes=(RW,))


# --- 2. borne de rafraîchissement -------------------------------------------------------

def test_webmasters_reste_dans_la_borne():
    enregistre = f"openid {G.IDENTITY_SCOPES[1]} {RO} {RW}"
    assert G._declared_scopes(enregistre) == enregistre
    avec_gmail = f"{enregistre} https://www.googleapis.com/auth/gmail.modify"
    repli = G._service_bound(avec_gmail, SC)
    assert repli is not None and RW in repli.split() and RO in repli.split()
    assert "gmail" not in repli


# --- 3. consentement --------------------------------------------------------------------

def test_scopes_du_consentement_par_niveau(ecriture_ouverte):
    assert G.scopes_for(SC, _app()) == [*G.IDENTITY_SCOPES, RO]
    assert G.scopes_for(SC, _app(), "read") == [*G.IDENTITY_SCOPES, RO]
    assert G.scopes_for(SC, _app(), "full") == [*G.IDENTITY_SCOPES, RW]


def test_interrupteur_eteint_jamais_webmasters():
    assert G.scopes_for(SC, _app(), "full") == [*G.IDENTITY_SCOPES, RO]


def test_le_niveau_du_consentement_suit_le_reglage(monkeypatch, ecriture_ouverte):
    _reglages(monkeypatch)
    assert G.consent_level(SC, 1, None) == "read"
    _reglages(monkeypatch, search_console={"org": "full"})
    assert G.consent_level(SC, 1, None) == "full"
    _reglages(monkeypatch, search_console={"org": "full", "group": "read"})
    assert G.consent_level(SC, 1, 2) == "read"
    assert G.consent_level("drive", 1, None) is None


def test_build_auth_url_demande_webmasters_en_full(monkeypatch, ecriture_ouverte):
    from urllib.parse import parse_qs, urlparse

    from oto_mcp import access
    from oto_mcp.connectors import activation_gate
    monkeypatch.setattr(activation_gate, "exiger_connectable_capacite", lambda *a, **k: None)
    monkeypatch.setattr(G, "_ctx_org", lambda sub: 1)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(G, "app_for", lambda sub: _app())
    signes = []
    monkeypatch.setattr(G.oauth_flow, "sign_state", lambda aud, p: signes.append(p) or "st")
    _reglages(monkeypatch, search_console={"org": "full"})
    url = G.build_auth_url("u1", connector=SC)
    assert parse_qs(urlparse(url).query)["scope"] == [" ".join([*G.IDENTITY_SCOPES, RW])]
    assert signes[-1]["l"] == "full" and signes[-1]["c"] == SC
    _reglages(monkeypatch)
    url = G.build_auth_url("u1", connector=SC)
    assert parse_qs(urlparse(url).query)["scope"] == [" ".join([*G.IDENTITY_SCOPES, RO])]
    G.build_auth_url("u1", connector="drive")
    assert "l" not in signes[-1]


def test_le_state_rejette_un_niveau_inconnu(monkeypatch):
    base = {"sub": "u1", "org": 1, "c": SC, "s": "member", "jti": "j"}
    monkeypatch.setattr(G.oauth_flow, "read_state", lambda *a, **k: dict(base, l="full"))
    assert G.read_state("x")["level"] == "full"
    monkeypatch.setattr(G.oauth_flow, "read_state", lambda *a, **k: dict(base, l="admin"))
    assert G.read_state("x") is None
    monkeypatch.setattr(G.oauth_flow, "read_state",
                        lambda *a, **k: dict(base, c="drive", l="full"))
    assert G.read_state("x") is None


# --- 4 & 5. interrupteur d'instance et rangement du réglage ------------------------------

def test_le_reglage_est_range_sous_le_service():
    assert al.setting_connector(SC) == SC
    assert al.setting_connector("zoho") == "zoho"


def test_declaration():
    decl = al.DECLARED[SC]
    assert decl.default == "read" and decl.widening_needs_consent is True
    assert decl.covered_ops("read", SC) == set(decl.tools[SC])
    assert decl.covered_ops("read", "search_console_act") == set()
    assert decl.covered_ops("full", "search_console_act") == {"add_sitemap"}
    assert decl.instance_gates == {"full": ENV}


def test_eteint_un_full_pose_s_applique_en_read(monkeypatch):
    _reglages(monkeypatch, search_console={"org": "full"})
    assert al.resolve(SC, 1, None) == ("read", al.INSTANCE)
    assert gate.hidden_tools({SC, "search_console_act"}, 1, None) == {"search_console_act"}


def test_allume_full_s_applique(monkeypatch, ecriture_ouverte):
    _reglages(monkeypatch, search_console={"org": "full"})
    assert al.resolve(SC, 1, None) == ("full", "org")
    assert gate.hidden_tools({SC, "search_console_act"}, 1, None) == set()


def test_eteint_poser_full_est_refuse(monkeypatch):
    from oto_mcp.db import connector_settings
    poses = []
    monkeypatch.setattr(connector_settings, "set_connector_setting",
                        lambda *a: poses.append(a))
    _reglages(monkeypatch)
    with pytest.raises(al.AccessLevelError) as e:
        al.set_level("org", 1, SC, "full", "sub")
    assert e.value.code == "access_level_off_on_instance" and ENV in str(e.value)
    al.set_level("org", 1, SC, "read", "sub")
    assert poses[-1][2:4] == (SC, al.KEY)


def test_allume_poser_full_passe_sous_le_service(monkeypatch, ecriture_ouverte):
    from oto_mcp.db import connector_settings
    poses = []
    monkeypatch.setattr(connector_settings, "set_connector_setting",
                        lambda *a: poses.append(a))
    _reglages(monkeypatch)
    al.set_level("org", 1, SC, "full", "sub")
    assert poses == [("org", "1", SC, al.KEY, "full", "sub")]


def test_un_interrupteur_illisible_leve(monkeypatch):
    monkeypatch.setenv(ENV, "peut-etre")
    with pytest.raises(Exception, match=ENV):
        al.DECLARED[SC].gate_of("full")


def test_le_refus_de_la_garde_nomme_l_interrupteur():
    decl = al.DECLARED[SC]
    refus = gate._verdict(decl, "search_console_act", "add_sitemap",
                          [(1, None, "read", al.INSTANCE)])
    assert refus is not None and refus.data["code"] == "access_level_denied"
    assert ENV in refus.message and "platform operator" in refus.message
    assert gate._verdict(decl, SC, "list_sites", [(1, None, "read", "default")]) is None


def test_sans_reglage_interrupteur_eteint_le_geste_est_celui_de_l_operateur():
    """Le défaut déployé : aucune org ne pose rien, l'interrupteur est éteint. Dire
    « posez `full` » enverrait l'admin vers un refus : le geste nommé est l'opérateur."""
    decl = al.DECLARED[SC]
    refus = gate._verdict(decl, "search_console_act", "add_sitemap",
                          [(1, None, "read", "default")])
    assert ENV in refus.message and "platform operator" in refus.message


def test_interrupteur_allume_le_geste_est_l_admin(ecriture_ouverte):
    decl = al.DECLARED[SC]
    refus = gate._verdict(decl, "search_console_act", "add_sitemap",
                          [(1, None, "read", "default")])
    assert "level='full'" in refus.message and ENV not in refus.message


# --- 6. l'outil -------------------------------------------------------------------------

class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self.content = json.dumps(body).encode() if body is not None else b""
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def outil(monkeypatch):
    from fastmcp import FastMCP
    from oto_mcp.tools import search_console as T

    etat = {"calls": [], "reply": (200, None), "level": "full", "scopes": []}

    def request(method, url, **kw):
        etat["calls"].append({"method": method, "url": url, **kw})
        return _Resp(*etat["reply"])

    def token(account=None, require_scopes=()):
        etat["scopes"].append(require_scopes)
        return "ya29.jeton"

    monkeypatch.setattr(T.requests, "request", request)
    monkeypatch.setattr(T, "_token_for_user", token)
    monkeypatch.setattr(T.access_level_gate, "level_of_the_call", lambda c: etat["level"])
    m = FastMCP("t")
    T.register(m)
    fn = asyncio.run(m.get_tool("search_console_act")).fn
    etat["run"] = lambda **kw: asyncio.run(fn(**kw))
    return etat


def test_add_sitemap_put_d_un_segment(outil, ecriture_ouverte):
    assert outil["run"](site_url=SITE, feedpath=FEED) == {}
    (c,) = outil["calls"]
    assert c["method"] == "PUT"
    assert c["url"] == ("https://searchconsole.googleapis.com/webmasters/v3/sites/"
                        "sc-domain%3Aexemple.test/sitemaps/"
                        "https%3A%2F%2Fwww.exemple.test%2Fsitemap.xml")
    assert c["headers"]["Authorization"] == "Bearer ya29.jeton"
    assert outil["scopes"] == [(RW,)]


def test_l_outil_est_un_acte():
    from fastmcp import FastMCP
    from oto_mcp.tools import search_console as T
    m = FastMCP("t")
    T.register(m)
    tool = asyncio.run(m.get_tool("search_console_act"))
    assert tool.annotations.readOnlyHint is False and tool.annotations.destructiveHint
    assert "google_search_console_act" in tool.description


def test_niveau_read_access_level_denied(outil, ecriture_ouverte):
    outil["level"] = "read"
    with pytest.raises(McpError) as e:
        outil["run"](site_url=SITE, feedpath=FEED)
    assert e.value.error.data["code"] == "access_level_denied"
    assert outil["calls"] == []


def test_interrupteur_eteint_access_level_denied(outil):
    with pytest.raises(McpError) as e:
        outil["run"](site_url=SITE, feedpath=FEED)
    assert e.value.error.data["code"] == "access_level_denied"
    assert ENV in e.value.error.message and outil["calls"] == []


def test_utilisateur_restreint_owner_or_full_required(outil, ecriture_ouverte):
    outil["reply"] = (403, {"error": {"code": 403, "message": (
        "User does not have sufficient permission for site 'sc-domain:exemple.test'."),
        "errors": [{"reason": "forbidden"}]}})
    with pytest.raises(McpError) as e:
        outil["run"](site_url=SITE, feedpath=FEED)
    assert e.value.error.data["code"] == "owner_or_full_required"


def test_jeton_sans_le_scope_reautoriser(outil, ecriture_ouverte):
    outil["reply"] = (403, {"error": {"code": 403, "status": "PERMISSION_DENIED",
                                      "message": "Request had insufficient authentication scopes.",
                                      "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}})
    with pytest.raises(McpError) as e:
        outil["run"](site_url=SITE, feedpath=FEED)
    assert e.value.error.data["code"] == "search_console_reauthorize_required"


def test_compte_en_lecture_seule_reautoriser(monkeypatch):
    from oto_mcp.tools import search_console as T

    def credentials_for(sub, account=None, service=None, require_scopes=()):
        raise G.GoogleScopeInsufficient("re-authorize it from the card")

    monkeypatch.setattr(T.access, "current_user_sub_or_raise", lambda: "u1")
    monkeypatch.setattr(T.google_oauth, "credentials_for", credentials_for)
    with pytest.raises(McpError) as e:
        T._token_for_user(None, (RW,))
    assert e.value.error.data["code"] == "search_console_reauthorize_required"


@pytest.mark.parametrize("kw", [
    {"site_url": "exemple.test", "feedpath": FEED},
    {"site_url": SITE, "feedpath": "sitemap.xml"},
    {"site_url": SITE, "feedpath": "ftp://www.exemple.test/sitemap.xml"},
    {"site_url": SITE, "feedpath": ""},
])
def test_arguments_refuses_avant_tout_appel(outil, ecriture_ouverte, kw):
    with pytest.raises(McpError):
        outil["run"](**kw)
    assert outil["calls"] == []
