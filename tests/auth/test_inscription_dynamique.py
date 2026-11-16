"""L'inscription dynamique (RFC 7591) des clients MCP sans CIMD, devant Logto.

Liste fermée des adresses de retour, une application par type et adresses de retour,
plafond des seules adresses variables, et aucune panne masquée : Logto injoignable ou
API de gestion en panne lèvent. La forme de Le Chat est celle relevée sur banc (client
confidentiel, secret dans le corps, scope sans `offline_access`).

Logto est doublé au niveau de `requests` (aucun appel réseau) ; le jeton de gestion par
`annuaire._mgmt_token`.
"""
from __future__ import annotations

import logging

import pytest
import requests
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oto_mcp.auth import annuaire, inscription_dynamique as dcr

LOGTO = "https://auth.example.test"
MISTRAL = "https://callback.mistral.ai/v1/integrations_auth/oauth2_callback"
CLAUDE = "https://claude.ai/api/mcp/auth_callback"
CHATGPT = "https://chatgpt.com/connector/oauth/abc123"
GEMINI = ("https://oauth-redirect.googleusercontent.com/r/"
          "user_bound_custom-mcp-112233445566778899-mcp_exemple_test")
GROK = "https://grok.com/connectors-oauth-exchange-code/"
GEMINI_PREFIXE = "https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-"
LE_CHAT = {
    "client_name": "Le Chat",
    "redirect_uris": [MISTRAL],
    "token_endpoint_auth_method": "client_secret_post",
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "scope": "openid email profile",
}
DESCRIPTION = {
    "issuer": f"{LOGTO}/oidc",
    "authorization_endpoint": f"{LOGTO}/oidc/auth",
    "token_endpoint": f"{LOGTO}/oidc/token",
    "client_id_metadata_document_supported": True,
}


class _Rep:
    def __init__(self, status: int, corps):
        self.status_code = status
        self._corps = corps

    def json(self):
        return self._corps

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class FauxLogto:
    """Le Logto du banc : la description, et l'API de gestion des applications."""

    def __init__(self, applications=None, *, secrets=True, panne_gestion=False):
        self.applications = list(applications or [])
        self.creees: list[dict] = []
        self.secrets = secrets
        self.panne_gestion = panne_gestion
        self.description = (200, DESCRIPTION)

    def get(self, url, params=None, headers=None, timeout=None):
        if url == f"{LOGTO}/oidc/.well-known/openid-configuration":
            if isinstance(self.description, Exception):
                raise self.description
            return _Rep(*self.description)
        assert headers["Authorization"] == "Bearer jeton-de-gestion"
        if self.panne_gestion:
            return _Rep(503, {})
        if url == f"{LOGTO}/api/applications":
            debut = (params["page"] - 1) * params["page_size"]
            return _Rep(200, self.applications[debut:debut + params["page_size"]])
        if url.startswith(f"{LOGTO}/api/applications/") and url.endswith("/secrets"):
            app_id = url.split("/")[-2]
            return _Rep(200, [{"name": "s", "value": f"secret-de-{app_id}"}]
                        if self.secrets else [])
        raise AssertionError(f"GET inattendu : {url}")

    def post(self, url, json=None, headers=None, timeout=None):
        assert url == f"{LOGTO}/api/applications", url
        assert headers["Authorization"] == "Bearer jeton-de-gestion"
        if self.panne_gestion:
            return _Rep(500, {})
        app = {"id": f"app{len(self.applications) + 1}", **json}
        self.applications.append(app)
        self.creees.append(app)
        return _Rep(200, app)


def _app_dcr(i: int, uris: list[str], type_: str = "Traditional") -> dict:
    return {"id": f"ancienne{i}", "name": f"{dcr.PREFIXE_DCR}client {i}", "type": type_,
            "oidcClientMetadata": {"redirectUris": uris}}


@pytest.fixture
def logto(monkeypatch):
    faux = FauxLogto()
    monkeypatch.setenv("LOGTO_ENDPOINT", LOGTO)
    monkeypatch.setattr(annuaire, "_mgmt_token", lambda directory=None: "jeton-de-gestion")
    monkeypatch.setattr(requests, "get", faux.get)
    monkeypatch.setattr(requests, "post", faux.post)
    return faux


@pytest.fixture
def client(logto):
    return TestClient(Starlette(routes=dcr.make_routes()), raise_server_exceptions=False)


# ── La forme réelle de Le Chat ───────────────────────────────────────────────

def test_le_chat_confidentiel_inscrit_en_traditional_avec_secret(client, logto, caplog):
    caplog.set_level(logging.INFO, logger="oto_mcp.inscription_dynamique")
    r = client.post("/oauth/register", json=LE_CHAT)

    assert r.status_code == 201, r.text
    assert r.headers["cache-control"] == "no-store"
    corps = r.json()
    assert corps["client_id"] == "app1"
    assert corps["client_secret"] == "secret-de-app1"
    assert corps["client_secret_expires_at"] == 0
    assert isinstance(corps["client_id_issued_at"], int)
    assert corps["client_name"] == "Le Chat"
    assert corps["redirect_uris"] == [MISTRAL]
    assert corps["grant_types"] == ["authorization_code", "refresh_token"]
    assert corps["response_types"] == ["code"]
    assert corps["token_endpoint_auth_method"] == "client_secret_post"
    [creee] = logto.creees
    assert creee["type"] == "Traditional"
    assert creee["name"] == f"{dcr.PREFIXE_DCR}Le Chat"
    assert creee["oidcClientMetadata"] == {"redirectUris": [MISTRAL],
                                           "postLogoutRedirectUris": []}
    # Le secret n'est jamais journalisé.
    assert "secret-de-app1" not in caplog.text
    assert "app1" in caplog.text


# ── Les adresses de retour ───────────────────────────────────────────────────

@pytest.mark.parametrize("uri,admise", [
    (MISTRAL, True),
    (CLAUDE, True),
    ("https://claude.com/api/mcp/auth_callback", True),
    (CHATGPT, True),
    (f"{MISTRAL}/extra", False),
    ("https://callback.mistral.ai.evil.example/v1/integrations_auth/oauth2_callback", False),
    ("https://chatgpt.com/connector/oauthx", False),
    ("https://chatgpt.com/connector_platform_oauth_redirect", False),
    ("https://chatgpt.com/connector/oauth/", False),
    ("https://chatgpt.com/connector/oauth/a/../../ailleurs", False),
    ("https://chatgpt.com/connector/oauth/abc?x=https://evil.example", False),
    ("http://localhost:3000/callback", False),
    (GEMINI, True),
    (GEMINI_PREFIXE, False),
    (f"{GEMINI}/autre", False),
    (f"{GEMINI}?next=https://evil.example", False),
    (f"{GEMINI}#x", False),
    ("https://oauth-redirect.googleusercontent.com/r/autre-chose", False),
    ("http://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-1-x", False),
    ("https://oauth-redirect.googleusercontent.com.evil.example/r/"
     "user_bound_custom-mcp-1-x", False),
    (GEMINI_PREFIXE + "a" * 201, False),
    (GROK, True),
    ("https://grok.com/connectors/oauth/callback", False),
    (GROK.rstrip("/"), False),
    (f"{GROK}x", False),
    (f"{GROK}?next=https://evil.example", False),
    ("http://grok.com/connectors-oauth-exchange-code/", False),
    ("https://grok.com.evil.example/connectors-oauth-exchange-code/", False),
    ("https://x.ai/connectors-oauth-exchange-code/", False),
])
def test_liste_fermee(uri, admise):
    assert dcr.retour_admis(uri) is admise


def test_une_adresse_hors_liste_refuse_toute_la_demande(client, logto, caplog):
    r = client.post("/oauth/register",
                    json={"redirect_uris": [MISTRAL, "https://evil.example/cb"]})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_redirect_uri"
    assert "evil.example" in r.json()["error_description"]
    assert logto.creees == []
    assert "inscription refusée" in caplog.text


@pytest.mark.parametrize("corps", [{}, {"redirect_uris": []}, {"redirect_uris": "x"}])
def test_redirect_uris_obligatoire(client, logto, corps):
    r = client.post("/oauth/register", json=corps)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_redirect_uri"


def test_prefixe_chatgpt_admis(client, logto):
    r = client.post("/oauth/register", json={"redirect_uris": [CHATGPT]})
    assert r.status_code == 201
    assert r.json()["redirect_uris"] == [CHATGPT]
    # Sans client_name : l'hôte de la première adresse.
    assert logto.creees[0]["name"] == f"{dcr.PREFIXE_DCR}chatgpt.com"


def test_gemini_obtient_un_client_confidentiel(client, logto):
    r = client.post("/oauth/register",
                    json={"client_name": "Gemini", "redirect_uris": [GEMINI]})
    assert r.status_code == 201
    corps = r.json()
    assert corps["redirect_uris"] == [GEMINI]
    assert corps["client_secret"]
    assert logto.creees[0]["type"] == "Traditional"
    assert logto.creees[0]["name"] == f"{dcr.PREFIXE_DCR}Gemini"


def test_grok_s_inscrit_seul(client, logto):
    # Grok s'inscrit lui-même, client public PKCE ; adresse exacte → jamais plafonnée.
    r = client.post("/oauth/register",
                    json={"client_name": "Grok", "redirect_uris": [GROK],
                          "token_endpoint_auth_method": "none"})
    assert r.status_code == 201
    assert r.json()["redirect_uris"] == [GROK]
    assert logto.creees[0]["type"] == "Native"
    assert logto.creees[0]["name"] == f"{dcr.PREFIXE_DCR}Grok"


# ── Les métadonnées du client ────────────────────────────────────────────────

def test_methode_par_defaut_client_secret_basic(client, logto):
    r = client.post("/oauth/register", json={"redirect_uris": [CLAUDE]})
    assert r.status_code == 201
    assert r.json()["token_endpoint_auth_method"] == "client_secret_basic"
    assert "client_secret" in r.json()
    assert logto.creees[0]["type"] == "Traditional"


def test_none_donne_une_application_native_sans_secret(client, logto):
    r = client.post("/oauth/register",
                    json={"redirect_uris": [CLAUDE], "token_endpoint_auth_method": "none"})
    assert r.status_code == 201
    corps = r.json()
    assert "client_secret" not in corps and "client_secret_expires_at" not in corps
    assert corps["token_endpoint_auth_method"] == "none"
    assert logto.creees[0]["type"] == "Native"


@pytest.mark.parametrize("champ,valeur", [
    ("token_endpoint_auth_method", "private_key_jwt"),
    ("grant_types", ["client_credentials"]),
    ("grant_types", ["authorization_code", "implicit"]),
    ("response_types", ["token"]),
    ("response_types", "code"),
])
def test_metadonnees_hors_contrat(client, logto, champ, valeur):
    r = client.post("/oauth/register", json={"redirect_uris": [MISTRAL], champ: valeur})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_client_metadata"
    assert logto.creees == []


def test_corps_illisible(client, logto):
    r = client.post("/oauth/register", content=b"{pas du json",
                    headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_client_metadata"


def test_nom_tronque_a_64():
    demande = dcr.lire_demande({"redirect_uris": [MISTRAL], "client_name": "x" * 200})
    assert len(demande.nom) == 64


# ── La reprise ───────────────────────────────────────────────────────────────

def test_reprise_meme_type_memes_adresses_meme_client_meme_si_le_nom_differe(client, logto):
    premier = client.post("/oauth/register",
                          json={"client_name": "A", "redirect_uris": [CLAUDE, MISTRAL]})
    second = client.post("/oauth/register",
                         json={"client_name": "B", "redirect_uris": [MISTRAL, CLAUDE, CLAUDE]})
    assert premier.status_code == second.status_code == 201
    assert premier.json()["client_id"] == second.json()["client_id"]
    assert second.json()["client_secret"] == premier.json()["client_secret"]
    assert len(logto.creees) == 1
    assert logto.creees[0]["oidcClientMetadata"]["redirectUris"] == sorted([CLAUDE, MISTRAL])


def test_reprise_par_type_une_native_ne_reprend_pas_une_traditional(client, logto):
    a = client.post("/oauth/register", json={"redirect_uris": [CLAUDE]})
    b = client.post("/oauth/register",
                    json={"redirect_uris": [CLAUDE], "token_endpoint_auth_method": "none"})
    assert a.json()["client_id"] != b.json()["client_id"]
    assert [c["type"] for c in logto.creees] == ["Traditional", "Native"]


def test_seules_les_applications_dcr_sont_reprises(client, logto):
    logto.applications.append({"id": "amorcee", "name": "Console", "type": "Traditional",
                               "oidcClientMetadata": {"redirectUris": [MISTRAL]}})
    r = client.post("/oauth/register", json={"redirect_uris": [MISTRAL]})
    assert r.json()["client_id"] != "amorcee"


def test_applications_lues_sur_toutes_les_pages(client, logto):
    logto.applications.extend({"id": f"autre{i}", "name": f"autre {i}", "type": "Native",
                               "oidcClientMetadata": {"redirectUris": []}} for i in range(150))
    logto.applications.append(_app_dcr(0, [MISTRAL]))
    r = client.post("/oauth/register", json={"redirect_uris": [MISTRAL]})
    assert r.json()["client_id"] == "ancienne0"
    assert logto.creees == []


# ── Le plafond ───────────────────────────────────────────────────────────────

def test_plafond_refuse_une_nouvelle_adresse_prefixe(client, logto):
    logto.applications.extend(_app_dcr(i, [f"https://chatgpt.com/connector/oauth/c{i}"])
                              for i in range(dcr.PLAFOND_VARIABLES))
    r = client.post("/oauth/register", json={"redirect_uris": [CHATGPT]})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_client_metadata"
    assert "plafond" in r.json()["error_description"]
    assert logto.creees == []


def test_plafond_n_empeche_ni_les_adresses_exactes_ni_la_reprise(client, logto):
    logto.applications.extend(_app_dcr(i, [f"https://chatgpt.com/connector/oauth/c{i}"])
                              for i in range(dcr.PLAFOND_VARIABLES))
    exacte = client.post("/oauth/register", json=LE_CHAT)
    assert exacte.status_code == 201
    reprise = client.post("/oauth/register",
                          json={"redirect_uris": ["https://chatgpt.com/connector/oauth/c3"]})
    assert reprise.status_code == 201
    assert reprise.json()["client_id"] == "ancienne3"


def test_sous_le_plafond_une_adresse_prefixe_passe(client, logto):
    logto.applications.extend(_app_dcr(i, [f"https://chatgpt.com/connector/oauth/c{i}"])
                              for i in range(dcr.PLAFOND_VARIABLES - 1))
    r = client.post("/oauth/register", json={"redirect_uris": [CHATGPT]})
    assert r.status_code == 201


@pytest.mark.parametrize("forme", [
    "https://chatgpt.com/connector/oauth/c{i}",           # la forme bornée d'aujourd'hui
    "https://chatgpt.com/connector/oauth/c{i}/ancienne",  # admise avant la borne du segment
    "https://chatgpt.com/connector/oauth/c{i}?v=1",
])
def test_anciennes_applications_chatgpt_comptees_dans_le_plafond(client, logto, forme):
    logto.applications.extend(_app_dcr(i, [forme.format(i=i)])
                              for i in range(dcr.PLAFOND_VARIABLES))
    r = client.post("/oauth/register", json={"redirect_uris": [CHATGPT]})
    assert r.status_code == 400
    assert "plafond" in r.json()["error_description"]
    assert logto.creees == []
    assert client.post("/oauth/register", json={"redirect_uris": [GEMINI]}).status_code == 201


@pytest.fixture
def signaux(monkeypatch):
    import sentry_sdk
    recus: list[tuple[str, str]] = []
    monkeypatch.setattr(sentry_sdk, "capture_message",
                        lambda msg, level=None: recus.append((level, msg)))
    return recus


def test_plafond_approche_alerte(client, logto, signaux):
    seuil = int(dcr.PLAFOND_VARIABLES * dcr.PART_ALERTE)
    logto.applications.extend(_app_dcr(i, [f"https://chatgpt.com/connector/oauth/c{i}"])
                              for i in range(seuil - 1))
    assert client.post("/oauth/register", json={"redirect_uris": [CHATGPT]}).status_code == 201
    assert len(signaux) == 1
    niveau, message = signaux[0]
    assert niveau == "warning"
    assert "https://chatgpt.com/connector/oauth/" in message


def test_loin_du_plafond_aucune_alerte(client, logto, signaux):
    logto.applications.extend(_app_dcr(i, [f"https://chatgpt.com/connector/oauth/c{i}"])
                              for i in range(int(dcr.PLAFOND_VARIABLES * dcr.PART_ALERTE) - 2))
    assert client.post("/oauth/register", json={"redirect_uris": [CHATGPT]}).status_code == 201
    assert client.post("/oauth/register", json=LE_CHAT).status_code == 201
    assert signaux == []


def test_plafond_chatgpt_n_empeche_pas_gemini(client, logto):
    logto.applications.extend(_app_dcr(i, [f"https://chatgpt.com/connector/oauth/c{i}"])
                              for i in range(dcr.PLAFOND_VARIABLES))
    r = client.post("/oauth/register", json={"redirect_uris": [GEMINI]})
    assert r.status_code == 201


def test_plafond_gemini_a_part(client, logto):
    logto.applications.extend(_app_dcr(i, [f"{GEMINI_PREFIXE}{i}-mcp_exemple_test"])
                              for i in range(dcr.PLAFOND_GEMINI))
    r = client.post("/oauth/register", json={"redirect_uris": [GEMINI]})
    assert r.status_code == 400
    assert "plafond" in r.json()["error_description"]
    assert client.post("/oauth/register", json={"redirect_uris": [CHATGPT]}).status_code == 201
    reprise = client.post("/oauth/register",
                          json={"redirect_uris": [f"{GEMINI_PREFIXE}7-mcp_exemple_test"]})
    assert reprise.status_code == 201
    assert reprise.json()["client_id"] == "ancienne7"


# ── Aucune panne masquée ─────────────────────────────────────────────────────

def test_secrets_vides_erreur(client, logto):
    logto.secrets = False
    r = client.post("/oauth/register", json=LE_CHAT)
    assert r.status_code == 500


def test_api_de_gestion_en_panne_erreur(client, logto):
    logto.panne_gestion = True
    r = client.post("/oauth/register", json=LE_CHAT)
    assert r.status_code == 500


# ── La description ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("chemin", ["/.well-known/oauth-authorization-server/oidc",
                                    "/.well-known/openid-configuration/oidc"])
def test_description_de_logto_telle_quelle_plus_registration_endpoint(client, logto, chemin):
    r = client.get(chemin)
    assert r.status_code == 200
    assert r.json() == {**DESCRIPTION, "registration_endpoint": f"{LOGTO}/oauth/register"}


def test_registration_endpoint_sur_l_origine_de_l_emetteur(monkeypatch):
    monkeypatch.setenv("LOGTO_ENDPOINT", "https://auth.example.test/")
    assert dcr.registration_endpoint() == "https://auth.example.test/oauth/register"


def test_logto_injoignable_erreur_sur_la_description(client, logto):
    logto.description = requests.ConnectionError("injoignable")
    r = client.get("/.well-known/oauth-authorization-server/oidc")
    assert r.status_code == 500


def test_logto_non_200_erreur_sur_la_description(client, logto):
    logto.description = (502, {"issuer": "repli"})
    r = client.get("/.well-known/openid-configuration/oidc")
    assert r.status_code == 500
