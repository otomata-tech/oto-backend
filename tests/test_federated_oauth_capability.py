"""Les verbes du consentement OAuth per-user, en capacités : mêmes chemins, mêmes codes.

Dix routes `/api/{atlassian,folkmcp,google}/oauth*` ont quitté `api/atlassian.py`,
`api/folk.py` et `api/datastore.py` pour `capabilities/federated_oauth.py`
(27/08). Les **callbacks** restent écrits à la main : le fournisseur y redirige le
navigateur (302, sans auth), l'adaptateur REST authentifie toujours et répond en JSON.

Ce que ce fichier garde :

- **La symétrie atlassian ↔ folkmcp au champ près.** Le dashboard les pilote par un client
  GÉNÉRIQUE (`/api/${name}/oauth/…`) : une divergence de forme entre les deux casserait
  le widget pour l'un des deux sans qu'aucun test ne le voie.
- **Les champs racine de `google/oauth/status` décrivent le compte PAR DÉFAUT**, pas
  l'union des comptes — héritage du mono-compte. Sans défaut posé, ils sont vides alors
  que `connected` est vrai : c'est cohérent, et c'est contre-intuitif.
- **`DELETE /api/google/oauth` SANS paramètre révoque TOUT.** `account: null` dans la
  réponse veut dire « tous », pas « aucun ».
- **Le 500 `oauth_misconfigured:`** garde son format exact, espace compris : il signale
  une app OAuth mal configurée côté PLATEFORME, pas une erreur de l'appelant.
"""
from __future__ import annotations

import pytest

from _datastore_rest import call, stub_authz

from oto_mcp import db
from oto_mcp.auth import atlassian as atlassian_oauth
from oto_mcp.auth import folk as folk_oauth
from oto_mcp.auth import google as google_oauth
from oto_mcp.capabilities import federated_oauth as fo

_COMPTES = [
    {"google_email": "a@x.io", "is_default": True, "scopes": "s1 s2",
     "granted_at": "2026-08-01"},
    {"google_email": "b@x.io", "is_default": False, "scopes": None,
     "granted_at": "2026-08-02"},
]


@pytest.fixture()
def socle(monkeypatch):
    vus: list = []
    for mod, nom in ((atlassian_oauth, "atl"), (folk_oauth, "folk")):
        monkeypatch.setattr(mod, "build_auth_url",
                            (lambda n: lambda sub: f"https://{n}/auth?s={sub}")(nom))
        monkeypatch.setattr(mod, "status_for",
                            lambda sub: {"connected": True, "set_at": "2026-08-01"})
        monkeypatch.setattr(mod, "disconnect",
                            (lambda n: lambda sub: vus.append(("disc", n, sub)) or True)(nom))
    monkeypatch.setattr(google_oauth, "build_auth_url",
                        lambda sub: f"https://google/auth?s={sub}")
    monkeypatch.setattr(google_oauth, "list_accounts", lambda sub: list(_COMPTES))
    monkeypatch.setattr(google_oauth, "revoke",
                        lambda sub, account=None: vus.append(("revoke", sub, account)))
    monkeypatch.setattr(fo.access, "current_org", lambda sub: 35)
    monkeypatch.setattr(fo.db, "set_default_google_account",
                        lambda sub, oid, acc: vus.append(("defaut", sub, oid, acc)) or True)
    return vus


# --- Les deux fédérations MCP, au champ près --------------------------------

@pytest.mark.parametrize("nom", ["atlassian", "folkmcp"])
def test_start_est_retire_mesure_a_zero(nom):
    """oto-dashboard#125, 2026-09-04 : `.start` d'atlassian/folkmcp est SORTI du
    registre (mesuré à 0 appel/30j, toutes origines — contrairement à
    `me.federation.google.start`, resté). Un retour de cette clé serait une
    régression silencieuse du retrait, pas une amélioration à fêter."""
    from oto_mcp.capabilities.registry import CAPABILITIES
    assert f"me.federation.{nom}.start" not in {c.key for c in CAPABILITIES}


@pytest.mark.parametrize("nom", ["atlassian", "folkmcp"])
def test_status_rend_les_deux_memes_champs(monkeypatch, socle, nom):
    """La symétrie EST le contrat : le dashboard construit son URL à partir du nom du
    connecteur et lit la même forme pour les deux. Une divergence casserait l'un des
    deux widgets sans qu'aucun autre test ne le voie."""
    stub_authz(monkeypatch)
    code, out = call(f"me.federation.{nom}.status")
    assert code == 200
    assert set(out) == set(fo.FederationStatus.model_fields) == {"connected", "set_at"}


@pytest.mark.parametrize("nom", ["atlassian", "folkmcp"])
def test_jamais_connecte_rend_connected_false_et_set_at_null(monkeypatch, socle, nom):
    stub_authz(monkeypatch)
    mod = atlassian_oauth if nom == "atlassian" else folk_oauth
    monkeypatch.setattr(mod, "status_for",
                        lambda sub: {"connected": False, "set_at": None})
    _, out = call(f"me.federation.{nom}.status")
    assert out == {"connected": False, "set_at": None}


@pytest.mark.parametrize("nom,rendu", [("atlassian", True), ("atlassian", False),
                                       ("folkmcp", True), ("folkmcp", False)])
def test_la_deconnexion_est_idempotente(monkeypatch, socle, nom, rendu):
    """`disconnected: false` = il n'y avait rien à retirer, pas un échec. Le distinguer
    d'une erreur évite qu'un front affiche « échec » sur un geste sans effet."""
    stub_authz(monkeypatch)
    mod = atlassian_oauth if nom == "atlassian" else folk_oauth
    monkeypatch.setattr(mod, "disconnect", lambda sub: rendu)
    code, out = call(f"me.federation.{nom}.disconnect")
    assert (code, out) == (200, {"ok": True, "disconnected": rendu})


# --- Google, multi-compte ---------------------------------------------------

def test_les_champs_racine_decrivent_le_compte_par_defaut(monkeypatch, socle):
    """⚠️ PAS l'union des comptes : héritage du temps où Google était mono-compte. Un
    intégrateur qui lit `scopes` à la racine lit ceux du défaut, pas les siens."""
    stub_authz(monkeypatch)
    code, out = call("me.federation.google.status")
    assert code == 200
    assert out["connected"] is True
    assert out["granted_at"] == "2026-08-01" and out["scopes"] == ["s1", "s2"]
    assert [a["email"] for a in out["accounts"]] == ["a@x.io", "b@x.io"]
    # Le compte sans `scopes` en base rend une liste VIDE, jamais null.
    assert out["accounts"][1]["scopes"] == []
    assert set(out) == set(fo.GoogleStatus.model_fields)


def test_des_comptes_SANS_defaut_laissent_la_racine_vide(monkeypatch, socle):
    """`connected: true` avec `granted_at: null` est cohérent, et surprenant : il y a des
    comptes, aucun n'est élu par défaut. Le figer évite qu'on « corrige » la racine en y
    mettant le premier compte venu."""
    stub_authz(monkeypatch)
    monkeypatch.setattr(google_oauth, "list_accounts", lambda sub: [
        {"google_email": "b@x.io", "is_default": False, "scopes": "s1",
         "granted_at": "2026-08-02"}])
    _, out = call("me.federation.google.status")
    assert out["connected"] is True
    assert out["granted_at"] is None and out["scopes"] == []


def test_aucun_compte(monkeypatch, socle):
    stub_authz(monkeypatch)
    monkeypatch.setattr(google_oauth, "list_accounts", lambda sub: [])
    _, out = call("me.federation.google.status")
    assert out == {"connected": False, "granted_at": None, "scopes": [], "accounts": []}


def test_une_app_oauth_mal_configuree_est_un_500_qui_NOMME_la_cause(monkeypatch, socle):
    """Le format exact, espace compris : le code machine porte la cause. C'est une panne
    de PLATEFORME, pas une erreur de l'appelant — d'où le 5xx."""
    stub_authz(monkeypatch)

    def _boum(sub):
        raise RuntimeError("GOOGLE_CLIENT_ID absent")

    monkeypatch.setattr(google_oauth, "build_auth_url", _boum)
    code, out = call("me.federation.google.start")
    assert code == 500
    assert out["error"] == "oauth_misconfigured: GOOGLE_CLIENT_ID absent"


@pytest.mark.parametrize("query,attendu", [
    (b"", None),                       # SANS paramètre = TOUS les comptes
    (b"account=", None),               # vide = idem
    (b"account=a%40x.io", "a@x.io"),
])
def test_revoquer_sans_compte_revoque_TOUT(monkeypatch, socle, query, attendu):
    """⚠️ `account: null` dans la réponse veut dire « tous », pas « aucun ». C'est le
    geste le plus destructeur de cette surface et rien ne le signalait."""
    stub_authz(monkeypatch)
    code, out = call("me.federation.google.revoke", query=query)
    assert (code, out) == (200, {"ok": True, "account": attendu})
    assert socle[0] == ("revoke", "u-1", attendu)


def test_elire_un_defaut_le_normalise(monkeypatch, socle):
    stub_authz(monkeypatch)
    code, out = call("me.federation.google.set_default", body={"account": " a@x.io "})
    assert (code, out) == (200, {"ok": True, "default": "a@x.io"})
    assert socle[0] == ("defaut", "u-1", 35, "a@x.io")


@pytest.mark.parametrize("corps", [{}, {"account": ""}, {"account": "   "}])
def test_un_defaut_sans_compte_est_refuse(monkeypatch, socle, corps):
    stub_authz(monkeypatch)
    code, out = call("me.federation.google.set_default", body=corps)
    assert code == 400 and out["error"] == "missing_account"
    assert socle == []


def test_un_compte_inconnu_ou_hors_org_rend_le_meme_404(monkeypatch, socle):
    """Les deux causes — compte absent, ou aucune org de contexte — rendent
    `unknown_account`. C'est ce qui est servi ; le figer évite qu'un « affinage » invente
    un second code que personne ne lit."""
    stub_authz(monkeypatch)
    monkeypatch.setattr(fo.db, "set_default_google_account", lambda s, o, a: False)
    assert call("me.federation.google.set_default",
                body={"account": "z@x.io"})[1]["error"] == "unknown_account"
    monkeypatch.setattr(fo.access, "current_org", lambda sub: None)
    code, out = call("me.federation.google.set_default", body={"account": "a@x.io"})
    assert code == 404 and out["error"] == "unknown_account"


# --- Ce qui CHANGE pour un appelant -----------------------------------------

def test_un_champ_inconnu_est_desormais_refuse(monkeypatch, socle):
    stub_authz(monkeypatch)
    code, out = call("me.federation.google.revoke", query=b"compte=a%40x.io")
    assert code == 400
    assert out["error"] == "unknown_fields" and "compte" in out["detail"]
