"""Custom API (`api`) : un membre détient DEUX clés personnelles (palier membre).

Contre un VRAI PostgreSQL, par la vraie chaîne : la pose par la route REST du membre, la
résolution par le walker unique, l'outil `api_get` lancé comme `oto_call` le lance
(`executer_cible` : axes d'appel lus, épinglés puis retirés), et le mode `session` contre
un faux service local (le banc de `test_api_mode_session`).

La clé A, au compte `principal`, signe en mode session ; la clé B, au compte `autre`, porte
un jeton. Ce qui est prouvé :
- sans `_account`, l'appel ne se trompe pas de clé et ne prétend pas que rien n'est posé : il
  refuse, nommément, en listant les comptes (jamais un secret) ;
- `_account="principal"` lit par la clé A, en mode session ;
- l'axe `_account` est annoncé dans le schéma des outils `api*` dès la première clé ;
- une seule clé perso, ou une clé perso et une clé d'org, se résolvent comme avant.
"""
from __future__ import annotations

import asyncio
import json
import socket
import uuid
from types import SimpleNamespace

import pytest

from _datastore_rest import call

import test_api_mode_session as banc_session

CONNECTEUR = "api"
SECRETS = ("mot-de-passe-de-test", "JETON-B", "JETON-ORG")


@pytest.fixture
def service(live, monkeypatch):
    """Le faux service du mode session, joignable sous ses noms (`banc_session`)."""
    from oto_mcp import egress
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", banc_session._KEY)
    monkeypatch.delenv(egress.ALLOW_VAR, raising=False)
    monkeypatch.setattr(egress, "resolved_addresses", lambda hote, port: {"93.184.216.34"})
    vrai = socket.getaddrinfo

    def resoudre(hote, *a, **k):
        if isinstance(hote, str) and hote in (banc_session.API, banc_session.CONNEXION):
            hote = "127.0.0.1"
        return vrai(hote, *a, **k)
    monkeypatch.setattr(socket, "getaddrinfo", resoudre)
    etat = banc_session.Etat()
    api, port_api = banc_session._serveur(banc_session._api(etat))
    cx, port_cx = banc_session._serveur(banc_session._connexion(etat))
    base = f"http://{banc_session.API}:{port_api}"
    etat.auth_url = f"http://{banc_session.CONNEXION}:{port_cx}/connect?ticket=t1"
    etat.cible = f"{base}/code?code=c0de"
    yield SimpleNamespace(etat=etat, base=base)
    api.shutdown()
    cx.shutdown()


@pytest.fixture
def monde(service, monkeypatch):
    """Une org, `api` ouvert ; un membre simple A, son org pour active."""
    from oto_mcp import access, call_axes, db, org_store
    from oto_mcp.auth import hooks
    from oto_mcp.connectors import activation
    from oto_mcp.tools import api as outil_api
    u = uuid.uuid4().hex[:8]
    admin, a = f"admin_{u}", f"membre_a_{u}"
    for s in (admin, a):
        db.upsert_user(s, email=f"{s}@example.test")
    org = org_store.create_org(f"org_{u}", created_by=admin)
    org_store.add_org_member(org, admin, "org_admin")
    org_store.add_org_member(org, a)
    for s in (admin, a):
        org_store.set_active_org(s, org)
    activation.set_activation(CONNECTEUR, True, org_id=None, set_by=admin)
    for mod in (access, hooks, call_axes, outil_api):
        monkeypatch.setattr(mod, "current_user_sub_from_token", lambda: a, raising=False)
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: a)
    return SimpleNamespace(org=org, admin=admin, a=a, service=service)


def _poser(m, carte: dict):
    code, out = call("me.credential.set", path_params={"provider": CONNECTEUR},
                     body=carte, sub=m.a)
    assert code == 200, out


def _cle_session(m, compte: str | None = "principal"):
    carte = {"base_url": m.service.base, "auth_mode": "session",
             "username": "compte@example.org", "password": banc_session.MOT_DE_PASSE,
             "session": json.dumps(banc_session.ETAPES)}
    _poser(m, carte if compte is None else {**carte, "account": compte})


def _cle_jeton(m, compte: str = "autre"):
    _poser(m, {"base_url": "https://autre.example.test", "auth_mode": "bearer",
               "token": "JETON-B", "account": compte})


def _api_get(arguments: dict):
    """`api_get` lancé comme `oto_call` le lance : axes lus et retirés, puis l'outil."""
    from fastmcp import FastMCP
    from oto_mcp.tools import api as outil_api
    from oto_mcp.tools import meta
    m = FastMCP("t")
    outil_api.register(m)
    tool = asyncio.run(m.get_tool("api_get"))
    return asyncio.run(meta.executer_cible(tool, None, "api_get", "api_get",
                                           dict(arguments)))


def test_deux_cles_perso_sans_compte_refus_nomme_qui_liste_les_comptes(monde):
    _cle_session(monde)
    _cle_jeton(monde)
    issue = _api_get({"path": "/api/items"})
    assert not issue.ok
    assert "not configured" not in issue.message
    assert "`principal`" in issue.message and "`autre`" in issue.message
    assert "_account" in issue.message
    assert not any(s in issue.message for s in SECRETS)


def test_deux_cles_perso_le_compte_nomme_lit_en_mode_session(monde):
    _cle_session(monde)
    _cle_jeton(monde)
    issue = _api_get({"path": "/api/items", "_account": "principal"})
    assert issue.ok, issue.message
    assert issue.result.structured_content == {"items": [1, 2]}
    assert len(monde.service.etat.logins) == 1


def test_cle_session_sans_nom_lue_puis_renommee_par_la_seconde_cle(monde):
    """Le parcours vécu : la clé A est posée SANS nom de compte et sert (son jar est
    scellé sous la ligne `''`) ; la pose de B, nommée, renomme A en `principal`
    (`ensure_named_coexistence`). Le jar scellé pour l'ancienne ligne ne se relit plus :
    c'est un cache perdu — l'appel se reconnecte, jamais une erreur interne."""
    _cle_session(monde, compte=None)
    assert _api_get({"path": "/api/items"}).ok
    _cle_jeton(monde)
    issue = _api_get({"path": "/api/items", "_account": "principal"})
    assert issue.ok, issue.message
    assert issue.result.structured_content == {"items": [1, 2]}
    assert len(monde.service.etat.logins) == 2
    # Le jar neuf est scellé sous la ligne renommée : l'appel suivant ne se reconnecte pas.
    assert _api_get({"path": "/api/items", "_account": "principal"}).ok
    assert len(monde.service.etat.logins) == 2


def test_l_axe_compte_est_annonce_sur_les_outils_api_des_la_premiere_cle(monde):
    """Annoncé STATIQUEMENT (`account_axis_static`) : une session qui a figé sa liste
    d'outils avant la seconde clé voit déjà `_account` sur `api_get` — sans quoi il ne
    lui restait qu'`oto_call` pour viser une clé."""
    from oto_mcp import call_axes
    _cle_session(monde)
    for outil in ("api", "api_act", "api_get", "api_post", "api_doc"):
        assert any(a.param == "_account" for a in call_axes.axes_for_listing(outil, {})), \
            outil
    _cle_jeton(monde)
    annonce = call_axes.account_axis_advertised_for(monde.a)
    assert "`principal`" in annonce[CONNECTEUR] and "`autre`" in annonce[CONNECTEUR]


def test_une_seule_cle_perso_sert_sans_compte(monde):
    _cle_session(monde)
    issue = _api_get({"path": "/api/items"})
    assert issue.ok, issue.message
    assert issue.result.structured_content == {"items": [1, 2]}


def test_cle_perso_et_cle_d_org_la_perso_gagne_comme_avant(monde):
    from oto_mcp import credentials_store as cs
    cs.set_credential(cs.ORG, str(monde.org), CONNECTEUR,
                      cs.pack_secret(CONNECTEUR, {"base_url": "https://org.example.test",
                                                  "auth_mode": "bearer",
                                                  "token": "JETON-ORG"}),
                      set_by=monde.admin, account="principal")
    _cle_session(monde)
    issue = _api_get({"path": "/api/items"})
    assert issue.ok, issue.message
    assert issue.result.structured_content == {"items": [1, 2]}
