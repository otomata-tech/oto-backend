"""La découverte suit le HOST : émetteur, nom, et audience du tenant qui le réclame.

Ce que ce lot ferme (oto-private#83) : le domaine d'un partenaire annonçait NOTRE
serveur d'autorisation, donc son utilisateur natif atterrissait sur NOTRE écran de
connexion — où il n'a pas de compte. Le mur n'était pas dans la vérification du jeton
(elle acceptait déjà le sien), il était dans la découverte.

Et l'audience va avec : elle ne vivait que dans `MCP_AUDIENCE_ALT`, la variable que le
basculement doit retirer. La dériver des hosts déclarés est ce qui rend ce retrait
possible — sans elle, le flip invaliderait tous les jetons du partenaire d'un coup.

Invariant tenu de bout en bout : **un host qu'aucun tenant ne réclame ne change pas
d'un octet.** C'est ce qui permet à ce lot d'atterrir inerte.
"""
from __future__ import annotations

import pytest

from oto_mcp import tenancy


@pytest.fixture
def registre_avec_partenaire():
    """Un tenant tiers déclarant son host — l'état d'APRÈS la déclaration."""
    avant = tenancy.current()
    entries = tenancy.build(
        "https://auth.oto.ninja/oidc",
        tenants=[{"slug": "acme", "name": "Acme",
                  "issuer": "https://auth.acme.test/oidc",
                  "jwks_uri": "https://auth.acme.test/oidc/jwks",
                  "hosts": ["mcp.acme.test"]}])
    tenancy.install(tenancy.IssuerRegistry(entries))
    yield tenancy.current()
    tenancy.install(avant)


# --- le binding host → tenant --------------------------------------------------

def test_un_host_declare_designe_son_tenant(registre_avec_partenaire):
    entry = registre_avec_partenaire.for_host("mcp.acme.test")
    assert entry is not None and entry.slug == "acme"
    assert entry.issuer == "https://auth.acme.test/oidc"


def test_un_host_inconnu_ne_designe_rien(registre_avec_partenaire):
    """`None` est le cas NOMINAL, pas une erreur : c'est lui qui garde intact le
    comportement de tous les domaines qui n'appartiennent à personne."""
    assert registre_avec_partenaire.for_host("mcp.oto.cx") is None
    assert registre_avec_partenaire.for_host("") is None
    assert registre_avec_partenaire.for_host(None) is None


def test_le_host_se_compare_sans_casse_ni_port(registre_avec_partenaire):
    """Un `Host:` arrive tel que le client l'a écrit. Comparer les deux formes
    brutes ferait retomber le tenant sur le défaut EN SILENCE — le symptôme même
    qu'on corrige."""
    assert registre_avec_partenaire.for_host("MCP.Acme.Test") is not None
    assert registre_avec_partenaire.for_host("mcp.acme.test:443") is not None


def test_un_host_reclame_deux_fois_reste_au_premier(caplog):
    """Le host décide vers quel annuaire on envoie l'utilisateur : une réclamation
    ambiguë l'enverrait chez le mauvais partenaire. Le second est refusé ET loggé."""
    entries = tenancy.build(
        "https://auth.oto.ninja/oidc",
        tenants=[{"slug": "acme", "issuer": "https://auth.acme.test/oidc",
                  "hosts": ["mcp.partage.test"]},
                 {"slug": "beta", "issuer": "https://auth.beta.test/oidc",
                  "hosts": ["mcp.partage.test"]}])
    reg = tenancy.IssuerRegistry(entries)
    assert reg.for_host("mcp.partage.test").slug == "acme"


# --- l'audience, dérivée des hosts et non de l'environnement -------------------

def test_laudience_dun_host_declare_est_acceptee(registre_avec_partenaire):
    """Acceptée pour un jeton DU tenant qui déclare le host."""
    from oto_mcp.server import _audience_of_declared_tenant
    assert _audience_of_declared_tenant("https://mcp.acme.test/mcp", "acme")


def test_laudience_dun_partenaire_est_refusee_a_un_jeton_dun_AUTRE_tenant(
        registre_avec_partenaire):
    """LE cran strict du binding : « servi sur son domaine » doit vouloir dire plus
    que « servi ». Un jeton du tenant primaire, ou d'un autre partenaire, ne peut pas
    revendiquer l'audience de celui-ci.

    ⚠️ Ce cran s'active SEUL au retrait de `MCP_AUDIENCE_ALT` : tant que cette
    variable porte l'audience, elle est servie à tout le monde plus haut dans
    `_audience_ok`. Pas d'interrupteur à ne pas oublier."""
    from oto_mcp.server import _audience_of_declared_tenant
    for slug in ("oto", "beta", ""):
        assert not _audience_of_declared_tenant("https://mcp.acme.test/mcp", slug), slug


def test_laudience_dun_host_inconnu_est_refusee(registre_avec_partenaire):
    from oto_mcp.server import _audience_of_declared_tenant
    assert not _audience_of_declared_tenant("https://mcp.inconnu.test/mcp", "acme")


def test_un_host_declare_nouvre_pas_ses_autres_chemins(registre_avec_partenaire):
    """Déclarer un domaine ne consent qu'à SON endpoint. Accepter n'importe quel
    chemin ferait d'une ligne de configuration un blanc-seing sur le domaine."""
    from oto_mcp.server import _audience_of_declared_tenant
    for aud in ("https://mcp.acme.test/api",
                "https://mcp.acme.test/mcp/autre",
                "https://mcp.acme.test",
                "http://mcp.acme.test/mcp",          # pas https
                "", None, 42, ["https://mcp.acme.test/mcp"]):
        assert not _audience_of_declared_tenant(aud, "acme"), aud


def test_le_slash_final_ne_change_rien(registre_avec_partenaire):
    from oto_mcp.server import _audience_of_declared_tenant
    assert _audience_of_declared_tenant("https://mcp.acme.test/mcp/", "acme")


def test_sans_tenant_declare_laudience_ne_passe_pas():
    """L'état d'AVANT : registre sans host → la dérivation ne peut rien accepter,
    donc elle n'élargit rien tant que personne n'a écrit de ligne."""
    avant = tenancy.current()
    tenancy.install(tenancy.IssuerRegistry(
        tenancy.build("https://auth.oto.ninja/oidc")))
    try:
        from oto_mcp.server import _audience_of_declared_tenant
        assert not _audience_of_declared_tenant("https://mcp.acme.test/mcp", "acme")
    finally:
        tenancy.install(avant)


# --- ce que la découverte annonce ---------------------------------------------

def test_la_decouverte_dun_host_declare_pointe_la_facade_sur_ce_host(
        registre_avec_partenaire):
    """Le serveur annoncé est le host LUI-MÊME (notre façade), pas l'émetteur.

    C'est contre-intuitif — on annoncerait volontiers l'annuaire du tenant — et c'est
    ce qui a cassé la production le 13/08 : sans la façade sur le chemin, le client
    demande un enregistrement automatique que Logto self-hosted ne fait pas. L'annuaire
    du tenant est atteint UN CRAN plus loin, dans la métadonnée servie par la façade.
    """
    from oto_mcp.auth.facade import tenant_discovery_for_host
    as_url, nom = tenant_discovery_for_host("mcp.acme.test")
    assert as_url == "https://mcp.acme.test/"
    assert nom == "Acme", "le nom du tenant doit être servi, pas son identifiant"


def test_la_decouverte_dun_host_libre_ne_change_rien(registre_avec_partenaire):
    """LE garde-fou d'inertie : tant qu'aucun tenant ne réclame un host, la
    découverte doit rendre exactement ce qu'elle rendait avant ce lot."""
    from oto_mcp.auth.facade import tenant_discovery_for_host
    assert tenant_discovery_for_host("mcp.oto.cx") is None


def test_le_nom_retombe_sur_lidentifiant_si_absent():
    """Une ligne sans nom ne doit pas servir une chaîne vide au client — le champ
    est lu par un humain dans son écran de consentement."""
    entries = tenancy.build(
        "https://auth.oto.ninja/oidc",
        tenants=[{"slug": "acme", "issuer": "https://auth.acme.test/oidc",
                  "hosts": ["mcp.acme.test"]}])
    avant = tenancy.current()
    tenancy.install(tenancy.IssuerRegistry(entries))
    try:
        from oto_mcp.auth.facade import tenant_discovery_for_host
        assert tenant_discovery_for_host("mcp.acme.test")[1] == "acme"
    finally:
        tenancy.install(avant)


# --- la route servie, pas seulement le helper ---------------------------------
#
# ⚠️ Les tests ci-dessus interrogent les fonctions. Ce que le client LIT est la route,
# et c'est là qu'a vécu le défaut : le helper existait déjà, la route ne l'appelait pas.
# On sert donc réellement le document, pour les deux cas.

def _prm(host: str) -> dict:
    """Le document de découverte SERVI pour ce Host, tel qu'un client le reçoit."""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from oto_mcp.auth import facade as oauth_facade
    app = Starlette(routes=oauth_facade.make_routes("https://mcp.oto.cx", "app-x"))
    with TestClient(app) as client:
        r = client.get("/.well-known/oauth-protected-resource", headers={"host": host})
        assert r.status_code == 200, r.text
        return r.json()


def test_le_document_servi_a_un_host_declare_pointe_le_partenaire(registre_avec_partenaire):
    doc = _prm("mcp.acme.test")
    assert doc["authorization_servers"] == ["https://mcp.acme.test/"], (
        "le serveur annoncé doit être la façade SUR CE HOST : elle porte "
        "l'enregistrement, et route ensuite vers l'annuaire du tenant")
    assert doc["resource_name"] == "Acme"
    # L'audience DOIT suivre le host, sans quoi le client demande un jeton pour une
    # ressource qui n'est pas la sienne. C'est ce cran qui rend possible le retrait
    # de `MCP_AUDIENCE_ALT` pendant le basculement.
    assert doc["resource"].rstrip("/") == "https://mcp.acme.test/mcp"


def test_le_document_servi_a_un_host_libre_est_inchange(registre_avec_partenaire):
    """L'INERTIE, vérifiée sur le document réel : un host que personne ne réclame
    reçoit exactement ce qu'il recevait avant ce lot."""
    doc = _prm("mcp.oto.cx")
    assert doc["authorization_servers"] == ["https://mcp.oto.cx/"]
    assert doc["resource_name"] == "oto MCP"


# --- la façade sert le bon annuaire, et son client ------------------------------
#
# ⚠️ Ces trois tests existent parce que la première version du lot a CASSÉ la connexion
# en production (13/08) : le PRM annonçait l'émetteur du tenant EN DIRECT, ce qui
# retirait la façade d'enregistrement du chemin. Claude a demandé un enregistrement
# automatique à un Logto self-hosted, qui n'en fait pas — « l'enregistrement
# automatique du client n'est pas pris en charge ». La façade n'est pas un détail
# d'implémentation : elle est la raison pour laquelle un client sans DCR peut se
# connecter du tout.

@pytest.fixture
def registre_avec_client(registre_avec_partenaire):
    avant = tenancy.current()
    entries = tenancy.build(
        "https://auth.oto.ninja/oidc",
        tenants=[{"slug": "acme", "name": "Acme",
                  "issuer": "https://auth.acme.test/oidc",
                  "hosts": ["mcp.acme.test"], "oauth_client_id": "cli-acme"}])
    tenancy.install(tenancy.IssuerRegistry(entries))
    yield
    tenancy.install(avant)


def test_le_prm_annonce_la_facade_pas_lemetteur(registre_avec_client):
    """LE correctif du 13/08. L'émetteur du tenant n'a pas d'enregistrement
    automatique ; la façade, si — elle doit rester sur le chemin."""
    doc = _prm("mcp.acme.test")
    assert doc["authorization_servers"] == ["https://mcp.acme.test/"], (
        "le PRM annonce l'émetteur en direct : le client demandera un enregistrement "
        "automatique à un annuaire qui n'en fait pas")


def test_la_metadonnee_du_host_route_vers_lannuaire_du_tenant(registre_avec_client):
    """La façade s'annonce elle-même comme serveur (l'issuer, c'est le host), mais
    envoie autoriser et échanger le jeton chez le TENANT. Les deux moitiés sont
    nécessaires : l'une pour l'enregistrement, l'autre pour l'identité."""
    from oto_mcp.auth.facade import as_metadata
    md = as_metadata("https://mcp.acme.test", "https://auth.acme.test/oidc")
    assert md["issuer"] == "https://mcp.acme.test/"
    assert md["authorization_endpoint"] == "https://auth.acme.test/oidc/auth"
    assert md["token_endpoint"] == "https://auth.acme.test/oidc/token"
    assert md["jwks_uri"] == "https://auth.acme.test/oidc/jwks"
    assert md["registration_endpoint"] == "https://mcp.acme.test/oauth/register", (
        "l'enregistrement doit rester chez NOUS — c'est tout l'objet de la façade")


def test_la_metadonnee_dun_host_libre_est_inchangee(registre_avec_client, monkeypatch):
    monkeypatch.setenv("LOGTO_ENDPOINT", "https://auth.oto.ninja")
    from oto_mcp.auth.facade import as_metadata
    md = as_metadata("https://mcp.oto.cx")
    assert md["issuer"] == "https://mcp.oto.cx/"
    # oto#202 : NOTRE annuaire autorise par la façade ; celui d'un tenant (plus haut), non.
    assert md["authorization_endpoint"] == "https://mcp.oto.cx/oauth/authorize"
