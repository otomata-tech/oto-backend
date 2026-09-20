"""Façade DCR — l'allowlist des rappels, et ce que rend un enregistrement raté.

Mesuré le 2026-09-08 sur la production : `POST /oauth/register` avec le rappel
stable de ChatGPT était refusé (400), rien de `chatgpt.com` n'était enregistré
dans l'app Logto partagée, et l'`/authorize` rejouée avec ce rappel rendait
`oidc.invalid_redirect_uri` — l'étape qui échoue. Les deux formes de rappel sont
documentées par OpenAI (developers.openai.com/plugins/build/auth) ; c'est
l'éligibilité RFC 9207 du serveur d'autorisation qui tranche, pas le mode de
connexion, et un connecteur créé avant l'apparition de la forme par-connecteur
garde la forme stable.
"""
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oto_mcp.auth import facade
from oto_mcp.auth.facade import RedirectRegistrationFailed, _redirect_ok, make_routes


@pytest.mark.parametrize("uri", [
    "https://chatgpt.com/connector_platform_oauth_redirect",   # forme STABLE
    "https://chatgpt.com/connector/oauth/abc123",              # forme par connecteur
    "https://claude.ai/api/mcp/auth_callback",
    "https://callback.mistral.ai/v1/integrations_auth/oauth2_callback",
    # Hermes (Nous Research) : Hermes Cloud, un sous-domaine par instance.
    "https://volcanic-existentialism-0230.agents.nousresearch.com/api/mcp/oauth/callback/tulina",
    "https://agents.nousresearch.com/api/mcp/oauth/callback/tulina",
])
def test_rappels_connus_acceptes(uri):
    assert _redirect_ok(uri) is True


@pytest.mark.parametrize("uri", [
    # le host se compare en ENTIER : ni un voisin, ni un suffixe
    "https://chatgpt.com.attaquant.test/connector_platform_oauth_redirect",
    "https://attaquant.test/connector_platform_oauth_redirect",
    # le chemin stable est EXACT : pas un préfixe qu'on prolonge
    "https://chatgpt.com/connector_platform_oauth_redirect/suite",
    "https://chatgpt.com/connector_platform_oauth_redirectX",
    # et le schéma reste https
    "http://chatgpt.com/connector_platform_oauth_redirect",
    # Hermes : un host VOISIN n'est pas un sous-domaine (ni un suffixe brut),
    # et le chemin sans le segment (nom du serveur MCP) reste refusé.
    "https://evil-agents.nousresearch.com/api/mcp/oauth/callback/tulina",
    "https://agents.nousresearch.com.attaquant.test/api/mcp/oauth/callback/tulina",
    "https://volcanic-existentialism-0230.agents.nousresearch.com/api/mcp/oauth/callback",
])
def test_rappels_voisins_refuses(uri):
    assert _redirect_ok(uri) is False


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("LOGTO_ENDPOINT", "https://auth.oto.ninja")
    monkeypatch.setattr(facade, "tenant_for_host", lambda host: None)
    app = Starlette(routes=make_routes("https://mcp.oto.ninja", "app-partagee"))
    return TestClient(app)


_CORPS = {"redirect_uris": ["https://chatgpt.com/connector_platform_oauth_redirect"]}


def test_ecriture_ratee_ne_se_declare_pas_creee(client, monkeypatch):
    """L'annuaire a répondu, le PATCH a échoué : le callback n'y est pas, donc
    l'`/authorize` sera refusé. Annoncer 201 déplacerait le diagnostic chez un
    client qui ne lit pas nos journaux."""
    def _echoue(app_id, redirects, directory=None):
        raise RedirectRegistrationFailed("PATCH refusé")
    monkeypatch.setattr(facade, "_register_redirects", _echoue)

    r = client.post("/oauth/register", json=_CORPS)

    assert r.status_code == 503
    assert r.json()["error"] == "temporarily_unavailable"


def test_annuaire_injoignable_laisse_le_client_s_installer(client, monkeypatch):
    """État INCONNU : un client déjà enregistré (Claude) doit continuer de
    s'installer pendant un incident de l'annuaire. C'est le régime voulu, et la
    seule branche où un 201 sans écriture reste juste."""
    def _injoignable(app_id, redirects, directory=None):
        raise ConnectionError("annuaire injoignable")
    monkeypatch.setattr(facade, "_register_redirects", _injoignable)

    r = client.post("/oauth/register", json=_CORPS)

    assert r.status_code == 201
    assert r.json()["client_id"] == "app-partagee"


def test_enregistrement_reussi_rend_le_client(client, monkeypatch):
    vus = {}
    monkeypatch.setattr(facade, "_register_redirects",
                        lambda app_id, redirects, directory=None:
                        vus.update(app=app_id, r=list(redirects), d=directory))

    r = client.post("/oauth/register", json=_CORPS)

    assert r.status_code == 201
    assert vus["r"] == _CORPS["redirect_uris"]
    # …et sur NOTRE annuaire : le host de la plateforme ne doit jamais partir
    # écrire chez un tenant, pas plus que l'inverse.
    assert vus["d"].label == "oto"


def test_rappel_inconnu_reste_refuse(client):
    r = client.post("/oauth/register",
                    json={"redirect_uris": ["https://attaquant.test/callback"]})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_redirect_uri"
