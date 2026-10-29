"""Une requête REST qui porte `X-Oto-Org` n'authentifie son porteur qu'UNE fois.

`ViewAsMiddleware` authentifie le porteur pour juger la consultation, puis le handler
rappelle `_authenticate` : la même requête payait deux fois la vérification du jeton,
la garde d'identité et l'`upsert_user` — sur chaque appel du tableau de bord, qui pose
`X-Oto-Org` partout. Le second appel reprend le porteur du premier (`CLE_PORTEUR`),
à options identiques seulement.

⚠️ **Chaque économie est éprouvée à l'envers** : la pause, l'org hors d'appartenance,
une route aux options plus strictes et le verrou d'un jeton de délégation tiennent
toujours. Le VRAI middleware et le VRAI `_authenticate`, sur des doublures sans base.
"""
from __future__ import annotations

import types

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from oto_mcp import access, garde_identite, org_store, session_org, verrou_org
from oto_mcp.api import base as api_base
from oto_mcp.api import routes as api_routes
from oto_mcp.auth import token_scopes

ORG, AUTRE = 42, 43
JWT, API = "Bearer eyJ-un-jwt", "Bearer oto_deadbeef"


class _Verificateur:
    def __init__(self):
        self.appels = 0

    async def verify_token(self, token):
        self.appels += 1
        return types.SimpleNamespace(claims={"sub": "u-jwt", "email": "a@b.test"})


@pytest.fixture
def banc(monkeypatch):
    compte = {"garde": 0, "upsert": 0, "jeton": 0, "pause": None,
              "ligne": {"sub": "u-api", "scopes": None, "token_id": 7}}

    def _garde(sub):
        compte["garde"] += 1
        return compte["pause"]

    def _upsert(*a, **k):
        compte["upsert"] += 1

    def _jeton(t):
        compte["jeton"] += 1
        return dict(compte["ligne"])

    monkeypatch.setattr(garde_identite, "refus", _garde)
    monkeypatch.setattr(api_base.db, "upsert_user", _upsert)
    monkeypatch.setattr(api_base.db, "verify_api_token", _jeton)
    monkeypatch.setattr(api_base, "alias_drain_armed", lambda: False)
    monkeypatch.setattr(org_store, "get_org_role",
                        lambda org_id, sub: "org_member" if int(org_id) == ORG else None)
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: False)

    verificateur = _Verificateur()
    vu: dict = {}

    def _route(**options):
        async def handler(request):
            sub, err = await api_base._authenticate(request, verificateur, **options)
            if err:
                return err
            vu.update(sub=sub, org=session_org.current_view_org(),
                      verrou=verrou_org.travail_de_la_requete())
            return JSONResponse({"sub": sub})
        return handler

    app = Starlette(routes=[
        Route("/api/jetons", _route(allow_api_token=False), methods=["GET"]),
        Route("/api/{p:path}", _route(), methods=["GET"]),
    ])
    client = TestClient(api_routes.ViewAsMiddleware(app, verifier=verificateur))
    yield client, compte, verificateur, vu
    token_scopes.set_current(None)
    verrou_org.poser(None)


def test_un_jwt_avec_x_oto_org_n_est_authentifie_qu_une_fois(banc):
    client, compte, verificateur, vu = banc
    r = client.get("/api/me", headers={"Authorization": JWT, "X-Oto-Org": str(ORG)})
    assert r.status_code == 200, r.text
    assert (verificateur.appels, compte["garde"], compte["upsert"]) == (1, 1, 1)
    assert vu["sub"] == "u-jwt" and vu["org"] == ORG


def test_un_jeton_api_avec_x_oto_org_n_est_verifie_qu_une_fois(banc):
    client, compte, _, vu = banc
    r = client.get("/api/me", headers={"Authorization": API, "X-Oto-Org": str(ORG)})
    assert r.status_code == 200, r.text
    assert (compte["jeton"], compte["garde"]) == (1, 1)
    assert vu["sub"] == "u-api"


def test_sans_en_tete_le_chemin_ne_change_pas(banc):
    """Le contrôle : sans `X-Oto-Org`, le middleware n'authentifie pas, le handler une fois."""
    client, compte, verificateur, _ = banc
    assert client.get("/api/me", headers={"Authorization": JWT}).status_code == 200
    assert (verificateur.appels, compte["garde"], compte["upsert"]) == (1, 1, 1)


def test_un_compte_en_pause_reste_refuse_et_le_refus_ne_se_memorise_pas(banc):
    client, compte, _, _ = banc
    compte["pause"] = ("account_suspended", "en pause")
    r = client.get("/api/me", headers={"Authorization": JWT, "X-Oto-Org": str(ORG)})
    assert r.status_code == 403 and r.json()["error"] == "account_suspended"
    # Le middleware a été refusé, il passe la main ; le handler refait TOUT et refuse.
    assert compte["garde"] == 2 and compte["upsert"] == 0


def test_une_org_dont_on_n_est_pas_membre_reste_refusee(banc):
    client, _, _, _ = banc
    r = client.get("/api/me", headers={"Authorization": JWT, "X-Oto-Org": str(AUTRE)})
    assert r.status_code == 403 and r.json()["error"] == "forbidden"


def test_une_route_aux_options_plus_strictes_refait_l_authentification(banc):
    """Le porteur mémorisé l'a été avec les jetons API permis : une route qui les
    refuse ne le reprend pas, elle juge — et refuse."""
    client, compte, _, _ = banc
    r = client.get("/api/jetons", headers={"Authorization": API, "X-Oto-Org": str(ORG)})
    assert r.status_code == 403 and r.json()["error"] == "api_token_forbidden"
    assert compte["jeton"] == 1  # le handler a refusé avant toute vérification


def test_le_verrou_d_un_jeton_de_delegation_survit_a_la_reprise(banc):
    """Le verrou d'org est REPOSÉ depuis la mémoire, pas supposé survivre : perdu,
    il laisserait un agent hébergé sortir de l'org de son travail."""
    client, compte, _, vu = banc
    compte["ligne"] = {"sub": "u-api", "scopes": None, "token_id": 7,
                       "verrou_org": True, "verrou_org_id": ORG, "job_id": 9}
    r = client.get("/api/me", headers={"Authorization": API, "X-Oto-Org": str(ORG)})
    assert r.status_code == 200, r.text
    assert compte["jeton"] == 1
    assert vu["verrou"] is not None and vu["verrou"].org_id == ORG
