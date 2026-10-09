"""Le relais d'autorisation : la réponse porte `iss` = l'`issuer` que la façade annonce.

**Le défaut tenu fermé (mesuré le 08/09/2026).** La métadonnée annonçait `issuer` = le host
de la façade, mais la réponse d'autorisation sortait de Logto, estampillée `iss` = SON
émetteur. Le SDK MCP Python 2.0 applique RFC 9207 et refusait le flux juste après la
connexion (« Authorization response iss mismatch »), sans aucun jeton émis — deux fois en
production, sur le host d'un tenant.

Ce banc tient ce que la façade SERT, contre un annuaire factice :
- **la déclaration décide** : un host non déclaré sert la métadonnée d'avant, un host déclaré
  annonce le relais sur un chemin NEUF, sans drapeau RFC 9207 ; déclarer sans secret de
  sceau empêche le démarrage ;
- **l'autorisation relayée** ne change que `redirect_uri` et `state`, pour un rappel
  enregistré À L'OCTET PRÈS sur l'application d'un annuaire administré ; tout le reste reçoit
  une erreur NOMMÉE, jamais le trajet direct ;
- **le retour** rend le client à SON rappel, paramètres de réponse remplacés, `iss` = l'issuer
  servi, et refuse un sceau ou un émetteur qui ne sont pas les siens ;
- **l'échange de jeton** ne sert que le client de l'application, sans authentification client
  relayée, refuse sur un host non déclaré (sauf un code déjà relayé), remet le rappel de la
  façade pour un code marqué et transmet le reste à l'octet près.
Le parcours complet par un vrai client MCP est dans `test_authorization_relay_client_mcp.py`.
"""
from __future__ import annotations

import asyncio
import logging
import time
import types
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlparse

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oto_mcp import tenancy
from oto_mcp.auth import facade, relay, relay_maintenance, relay_seals

_SECRET = b"secret-de-banc"
_R = "http://127.0.0.1:27890/callback"
_TENANT = "mcp.acme.test"
_BASE_TENANT = "https://mcp.acme.test"
_POSTER_REEL = relay._poster      # capturé avant que le banc ne le remplace


# ── les sceaux ────────────────────────────────────────────────────────────────

def test_un_sceau_rend_le_rappel_et_le_state_du_client():
    etat = relay_seals.sceller_etat(_SECRET, _BASE_TENANT, _R, "s/a+b==~x")
    ouvert, raison = relay_seals.ouvrir_etat(_SECRET, etat, _BASE_TENANT)
    assert (ouvert[:2], raison) == ((_R, "s/a+b==~x"), "")


@pytest.mark.parametrize("alteration, raison", [
    ("autre_secret", "sig"), ("autre_host", "host"), ("expire", "ttl"),
    ("charge", "sig"), ("illisible", "format")])
def test_un_sceau_qui_nest_pas_le_sien_est_refuse_en_nommant_la_cause(alteration, raison):
    etat = relay_seals.sceller_etat(_SECRET, _BASE_TENANT, _R, "s")
    cle, base = _SECRET, _BASE_TENANT
    if alteration == "autre_secret":
        cle = b"autre"
    elif alteration == "autre_host":
        base = "https://mcp.oto.cx"      # un sceau d'un host ne se rejoue pas sur un autre
    elif alteration == "expire":
        etat = relay_seals.sceller_etat(_SECRET, base, _R, "s",
                                        maintenant=int(time.time()) - relay_seals.ETAT_TTL - 1)
    elif alteration == "charge":
        charge, sig = etat.split(".")
        faux = relay_seals._b64(relay_seals._unb64(charge).replace(b"127.0.0.1", b"evil.test.x"))
        etat = f"{faux}.{sig}"
    else:
        etat = "pas-un-sceau"
    assert relay_seals.ouvrir_etat(cle, etat, base) == (None, raison)


def test_un_sceau_ne_vit_que_le_temps_dune_connexion():
    etat = relay_seals.sceller_etat(_SECRET, _BASE_TENANT, _R, "s",
                                    maintenant=int(time.time()) - 11 * 60)
    assert relay_seals.ouvrir_etat(_SECRET, etat, _BASE_TENANT) == (None, "ttl")


def test_un_code_marque_ne_revient_quau_rappel_et_au_host_qui_lont_demande():
    code = relay_seals.marquer_code(_SECRET, _BASE_TENANT, _R, "codeLogto_1-x")
    assert relay_seals.lire_code(_SECRET, _BASE_TENANT, _R, code) == "codeLogto_1-x"
    assert relay_seals.lire_code(_SECRET, _BASE_TENANT, "http://127.0.0.1:1/cb", code) is None
    assert relay_seals.lire_code(_SECRET, "https://mcp.oto.cx", _R, code) is None
    assert relay_seals.lire_code(_SECRET, _BASE_TENANT, None, code) is None


def test_la_reecriture_ne_touche_que_les_segments_nommes():
    requete = ("response_type=code&client_id=app&redirect_uri=http%3A%2F%2F127.0.0.1%3A27890"
               "%2Fcallback&state=s%2Fa%2Bb%3D%3D~x&code_challenge=Zx-9_aQ"
               "&code_challenge_method=S256&scope=openid+offline_access"
               "&resource=https%3A%2F%2Fa.test%2Fmcp&resource=https%3A%2F%2Fb.test%2Fmcp")
    sortie = relay.reecrire(requete, {"redirect_uri": "https://h/oauth/callback", "state": "S.x"})
    hors = ("redirect_uri=", "state=")
    assert [s for s in sortie.split("&") if not s.startswith(hors)] == \
        [s for s in requete.split("&") if not s.startswith(hors)]
    assert parse_qs(sortie)["redirect_uri"] == ["https://h/oauth/callback"]


def test_le_retour_laisse_la_requete_du_rappel_a_loctet_pres():
    sortie = relay.retour("http://127.0.0.1:27890/cb?a=b%20c&iss=glisse",
                          [("code", "k"), ("iss", "https://h/")])
    assert sortie == "http://127.0.0.1:27890/cb?a=b%20c&code=k&iss=https%3A%2F%2Fh%2F"


# ── le banc des routes ────────────────────────────────────────────────────────

class _Annuaire:
    """Ce que Logto fait de ce que la façade lui transmet : l'échange de jeton, et
    l'application partagée dont elle relit et réécrit les rappels."""

    def __init__(self):
        self.echanges = []
        self.poses = []
        self.lectures = 0
        self.rappels = [_R, "https://claude.ai/api/mcp/auth_callback"]
        self.reponse = httpx.Response(200, json={"access_token": "at", "token_type": "Bearer"},
                                      headers={"cache-control": "no-store"})

    async def poster(self, url, corps, headers):
        self.echanges.append((url, corps, dict(headers)))
        if isinstance(self.reponse, Exception):
            raise self.reponse
        return self.reponse

    def enregistrer(self, app_id, uris, directory=None, *, cors_uris=None):
        self.poses.append((app_id, list(uris), cors_uris))
        self.rappels += [u for u in uris if u not in self.rappels]

    def relire(self, app_id, directory=None):
        self.lectures += 1
        return list(self.rappels)


@pytest.fixture
def annuaire(monkeypatch):
    a = _Annuaire()
    monkeypatch.setattr(relay, "_poster", a.poster)
    monkeypatch.setattr(facade, "_register_redirects", a.enregistrer)
    monkeypatch.setattr(facade, "_redirect_uris", a.relire)
    relay._seaux.clear()
    relay._rappels.clear()
    yield a
    relay._rappels.clear()


def _env(monkeypatch, declares=(_TENANT, "mcp.oto.cx")):
    monkeypatch.setenv("LOGTO_ENDPOINT", "https://auth.oto.ninja")
    monkeypatch.setenv("LOGTO_PUBLIC_ENDPOINT", "https://auth.oto.cx")
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", _SECRET.decode())
    monkeypatch.setenv("OTO_MCP_LOGTO_M2M_ID", "cid")
    monkeypatch.setenv("OTO_MCP_LOGTO_M2M_SECRET", "csec")
    monkeypatch.setenv("LOGTO_ACME_MGMT_ID", "cid")
    monkeypatch.setenv("LOGTO_ACME_MGMT_SECRET", "csec")
    monkeypatch.setenv("OTO_MCP_OAUTH_RELAY_HOSTS", ",".join(declares))


def _tenants(*sans_mgmt):
    rangs = [{"slug": "acme", "name": "Acme", "issuer": "https://auth.acme.test/oidc",
              "hosts": [_TENANT], "oauth_client_id": "app-acme",
              "logto_mgmt": {"token_endpoint": "https://admin.acme.test",
                             "api_endpoint": "https://auth.acme.test",
                             "credential": "LOGTO_ACME_MGMT"}}]
    rangs += [{"slug": h.split(".")[1], "name": h, "issuer": f"https://auth.{h}/oidc",
               "hosts": [h], "oauth_client_id": "app-sans"} for h in sans_mgmt]
    return rangs


@pytest.fixture
def registre():
    avant = tenancy.current()
    tenancy.install(tenancy.IssuerRegistry(tenancy.build(
        "https://auth.oto.ninja/oidc", tenants=_tenants("mcp.sans.test"))))
    yield
    tenancy.install(avant)


def _app():
    return Starlette(routes=facade.make_routes("https://mcp.oto.cx", "app-oto"))


@pytest.fixture
def client(monkeypatch, annuaire, registre):
    _env(monkeypatch)
    return TestClient(_app())


def _demande(client_id="app-acme", rappel=_R, extra="&scope=openid+offline_access"):
    return (f"response_type=code&client_id={quote(client_id, safe='')}"
            f"&redirect_uri={quote(rappel, safe='')}"
            f"&state=etat-du-client&code_challenge=Zx-9_aQ&code_challenge_method=S256{extra}")


def _autoriser(client, requete, host=_TENANT):
    return client.get(f"{relay.AUTHORIZE_PATH}?{requete}", headers={"host": host},
                      follow_redirects=False)


# ── la déclaration décide de ce qui est annoncé ───────────────────────────────

@pytest.mark.parametrize("chemin", ["/.well-known/oauth-authorization-server",
                                    "/.well-known/openid-configuration"])
@pytest.mark.parametrize("host", [_TENANT, "mcp.oto.cx"])
def test_un_host_declare_annonce_le_relais_sans_le_drapeau(client, chemin, host):
    md = client.get(chemin, headers={"host": host}).json()
    assert md["issuer"] == f"https://{host}/"
    assert md["authorization_endpoint"] == f"https://{host}/oauth/relay/authorize"
    assert md["token_endpoint"] == f"https://{host}/oauth/token"
    assert "authorization_response_iss_parameter_supported" not in md


def test_un_host_non_declare_garde_la_metadonnee_davant(monkeypatch, annuaire, registre):
    _env(monkeypatch, declares=())
    avant = facade.as_metadata(_BASE_TENANT, "https://auth.acme.test/oidc")
    assert TestClient(_app()).get("/.well-known/oauth-authorization-server",
                                  headers={"host": _TENANT}).json() == avant
    assert avant["authorization_endpoint"] == "https://auth.acme.test/oidc/auth"


def test_declarer_sans_secret_empeche_de_demarrer(monkeypatch):
    monkeypatch.setenv("OTO_MCP_OAUTH_RELAY_HOSTS", _TENANT)
    monkeypatch.delenv("OTO_MCP_OAUTH_STATE_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="OTO_MCP_OAUTH_STATE_SECRET"):
        relay.verifier_configuration()


def test_le_serveur_refuse_de_demarrer_avant_davoir_rien_prepare(monkeypatch):
    """Le refus est au DÉMARRAGE réel (`server.main`), avant la base et les boucles de fond."""
    from oto_mcp import boucles_de_fond, loop_watch, sentry_setup, server
    monkeypatch.setenv("OTO_MCP_OAUTH_RELAY_HOSTS", _TENANT)
    monkeypatch.delenv("OTO_MCP_OAUTH_STATE_SECRET", raising=False)
    monkeypatch.setattr(sentry_setup, "init_sentry", lambda: False)
    monkeypatch.setattr(loop_watch, "enable", lambda: None)
    monkeypatch.setattr(boucles_de_fond, "composer",
                        lambda: pytest.fail("le démarrage a continué malgré la configuration"))
    with pytest.raises(RuntimeError, match="OTO_MCP_OAUTH_RELAY_HOSTS"):
        server.main()


def test_lautorisation_oto202_reste_celle_davant_meme_declaree(client):
    """Un client qui a lu la métadonnée d'avant présente son code à Logto : lui rendre un
    code relayé le casserait. `/oauth/authorize` ne relaie donc jamais."""
    r = client.get(f"/oauth/authorize?{_demande(client_id='app-oto')}",
                   headers={"host": "mcp.oto.cx"}, follow_redirects=False)
    assert parse_qs(urlparse(r.headers["location"]).query)["redirect_uri"] == [_R]


# ── l'autorisation relayée ────────────────────────────────────────────────────

def test_sur_le_host_dun_tenant_lautorisation_est_relayee_chez_lui(client, annuaire):
    r = _autoriser(client, _demande())
    assert r.status_code == 302
    cible = urlparse(r.headers["location"])
    assert (cible.netloc, cible.path) == ("auth.acme.test", "/oidc/auth")
    lu = parse_qs(cible.query)
    assert lu["redirect_uri"] == [f"{_BASE_TENANT}/oauth/callback"]
    assert "prompt" not in lu, "le consentement d'un tenant est SA décision"
    ouvert, _ = relay_seals.ouvrir_etat(_SECRET, lu["state"][0], _BASE_TENANT)
    assert ouvert[:2] == (_R, "etat-du-client")
    assert annuaire.poses == [], "aucune écriture sur le chemin d'autorisation"


def test_sur_notre_host_lautorisation_relayee_garde_le_consentement(client):
    cible = urlparse(_autoriser(client, _demande(client_id="app-oto"), host="mcp.oto.cx")
                     .headers["location"])
    assert (cible.netloc, cible.path) == ("auth.oto.cx", "/oidc/auth")
    lu = parse_qs(cible.query)
    assert lu["redirect_uri"] == ["https://mcp.oto.cx/oauth/callback"]
    assert lu["prompt"] == ["consent"]


def _refus_nomme(r, statut, erreur):
    assert (r.status_code, r.json()["error"]) == (statut, erreur)
    assert "location" not in r.headers, "un refus ne redirige nulle part, trajet direct compris"
    assert r.json()["error_description"]


@pytest.mark.parametrize("requete, erreur", [
    (_demande(client_id="colle-a-la-main"), "invalid_client"),
    (_demande(rappel="https://attaquant.test/callback"), "invalid_request"),
    (_demande(rappel="http://127.0.0.1:1/callback"), "invalid_request"),  # autorisé, NON enregistré
    (_demande().replace("&code_challenge_method=S256", "&code_challenge_method=plain"),
     "invalid_request"),
    (_demande().replace("&code_challenge=Zx-9_aQ&code_challenge_method=S256", ""),
     "invalid_request"),
    (_demande() + "&response_mode=form_post", "invalid_request"),
    (_demande() + "&response_type=token", "invalid_request"),
    (_demande() + "&state=deux", "invalid_request"),
    (_demande() + "&request_uri=urn%3Ax", "invalid_request"),
    (_demande(rappel=f"{_BASE_TENANT}/oauth/callback"), "invalid_request"),
], ids=["autre_client", "rappel_hors_liste", "rappel_non_enregistre", "pkce_plain", "sans_pkce",
        "form_post", "response_type_repete", "state_repete", "request_uri", "rappel_facade"])
def test_ce_qui_ne_se_relaie_pas_recoit_une_erreur_nommee(client, annuaire, requete, erreur):
    annuaire.rappels.append(f"{_BASE_TENANT}/oauth/callback")   # posé, mais jamais un rappel client
    _refus_nomme(_autoriser(client, requete), 400, erreur)


def test_un_host_non_declare_refuse_le_chemin_du_relais(monkeypatch, client):
    monkeypatch.setenv("OTO_MCP_OAUTH_RELAY_HOSTS", "")
    _refus_nomme(_autoriser(client, _demande()), 400, "invalid_request")


def test_un_annuaire_non_administre_ne_se_relaie_pas(monkeypatch, annuaire, registre):
    """Comparer le rappel aux rappels ENREGISTRÉS exige de pouvoir les lire."""
    _env(monkeypatch, declares=("mcp.sans.test",))
    r = _autoriser(TestClient(_app()), _demande(client_id="app-sans"), host="mcp.sans.test")
    _refus_nomme(r, 503, "temporarily_unavailable")


def test_une_application_illisible_est_un_refus_nomme(client, monkeypatch):
    def _illisible(app_id, directory=None):
        raise ConnectionError("annuaire injoignable")
    monkeypatch.setattr(facade, "_redirect_uris", _illisible)
    _refus_nomme(_autoriser(client, _demande()), 503, "temporarily_unavailable")


def test_les_rappels_sont_relus_en_cache_court_et_oublies_par_la_dcr(client, annuaire):
    """Le client qui s'enregistre puis autorise dans la foulée trouve son rappel — sans que
    chaque autorisation relise l'application."""
    neuf = "http://127.0.0.1:27891/callback"
    _refus_nomme(_autoriser(client, _demande(rappel=neuf)), 400, "invalid_request")
    _autoriser(client, _demande())
    assert annuaire.lectures == 1, "relue une fois, puis servie du cache"
    assert client.post("/oauth/register", json={"redirect_uris": [neuf]},
                       headers={"host": _TENANT}).status_code == 201
    assert _autoriser(client, _demande(rappel=neuf)).status_code == 302
    assert annuaire.lectures == 2


# ── le retour ─────────────────────────────────────────────────────────────────

def _sceau(base=_BASE_TENANT, rappel=_R, etat="etat-du-client"):
    return relay_seals.sceller_etat(_SECRET, base, rappel, etat)


def _revenir(client, **params):
    return client.get(f"/oauth/callback?{urlencode(params)}", headers={"host": _TENANT},
                      follow_redirects=False)


def test_le_retour_porte_liss_servi_et_le_state_du_client(client):
    r = _revenir(client, code="codeLogto", state=_sceau(), iss="https://auth.acme.test/oidc")
    assert r.status_code == 302
    cible = urlparse(r.headers["location"])
    assert f"{cible.scheme}://{cible.netloc}{cible.path}" == _R
    lu = dict(parse_qsl(cible.query))
    md = client.get("/.well-known/oauth-authorization-server", headers={"host": _TENANT}).json()
    assert lu["iss"] == md["issuer"], "RFC 9207 : iss == issuer de la métadonnée SERVIE"
    assert lu["state"] == "etat-du-client"
    assert relay_seals.lire_code(_SECRET, _BASE_TENANT, _R, lu["code"]) == "codeLogto"


def test_un_iss_glisse_dans_le_rappel_est_remplace_pas_double(client):
    rappel = "http://127.0.0.1:27890/callback?iss=https%3A%2F%2Fattaquant.test&garde=1"
    r = _revenir(client, code="c", state=_sceau(rappel=rappel), iss="https://auth.acme.test/oidc")
    lu = parse_qs(urlparse(r.headers["location"]).query)
    assert lu["iss"] == [f"{_BASE_TENANT}/"]
    assert lu["garde"] == ["1"]


def test_une_erreur_revient_au_client_avec_liss_servi(client):
    r = _revenir(client, error="access_denied", error_description="refusé",
                 state=_sceau(), iss="https://auth.acme.test/oidc")
    assert dict(parse_qsl(urlparse(r.headers["location"]).query)) == {
        "error": "access_denied", "error_description": "refusé",
        "state": "etat-du-client", "iss": f"{_BASE_TENANT}/"}


@pytest.mark.parametrize("params", [
    {"code": "c", "state": "forge.x"},
    {"code": "c", "state": _sceau(base="https://mcp.oto.cx")},
    {"code": "c", "state": _sceau(), "iss": "https://auth.oto.ninja/oidc"},
    {"state": _sceau()},
], ids=["sceau_forge", "sceau_dun_autre_host", "emetteur_dun_autre_annuaire", "ni_code_ni_erreur"])
def test_un_retour_douteux_ne_redirige_nulle_part(client, params):
    r = _revenir(client, **params)
    assert r.status_code == 400
    assert "location" not in r.headers


def test_le_journal_dacces_ne_garde_pas_le_code(caplog):
    # Le filtre COMMUN du journal d'accès (posé par `server.main`), pas un filtre du relais.
    from oto_mcp.journal_secrets import MasqueCheminAcces

    lg = logging.getLogger("test.acces")
    lg.filters.clear()
    lg.addFilter(MasqueCheminAcces())
    with caplog.at_level("INFO", logger="test.acces"):
        lg.info('%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET",
                "/oauth/callback?code=SECRET&state=SCEAU&iss=https%3A%2F%2Fa", "1.1", 302)
    ligne = caplog.records[-1].getMessage()
    assert "SECRET" not in ligne and "SCEAU" not in ligne and "iss=" in ligne


# ── l'échange de jeton ────────────────────────────────────────────────────────

def _echanger(client, corps, host=_TENANT, **headers):
    if isinstance(corps, dict):
        corps = urlencode(corps)
    return client.post("/oauth/token", content=corps, headers={
        "host": host, "content-type": "application/x-www-form-urlencoded", **headers})


_RAFRAICHIR = "grant_type=refresh_token&refresh_token=rt&client_id=app-acme"


def test_un_code_marque_part_chez_lannuaire_avec_le_rappel_de_la_facade(client, annuaire):
    code = relay_seals.marquer_code(_SECRET, _BASE_TENANT, _R, "codeLogto")
    corps = (f"grant_type=authorization_code&code={code}&redirect_uri={quote(_R, safe='')}"
             "&code_verifier=" + "v" * 43 + "&client_id=app-acme"
             "&resource=https%3A%2F%2Fmcp.acme.test%2Fmcp&resource=https%3A%2F%2Fautre.test")
    r = _echanger(client, corps)
    assert r.status_code == 200 and r.json()["access_token"] == "at"
    url, envoye, headers = annuaire.echanges[0]
    assert url == "https://auth.acme.test/oidc/token"
    hors = ("code=", "redirect_uri=")
    assert [s for s in envoye.decode().split("&") if not s.startswith(hors)] == \
        [s for s in corps.split("&") if not s.startswith(hors)], "le reste part à l'octet près"
    lu = parse_qs(envoye.decode())
    assert (lu["code"], lu["redirect_uri"]) == (["codeLogto"], [f"{_BASE_TENANT}/oauth/callback"])
    assert headers["User-Agent"] == facade._UA
    assert "Authorization" not in headers


@pytest.mark.parametrize("corps, headers, erreur", [
    ("grant_type=refresh_token&refresh_token=rt", {}, "invalid_client"),
    ("grant_type=refresh_token&refresh_token=rt&client_id=autre", {}, "invalid_client"),
    ("grant_type=refresh_token&refresh_token=rt&client_id=app-acme&client_id=app-acme", {},
     "invalid_client"),
    (_RAFRAICHIR, {"authorization": "Basic YXBwLWFjbWU6"}, "invalid_client"),
    ("grant_type=client_credentials&client_id=app-acme", {}, "unsupported_grant_type"),
    ("grant_type=authorization_code&code=a&code=b&client_id=app-acme", {}, "invalid_request"),
    ("grant_type=authorization_code&code=a&client_id=app-acme", {}, "invalid_request"),
    ("grant_type=authorization_code&code=c&redirect_uri=a&redirect_uri=b&client_id=app-acme", {},
     "invalid_request"),
    ('{"grant_type": "refresh_token"}', {"content-type": "application/json"}, "invalid_request"),
], ids=["sans_client", "autre_client", "client_repete", "authentification_relayee",
        "grant_non_servi", "code_repete", "sans_rappel", "rappel_repete", "json"])
def test_un_echange_hors_contrat_ne_part_pas(client, annuaire, corps, headers, erreur):
    r = _echanger(client, corps, **headers)
    assert (r.status_code, r.json()["error"]) == (400, erreur)
    assert annuaire.echanges == []


@pytest.mark.parametrize("corps", [
    {"grant_type": "authorization_code", "redirect_uri": "http://127.0.0.1:1/callback"},
    {"grant_type": "authorization_code", "redirect_uri": f"{_BASE_TENANT}/oauth/callback",
     "demarque": True},
], ids=["autre_rappel", "marque_retiree"])
def test_un_code_detourne_nest_pas_transmis(client, annuaire, corps):
    code = relay_seals.marquer_code(_SECRET, _BASE_TENANT, _R, "codeLogto")
    corps = dict(corps, code="codeLogto" if corps.pop("demarque", False) else code,
                 code_verifier="v" * 43, client_id="app-acme")
    r = _echanger(client, corps)
    assert (r.status_code, r.json()["error"]) == (400, "invalid_grant")
    assert annuaire.echanges == []


@pytest.mark.parametrize("corps", [
    "grant_type=authorization_code&code=codeDuTrajetDirect&redirect_uri=http%3A%2F%2F127.0.0.1"
    "%3A27890%2Fcallback&code_verifier=vvv&client_id=app-acme",
    _RAFRAICHIR,
], ids=["code_sans_marque", "rafraichissement"])
def test_sur_un_host_declare_le_reste_part_tel_quel(client, annuaire, corps):
    assert _echanger(client, corps).status_code == 200
    assert annuaire.echanges[0][1] == corps.encode()


def test_un_host_non_declare_nechange_que_les_codes_deja_relayes(monkeypatch, client, annuaire):
    monkeypatch.setenv("OTO_MCP_OAUTH_RELAY_HOSTS", "")
    r = _echanger(client, _RAFRAICHIR)
    assert (r.status_code, r.json()["error"]) == (400, "invalid_request")
    code = relay_seals.marquer_code(_SECRET, _BASE_TENANT, _R, "codeLogto")
    assert _echanger(client, {"grant_type": "authorization_code", "code": code,
                              "redirect_uri": _R, "client_id": "app-acme"}).status_code == 200
    assert len(annuaire.echanges) == 1


def test_notre_host_echange_a_lorigine_de_notre_annuaire(client, annuaire):
    _echanger(client, "grant_type=refresh_token&refresh_token=rt&client_id=app-oto",
              host="mcp.oto.cx")
    assert annuaire.echanges[0][0] == "https://auth.oto.ninja/oidc/token"


def test_une_erreur_oauth_de_lannuaire_revient_telle_quelle(client, annuaire):
    annuaire.reponse = httpx.Response(400, json={"error": "invalid_grant"})
    r = _echanger(client, _RAFRAICHIR)
    assert (r.status_code, r.json()) == (400, {"error": "invalid_grant"})
    assert r.headers["access-control-allow-origin"] == "*"


@pytest.mark.parametrize("reponse, statut", [
    (httpx.Response(503, text="<html>maintenance</html>"), 502),
    (httpx.Response(403, text="<html>challenge</html>", headers={"content-type": "text/html"}), 502),
    (asyncio.TimeoutError(), 504),
    (httpx.ConnectError("refusé"), 502),
], ids=["5xx", "page_html", "delai", "injoignable"])
def test_une_panne_amont_rend_une_erreur_json_nommee(client, annuaire, reponse, statut):
    annuaire.reponse = reponse
    r = _echanger(client, _RAFRAICHIR)
    assert (r.status_code, r.json()["error"]) == (statut, "server_error")


@pytest.mark.parametrize("pair, xff", [
    ("testclient", "198.51.100.{i}"),                 # pair quelconque : l'en-tête ne compte pas
    ("127.0.0.1", "198.51.100.{i}, 203.0.113.9"),     # proxy de la box : son dernier segment
], ids=["pair_non_approuve", "proxy_de_la_box"])
def test_le_seau_par_ip_arrete_une_rafale_et_ne_se_contourne_pas(monkeypatch, annuaire, registre,
                                                                  pair, xff):
    _env(monkeypatch)
    # Horloge GELÉE, pour `relay` seul : `_seau_ok` recharge le seau au prorata du temps
    # RÉEL écoulé entre deux appels (`time.monotonic()`). Sur une machine chargée, le simple
    # aller-retour des 65 requêtes de cette rafale laisse passer assez de secondes pour
    # rendre un ou deux jetons — flaky en CI (`4 >= 5` puis vert à la relance). Une vraie
    # rafale tient dans un instant ; on le simule en figeant l'horloge QUE `relay` lit
    # (`relay.time`, pas le module `time` global — asyncio en dépend pour ses délais, y
    # compris ceux, réels, que `_client_http`/`_poster` peuvent encore attendre ailleurs).
    # Avec le temps figé le seau ne se remplit plus pendant la boucle : le compte de refus
    # devient exact, plus seulement une borne basse.
    monkeypatch.setattr(relay, "time", types.SimpleNamespace(monotonic=lambda: 1_000_000.0))
    c = TestClient(_app(), client=(pair, 50000))
    statuts = [_echanger(c, _RAFRAICHIR, **{"cf-connecting-ip": f"192.0.2.{i}",
                                            "x-forwarded-for": xff.format(i=i)}).status_code
               for i in range(int(relay._SEAU_RAFALE) + 5)]
    assert statuts.count(429) == 5, statuts


def test_le_proxy_de_la_box_distingue_ses_clients(monkeypatch, annuaire, registre):
    _env(monkeypatch)
    c = TestClient(_app(), client=("127.0.0.1", 50000))
    statuts = [_echanger(c, _RAFRAICHIR, **{"x-forwarded-for": f"198.51.100.{i}"}).status_code
               for i in range(int(relay._SEAU_RAFALE) + 5)]
    assert 429 not in statuts


def test_le_plafond_dechanges_en_vol_refuse_sans_appeler_lannuaire(client, annuaire, monkeypatch):
    monkeypatch.setattr(relay, "_en_vol", relay._EN_VOL_MAX)
    r = _echanger(client, _RAFRAICHIR)
    assert (r.status_code, r.json()["error"]) == (503, "temporarily_unavailable")
    assert annuaire.echanges == []


@pytest.mark.parametrize("annonce", [True, False], ids=["content_length", "lu_en_flux"])
def test_un_corps_trop_gros_nest_pas_mis_en_memoire(client, annuaire, annonce):
    gros = (_RAFRAICHIR + "&x=" + "x" * (relay._CORPS_MAX + 10)).encode()

    def _flux():
        for i in range(0, len(gros), 8192):
            yield gros[i:i + 8192]
    r = client.post("/oauth/token", content=gros if annonce else _flux(),
                    headers={"host": _TENANT, "content-type": "application/x-www-form-urlencoded"})
    assert (r.status_code, r.json()["error"]) == (413, "invalid_request")
    assert annuaire.echanges == []


def test_le_delai_de_lechange_est_borne_par_le_vrai_poster(client, monkeypatch):
    """Le vrai `_poster`, un annuaire qui ne répond pas : 504 nommé, au délai près."""
    monkeypatch.setattr(relay, "_JETON_TIMEOUT", 0.2)

    async def _jamais(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    async def _client_http():
        return httpx.AsyncClient(transport=httpx.MockTransport(_jamais))
    monkeypatch.setattr(relay, "_client_http", _client_http)
    monkeypatch.setattr(relay, "_poster", _POSTER_REEL)
    r = _echanger(client, _RAFRAICHIR)
    assert (r.status_code, r.json()["error"]) == (504, "server_error")


def test_le_preflight_du_jeton_est_servi(client):
    r = client.options("/oauth/token", headers={"host": _TENANT})
    assert r.status_code == 204
    assert r.headers["access-control-allow-headers"] == "content-type"


# ── le rappel de la façade se pose hors du chemin d'autorisation ──────────────

def test_une_dcr_sur_un_host_declare_repose_le_rappel_de_la_facade(client, annuaire):
    neuf = "http://127.0.0.1:27892/callback"
    r = client.post("/oauth/register", json={"redirect_uris": [neuf]}, headers={"host": _TENANT})
    assert r.status_code == 201
    assert annuaire.poses == [("app-acme", [neuf, f"{_BASE_TENANT}/oauth/callback"], [neuf])]


@pytest.fixture
def maintenance(monkeypatch, annuaire):
    from oto_mcp import db, server
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://mcp.oto.cx")
    monkeypatch.setenv("OTO_MCP_CLAUDE_APP_ID", "app-oto")

    def _installer(*declares, sans_mgmt=()):
        _env(monkeypatch, declares=declares)
        rangs = _tenants(*sans_mgmt)
        monkeypatch.setattr(db, "list_tenant_issuers", lambda: rangs)
        monkeypatch.setattr(server, "_registry_and_issuers", lambda: (tenancy.IssuerRegistry(
            tenancy.build("https://auth.oto.ninja/oidc", tenants=rangs)), {}))
    avant = tenancy.current()
    yield _installer
    tenancy.install(avant)


def test_la_maintenance_constate_a_blanc_puis_pose_avec_apply(maintenance, annuaire):
    annuaire.rappels = []
    maintenance(_TENANT, "mcp.oto.cx", "mcp.sans.test", "inconnu.test", sans_mgmt=["mcp.sans.test"])
    a_blanc = relay_maintenance.poser_les_rappels()
    assert a_blanc[_TENANT].startswith("absent") and annuaire.poses == []
    rendu = relay_maintenance.poser_les_rappels(dry_run=False)
    assert rendu[_TENANT] == "posé" and rendu["mcp.oto.cx"] == "posé"
    assert rendu["mcp.sans.test"].startswith("refusé : annuaire non administré")
    assert rendu["inconnu.test"].startswith("inconnu")
    assert all(cors == [] for _, _, cors in annuaire.poses), "le rappel du relais sans CORS"


def test_la_maintenance_constate_ce_quelle_relit_pas_ce_quelle_a_promis(maintenance, monkeypatch):
    maintenance(_TENANT)
    monkeypatch.setattr(facade, "_redirect_uris", lambda app_id, d=None: [])
    assert relay_maintenance.poser_les_rappels(dry_run=False)[_TENANT] == \
        "NON CONSTATÉ après écriture"


def test_un_host_en_echec_nempeche_pas_les_autres(maintenance, monkeypatch, annuaire):
    maintenance(_TENANT, "mcp.oto.cx")
    ecrire = annuaire.enregistrer

    def _echoue_chez_le_tenant(app_id, uris, directory=None, *, cors_uris=None):
        if app_id == "app-acme":
            raise facade.RedirectRegistrationFailed("PATCH refusé")
        ecrire(app_id, uris, directory, cors_uris=cors_uris)
    monkeypatch.setattr(facade, "_register_redirects", _echoue_chez_le_tenant)
    assert relay_maintenance.poser_les_rappels(dry_run=False) == \
        {_TENANT: "échec : RedirectRegistrationFailed", "mcp.oto.cx": "posé"}


def test_sans_host_declare_la_maintenance_le_dit(maintenance):
    maintenance()
    assert list(relay_maintenance.poser_les_rappels()) == ["(aucun)"]


def test_une_base_illisible_fait_echouer_la_maintenance(maintenance, monkeypatch):
    from oto_mcp import db
    maintenance(_TENANT)

    def _illisible():
        raise RuntimeError("DATABASE_URL not set")
    monkeypatch.setattr(db, "list_tenant_issuers", _illisible)
    with pytest.raises(RuntimeError):
        relay_maintenance.poser_les_rappels()


def test_la_commande_de_maintenance_est_un_acte_a_blanc(maintenance, annuaire):
    from oto_mcp import maintenance as m
    maintenance(_TENANT)
    assert m.main(["oauth-relay-callbacks", "--strict"]) == 0
    assert annuaire.poses == [], "sans --apply, rien n'est écrit"
    assert m.main(["oauth-relay-callbacks", "--apply", "--strict"]) == 0
    assert annuaire.poses and annuaire.poses[0][0] == "app-acme"


def test_une_ecriture_logto_nenvoie_que_la_colonne_qui_change(monkeypatch):
    """Logto remplace chaque colonne JSON entière : renvoyer `customClientMetadata`
    inchangé effacerait un réglage posé entre-temps dans la console."""
    monkeypatch.setenv("LOGTO_ENDPOINT", "https://auth.oto.ninja")
    monkeypatch.setattr(facade, "_mgmt_token", lambda d=None: "tok")
    monkeypatch.setattr(facade, "_application", lambda app_id, d: (
        {"redirectUris": ["https://claude.ai/api/mcp/auth_callback"]},
        {"corsAllowedOrigins": ["https://claude.ai"], "rotateRefreshToken": True}))
    envoyes = []

    class _R:
        def raise_for_status(self):
            return None
    monkeypatch.setattr("requests.patch", lambda url, json, **k: envoyes.append(json) or _R())
    facade._register_redirects("app", ["https://mcp.oto.cx/oauth/callback"], cors_uris=[])
    assert list(envoyes[0]) == ["oidcClientMetadata"]


# ── les gardes ajoutées, chacune prouvée ──────────────────────────────────────

@pytest.mark.parametrize("rappel", [
    "http://attaquant.test\\@127.0.0.1/cb",       # barre oblique inverse (WHATWG ≠ urlsplit)
    "https://claude.ai/api/mcp/auth_callback/../../x",
    "https://claude.ai/api/mcp/auth_callback\\..\\..\\x",  # `\\` = `/` pour un navigateur
    "https://claude.ai/api/mcp/auth_callbackXYZ",          # préfixe sans borne de segment
    "https://chatgpt.com/connector/oauth/abc/def",         # plus d'un segment
    "http://127.0.0.1:27890/%2e%2E/cb",
    "http://127.0.0.1:27890/cb#fragment",
    "http://127.0.0.1:27890/cb?x=é",
    "http://127.0.0.1:99999/cb",
    "http://user@127.0.0.1/cb",
    "http://127.0.0.1%2F@attaquant.test/cb",
    "http://[::1/cb",                              # `urlsplit` lève : refus nommé, pas 500
])
def test_un_rappel_ambigu_est_refuse_a_la_dcr_comme_au_relais(client, annuaire, rappel):
    r = client.post("/oauth/register", json={"redirect_uris": [rappel]}, headers={"host": _TENANT})
    assert (r.status_code, r.json()["error"]) == (400, "invalid_redirect_uri")
    annuaire.rappels.append(rappel)        # même posé à la main : jamais relayé
    _refus_nomme(_autoriser(client, _demande(rappel=rappel)), 400, "invalid_request")


@pytest.mark.parametrize("rappel", [
    "http://127.0.0.1:27890/cb%2Ehtml",            # contient %2E, ne se résout pas en `.`
    "http://[::1]:8080/callback",
    "https://claude.ai/api/mcp/auth_callback",
    "https://chatgpt.com/connector/oauth/abc_12-3",
    "https://callback.mistral.ai/v1/integrations_auth/oauth2_callback",
])
def test_un_rappel_canonique_reste_accepte(rappel):
    assert facade._redirect_ok(rappel) is True


def test_une_valeur_ecrite_par_le_client_ne_forge_pas_de_ligne_de_journal(client, caplog):
    faux = "evil\noauth.relay token grant=refresh_token upstream=200"
    with caplog.at_level("INFO", logger="oto_mcp.oauth_facade"):
        _autoriser(client, _demande(client_id=faux))
        _revenir(client, error=faux, state=_sceau(), iss="https://auth.acme.test/oidc")
    assert caplog.records and all("\n" not in r.getMessage() for r in caplog.records)


def test_une_etiquette_non_ascii_est_un_refus_nomme(client, annuaire):
    r = _echanger(client, "grant_type=authorization_code&code=oto1.%C3%A9.abc"
                          "&redirect_uri=http%3A%2F%2F127.0.0.1%3A27890%2Fcallback&client_id=app-acme")
    assert (r.status_code, r.json()["error"]) == (400, "invalid_grant")
    assert annuaire.echanges == []


def test_un_content_length_trop_grand_est_refuse_sans_lire_le_corps():
    class _Requete:
        headers = {"content-length": str(relay._CORPS_MAX + 1)}

        async def stream(self):
            pytest.fail("le corps a été lu alors que Content-Length le refusait déjà")
            yield b""
    assert asyncio.run(relay._corps_borne(_Requete())) is None
