"""Le point d'autorisation de la façade pose le consentement que Logto exige (oto#202).

**Le défaut tenu fermé.** Le connecteur Codex Apps demandait `offline_access` sans
`prompt=consent` ; Logto retirait `offline_access`, ne délivrait aucun jeton de
rafraîchissement, et chaque expiration du jeton d'accès (3 600 s) imposait une
autorisation complète. La métadonnée publiait le point d'autorisation de Logto en direct,
donc Oto ne pouvait rien y faire.

Ce banc tient les deux moitiés du correctif :
- la TRANSFORMATION de la requête — seul `prompt` change, et seulement quand
  `offline_access` est demandé sans consentement ; tout le reste arrive à l'octet près ;
- la ROUTE servie — la destination est notre annuaire, résolu côté serveur, jamais une
  valeur de la requête ; la métadonnée garde son `issuer`, et l'annuaire d'un tenant garde
  son propre point d'autorisation.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oto_mcp import tenancy
from oto_mcp.auth.authorize_consent import avec_consentement, redirection
from oto_mcp.auth.facade import make_routes

_BASE = ("response_type=code&client_id=cli-x"
         "&redirect_uri=https%3A%2F%2Fclient.example%2Fcallback"
         "&state=s%2Fa%2Bb%3D%3D~x&code_challenge=Zx-9_aQ&code_challenge_method=S256"
         "&resource=https%3A%2F%2Fmcp.oto.cx%2Fmcp")
_OFFLINE = _BASE + "&scope=openid+email+offline_access+profile"


def _segments_hors_prompt(requete: str) -> list[str]:
    return [s for s in requete.split("&") if not s.startswith("prompt=")]


# ── la transformation : seul `prompt` change, et seulement quand il le faut ────

def test_offline_access_sans_prompt_recoit_consent_et_rien_dautre():
    assert avec_consentement(_OFFLINE) == _OFFLINE + "&prompt=consent"


@pytest.mark.parametrize("prompt, segment", [
    ("login", "prompt=login%20consent"),
    ("select_account", "prompt=select_account%20consent"),
])
def test_un_prompt_existant_garde_ses_valeurs_et_gagne_consent_en_place(prompt, segment):
    requete = f"{_OFFLINE}&prompt={prompt}&ui_locales=fr"
    sortie = avec_consentement(requete)
    assert sortie.split("&")[-2:] == [segment, "ui_locales=fr"], "prompt déplacé ou mal écrit"
    assert _segments_hors_prompt(sortie) == _segments_hors_prompt(requete), (
        "un autre paramètre a été réécrit : state, PKCE, redirect_uri ou resource "
        "doivent arriver chez Logto à l'octet près")


@pytest.mark.parametrize("prompt", ["consent", "login%20consent", "consent+login"])
def test_un_consentement_deja_demande_laisse_la_requete_intacte(prompt):
    """`login%20consent` est la forme qu'envoie déjà claude.ai : rien ne change pour lui."""
    requete = f"{_OFFLINE}&prompt={prompt}"
    assert avec_consentement(requete) == requete


def test_prompt_none_nest_jamais_combine_a_consent():
    """`none` demande une autorisation SANS interaction : `none consent` est refusé par
    Logto, et supposer acquis un consentement silencieux serait faux. La requête passe
    telle quelle — Logto retire `offline_access`, comme avant."""
    requete = f"{_OFFLINE}&prompt=none"
    assert avec_consentement(requete) == requete


@pytest.mark.parametrize("scope", ["&scope=openid+profile+email", "&scope=offline_accessX", ""],
                         ids=["sans_offline_access", "jeton_voisin", "aucun_scope_mistral"])
def test_sans_offline_access_rien_ne_change_aucun_droit_ajoute(scope):
    requete = _BASE + scope
    assert avec_consentement(requete) == requete


def test_un_scope_encode_en_pourcent_est_reconnu():
    requete = _BASE + "&scope=openid%20offline_access"
    assert avec_consentement(requete) == requete + "&prompt=consent"


@pytest.mark.parametrize("suffixe", [
    "&scope=openid&scope=offline_access",
    "&scope=offline_access&prompt=login&prompt=none",
    "&scope=offline_access&request_uri=urn%3Aietf%3Aparams%3Aoauth%3Arequest_uri%3Aabc",
    "&scope=offline_access&request=eyJhbGciOiJub25lIn0.e30.",
], ids=["scope_repete", "prompt_repete", "request_uri", "request"])
def test_parametres_repetes_et_objets_de_requete_passent_tels_quels(suffixe):
    """Logto refuse la répétition et traite l'objet de requête lui-même : il tranche et
    nomme son refus — on ne réécrit rien."""
    requete = _BASE + suffixe
    assert avec_consentement(requete) == requete


def test_une_valeur_encodee_ne_peut_pas_injecter_un_prompt():
    requete = _OFFLINE.replace("state=s%2Fa%2Bb%3D%3D~x", "state=x%26prompt%3Dnone")
    lu = parse_qs(avec_consentement(requete))
    assert lu["prompt"] == ["consent"]
    assert lu["state"] == ["x&prompt=none"], "le state du client a été altéré"


def test_un_caractere_de_controle_est_refuse_en_le_nommant():
    r = redirection("https://auth.oto.cx/oidc", _OFFLINE + "&x=a\r\nLocation: https://evil.test")
    assert r.status_code == 400
    assert b"invalid_request" in r.body
    assert "location" not in r.headers


# ── la route servie : destination résolue côté serveur ────────────────────────

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("LOGTO_ENDPOINT", "https://auth.oto.ninja")
    monkeypatch.setenv("LOGTO_PUBLIC_ENDPOINT", "https://auth.oto.cx")
    avant = tenancy.current()
    tenancy.install(tenancy.IssuerRegistry(tenancy.build(
        "https://auth.oto.ninja/oidc",
        tenants=[{"slug": "acme", "name": "Acme", "issuer": "https://auth.acme.test/oidc",
                  "hosts": ["mcp.acme.test"], "oauth_client_id": "cli-acme"}])))
    yield TestClient(Starlette(routes=make_routes("https://mcp.oto.cx", "app-x")))
    tenancy.install(avant)


def _autoriser(client, requete: str, host: str = "mcp.oto.cx"):
    return client.get(f"/oauth/authorize?{requete}", headers={"host": host},
                      follow_redirects=False)


def test_notre_annuaire_recoit_la_requete_a_loctet_pres_plus_consent(client):
    requete = _OFFLINE + "&resource=https%3A%2F%2Fautre.test%2Fmcp"   # `resource` répétable
    r = _autoriser(client, requete)
    assert r.status_code == 302, "302 attendu (pas 307, qui rejouerait la méthode)"
    assert r.headers["location"] == f"https://auth.oto.cx/oidc/auth?{requete}&prompt=consent"
    assert r.headers["cache-control"] == "no-store"


def test_sans_domaine_public_la_cible_est_lemetteur(client, monkeypatch):
    monkeypatch.delenv("LOGTO_PUBLIC_ENDPOINT")
    cible = urlparse(_autoriser(client, _OFFLINE).headers["location"])
    assert (cible.netloc, cible.path) == ("auth.oto.ninja", "/oidc/auth")


def test_une_requete_vide_est_transmise_telle_quelle(client):
    r = client.get("/oauth/authorize", headers={"host": "mcp.oto.cx"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (302, "https://auth.oto.cx/oidc/auth")


def test_seul_get_est_servi(client):
    r = client.post(f"/oauth/authorize?{_OFFLINE}", headers={"host": "mcp.oto.cx"})
    assert r.status_code == 405


@pytest.mark.parametrize("requete, host", [
    (_OFFLINE + "&redirect=https%3A%2F%2Fevil.test", "mcp.oto.cx"),
    ("next=https://evil.test&" + _OFFLINE, "mcp.oto.cx"),
    ("@evil.test/&" + _OFFLINE, "mcp.oto.cx"),
    (_OFFLINE, "evil.test"),
], ids=["parametre_redirect", "parametre_next", "arobase", "host_inconnu"])
def test_aucune_destination_forgee_nest_suivie(client, requete, host):
    """Aucune redirection ouverte : ni un paramètre, ni un en-tête `Host` inconnu ne
    déplacent la cible hors de notre annuaire."""
    r = _autoriser(client, requete, host=host)
    assert r.status_code == 302
    cible = urlparse(r.headers["location"])
    assert (cible.scheme, cible.netloc, cible.path) == ("https", "auth.oto.cx", "/oidc/auth")


def test_un_client_sans_offline_access_suit_le_meme_parcours(client):
    requete = _BASE + "&scope=openid+profile+email"
    assert _autoriser(client, requete).headers["location"] == \
        f"https://auth.oto.cx/oidc/auth?{requete}"


def test_un_hote_de_tenant_ne_passe_pas_par_la_facade(client):
    """Délivrer des jetons de rafraîchissement aux utilisateurs d'un partenaire est SA
    décision : sa métadonnée garde son point d'autorisation, et la route n'existe pas
    sur son hôte."""
    r = _autoriser(client, _OFFLINE, host="mcp.acme.test")
    assert r.status_code == 404
    assert "location" not in r.headers


# ── la métadonnée : issuer inchangé ; autorisation sur la façade, sauf tenant ──

@pytest.mark.parametrize("chemin", ["/.well-known/oauth-authorization-server",
                                    "/.well-known/openid-configuration"])
@pytest.mark.parametrize("host, autorisation, jeton", [
    ("mcp.oto.cx", "https://mcp.oto.cx/oauth/authorize", "https://auth.oto.cx/oidc/token"),
    ("mcp.acme.test", "https://auth.acme.test/oidc/auth", "https://auth.acme.test/oidc/token"),
], ids=["notre_annuaire", "annuaire_tenant"])
def test_la_metadonnee_servie_garde_son_issuer(client, chemin, host, autorisation, jeton):
    """Les clients stricts comparent l'`issuer` au PRM octet pour octet : il ne bouge
    pas, ni l'enregistrement. Seule l'autorisation de NOTRE annuaire passe par la
    façade ; celle du tenant reste chez lui."""
    md = client.get(chemin, headers={"host": host}).json()
    assert md["issuer"] == str(AnyHttpUrl(f"https://{host}"))
    assert md["registration_endpoint"] == f"https://{host}/oauth/register"
    assert md["authorization_endpoint"] == autorisation
    assert md["token_endpoint"] == jeton
