"""Le réglage du repli sur la clé de PLATEFORME, SUR LES ROUTES, la console et une vraie
base (19/11/2026, `docs/repli-cle-plateforme.md`).

1. `GET /api/orgs/{id}/model-subscriptions/{family}` rend le réglage de l'org
   (`platform_key_fallback`, qui et quand) et si l'instance le permet ;
2. `PUT …/{family}/platform-key-fallback` : org_admin seulement ; l'OUVRIR sur une
   instance qui ne le permet pas est refusé (409 `platform_key_fallback_not_permitted`),
   le REFERMER passe toujours ;
3. la console MCP (`oto_org_settings domain=model_subscriptions platform_key_fallback=…`)
   le règle seul, avec le même refus.
"""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

_F = "claude_subscription"


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@repli.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


def _h(sub: str) -> dict:
    return {"Authorization": f"Bearer {sub}"}


@pytest.fixture(scope="module")
def client(live):
    from oto_mcp.api import routes as api_routes
    return TestClient(Starlette(routes=api_routes.make_routes(_Verifier(),
                                                              mcp_instance=None)))


@pytest.fixture(scope="module")
def org(live):
    from oto_mcp import db, org_store
    admin, membre = "usr_rp_admin", "usr_rp_membre"
    for sub in (admin, membre):
        db.upsert_user(sub, email=f"{sub}@repli.invalid", name=sub)
    oid = org_store.create_org("Org du repli", created_by=admin)
    org_store.add_org_member(oid, admin, "org_admin")
    org_store.add_org_member(oid, membre, "org_member")
    return {"id": oid, "admin": admin, "membre": membre,
            "route": f"/api/orgs/{oid}/model-subscriptions/{_F}",
            "plateforme": f"/api/orgs/{oid}/model-subscriptions/{_F}/platform-key-fallback"}


@pytest.fixture
def permis(monkeypatch):
    def poser(valeur):
        if valeur is None:
            monkeypatch.delenv("OTO_REPLI_CLE_PLATEFORME_PERMIS", raising=False)
        else:
            monkeypatch.setenv("OTO_REPLI_CLE_PLATEFORME_PERMIS", valeur)
    poser(None)
    return poser


def test_la_lecture_dit_le_reglage_et_si_l_instance_le_permet(client, org, permis):
    lu = client.get(org["route"], headers=_h(org["membre"])).json()
    assert (lu["platform_key_fallback"], lu["platform_key_fallback_permitted"]) == (
        False, False)
    assert lu["platform_key_fallback_set_by"] is None
    permis("1")
    lu = client.get(org["route"], headers=_h(org["membre"])).json()
    assert lu["platform_key_fallback_permitted"] is True


def test_un_admin_l_ouvre_quand_l_instance_le_permet(client, org, permis):
    from oto_mcp.db import org_subscription_pool as P
    permis("1")
    avant = P.repli_api_actif(org["id"], _F)
    r = client.put(org["plateforme"], json={"platform_key_fallback": True},
                   headers=_h(org["admin"]))
    assert r.status_code == 200, r.text
    corps = r.json()
    assert corps["platform_key_fallback"] is True
    assert corps["platform_key_fallback_set_by"] == org["admin"]
    assert corps["platform_key_fallback_set_at"]
    assert corps["api_fallback"] is avant, "un geste par réglage : la porte n'a pas bougé"
    lu = client.get(org["route"], headers=_h(org["membre"])).json()
    assert lu["platform_key_fallback"] is True


def test_un_MEMBRE_ne_le_regle_pas(client, org, permis):
    from oto_mcp.db import org_subscription_pool as P
    permis("1")
    P.set_repli_cle_plateforme(org["id"], _F, False, org["admin"])
    r = client.put(org["plateforme"], json={"platform_key_fallback": True},
                   headers=_h(org["membre"]))
    assert r.status_code == 403, r.text
    assert P.get_mode(org["id"], _F)["repli_cle_plateforme"] is False


@pytest.mark.parametrize("valeur", [None, "0"])
def test_l_ouvrir_sur_une_instance_qui_ne_le_permet_pas_409(client, org, permis, valeur):
    from oto_mcp.db import org_subscription_pool as P
    permis("1")
    P.set_repli_cle_plateforme(org["id"], _F, False, org["admin"])
    permis(valeur)
    r = client.put(org["plateforme"], json={"platform_key_fallback": True},
                   headers=_h(org["admin"]))
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "platform_key_fallback_not_permitted"
    assert P.get_mode(org["id"], _F)["repli_cle_plateforme"] is False


def test_le_refermer_passe_toujours(client, org, permis):
    """Un réglage resté ouvert sur une instance qui ne le permet plus se referme."""
    from oto_mcp.db import org_subscription_pool as P
    permis("1")
    P.set_repli_cle_plateforme(org["id"], _F, True, org["admin"])
    permis(None)
    lu = client.get(org["route"], headers=_h(org["membre"])).json()
    assert (lu["platform_key_fallback"], lu["platform_key_fallback_permitted"]) == (
        True, False), "l'écran dit les deux : le réglage, et qu'il est sans effet ici"
    r = client.put(org["plateforme"], json={"platform_key_fallback": False},
                   headers=_h(org["admin"]))
    assert r.status_code == 200, r.text
    assert r.json()["platform_key_fallback"] is False


def test_famille_inconnue_400(client, org, permis):
    permis("1")
    r = client.put(f"/api/orgs/{org['id']}/model-subscriptions/anthropic/platform-key-fallback",
                   json={"platform_key_fallback": True}, headers=_h(org["admin"]))
    assert r.status_code == 400 and r.json()["error"] == "unknown_family"


def test_la_console_le_regle_seul(live, org, permis):
    """Face MCP (`oto_org_settings`) : un réglage par appel, le même refus."""
    from oto_mcp.capabilities import org_console as oc
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
    ctx = ResolvedCtx(sub=org["admin"], org_id=org["id"])
    S = oc.OrgSettingsInput
    permis("1")
    out = oc._org_settings(ctx, S(op="set", domain="model_subscriptions",
                                  org_id=org["id"], family=_F, platform_key_fallback=True))
    assert out["platform_key_fallback"] is True
    lu = oc._org_settings(ctx, S(op="get", domain="model_subscriptions",
                                 org_id=org["id"], family=_F))
    assert lu["platform_key_fallback"] is True and lu["platform_key_fallback_permitted"]
    for autre in ({"api_fallback": True}, {"mode": "pool"}, {"limit_pct": 50}):
        with pytest.raises(AuthzDenied) as e:
            oc._org_settings(ctx, S(op="set", domain="model_subscriptions",
                                    org_id=org["id"], family=_F,
                                    platform_key_fallback=False, **autre))
        assert e.value.code == "one_setting_per_call"
    permis(None)
    with pytest.raises(AuthzDenied) as e:
        oc._org_settings(ctx, S(op="set", domain="model_subscriptions",
                                org_id=org["id"], family=_F, platform_key_fallback=True))
    assert (e.value.status, e.value.code) == (409, "platform_key_fallback_not_permitted")
    out = oc._org_settings(ctx, S(op="set", domain="model_subscriptions",
                                  org_id=org["id"], family=_F, platform_key_fallback=False))
    assert out["platform_key_fallback"] is False


def test_la_console_d_un_membre_ne_regle_rien(live, org, permis):
    """La règle d'autorisation de la console, jouée telle que la face MCP la joue : `set`
    exige l'org_admin, `get` le membre."""
    from oto_mcp.capabilities import org_console as oc
    from oto_mcp.capabilities._types import AuthzDenied, RawCtx
    from oto_mcp.capabilities.registry import CAPABILITIES
    (console,) = [c for c in CAPABILITIES if c.key == "org.settings.console"]
    permis("1")
    S = oc.OrgSettingsInput
    poser = S(op="set", domain="model_subscriptions", org_id=org["id"], family=_F,
              platform_key_fallback=True)
    with pytest.raises(AuthzDenied) as e:
        console.authz(RawCtx(sub=org["membre"]), poser)
    assert e.value.status == 403
    assert console.authz(RawCtx(sub=org["admin"]), poser).org_id == org["id"]
    lire = S(op="get", domain="model_subscriptions", org_id=org["id"], family=_F)
    assert console.authz(RawCtx(sub=org["membre"]), lire).org_id == org["id"]
