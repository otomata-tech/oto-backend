"""QUAND un abonnement se replie, SUR LES ROUTES, la console et une vraie base
(22/11/2026, `docs/repli-cle-plateforme.md` §Quand se replier).

1. `GET /api/orgs/{id}/model-subscriptions/{family}` rend `fallback_when` (`limit` par
   défaut), qui l'a posé et quand — membres compris ;
2. `PUT …/{family}/fallback-when` `{"when": …}` : org_admin seulement, valeur au contrat ;
3. la console MCP (`oto_org_settings domain=model_subscriptions fallback_when=…`) le règle
   seul, et sa règle d'autorisation est celle de la face MCP (`set` = org_admin).
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
    admin, membre = "usr_rq_admin", "usr_rq_membre"
    for sub in (admin, membre):
        db.upsert_user(sub, email=f"{sub}@repli.invalid", name=sub)
    oid = org_store.create_org("Org du repli déconnecté", created_by=admin)
    org_store.add_org_member(oid, admin, "org_admin")
    org_store.add_org_member(oid, membre, "org_member")
    return {"id": oid, "admin": admin, "membre": membre,
            "route": f"/api/orgs/{oid}/model-subscriptions/{_F}",
            "quand": f"/api/orgs/{oid}/model-subscriptions/{_F}/fallback-when"}


@pytest.fixture(autouse=True)
def _defaut(monkeypatch):
    monkeypatch.delenv("OTO_REPLI_API_PAR_DEFAUT", raising=False)
    monkeypatch.delenv("OTO_REPLI_CLE_PLATEFORME_PERMIS", raising=False)


def test_une_org_sans_ligne_lit_limit(client, live):
    from oto_mcp import db, org_store
    sub = "usr_rq_seul"
    db.upsert_user(sub, email=f"{sub}@repli.invalid", name=sub)
    oid = org_store.create_org("Org sans réglage", created_by=sub)
    org_store.add_org_member(oid, sub, "org_admin")
    lu = client.get(f"/api/orgs/{oid}/model-subscriptions/{_F}", headers=_h(sub)).json()
    assert (lu["fallback_when"], lu["fallback_when_set_by"], lu["fallback_when_set_at"]) == (
        "limit", None, None)


def test_une_ligne_nee_d_un_autre_reglage_lit_limit(client, org):
    """Une ligne posée par le repli API, avant ou sans ce réglage : `plafond`, `limit`."""
    from oto_mcp.db import org_subscription_pool as P
    P.set_repli_api(org["id"], _F, True, org["admin"])
    lu = client.get(org["route"], headers=_h(org["membre"])).json()
    assert (lu["fallback_when"], lu["fallback_when_set_by"]) == ("limit", None)


def test_un_admin_l_elargit_et_un_membre_le_lit(client, org):
    from oto_mcp.db import org_subscription_pool as P
    avant = (P.repli_api_actif(org["id"], _F),
             bool((P.get_mode(org["id"], _F) or {}).get("repli_cle_plateforme")))
    r = client.put(org["quand"], json={"when": "limit_or_signed_out"},
                   headers=_h(org["admin"]))
    assert r.status_code == 200, r.text
    corps = r.json()
    assert corps["fallback_when"] == "limit_or_signed_out"
    assert corps["fallback_when_set_by"] == org["admin"] and corps["fallback_when_set_at"]
    assert (corps["api_fallback"], corps["platform_key_fallback"]) == avant, (
        "un geste par réglage : ni la porte ni le payeur n'ont bougé")
    assert P.get_mode(org["id"], _F)["repli_quand"] == "plafond_ou_deconnexion"
    lu = client.get(org["route"], headers=_h(org["membre"])).json()
    assert lu["fallback_when"] == "limit_or_signed_out"

    r = client.put(org["quand"], json={"when": "limit"}, headers=_h(org["admin"]))
    assert r.status_code == 200 and r.json()["fallback_when"] == "limit"


def test_un_MEMBRE_ne_le_regle_pas(client, org):
    from oto_mcp.db import org_subscription_pool as P
    P.set_repli_quand(org["id"], _F, "plafond", org["admin"])
    r = client.put(org["quand"], json={"when": "limit_or_signed_out"},
                   headers=_h(org["membre"]))
    assert r.status_code == 403, r.text
    assert P.get_mode(org["id"], _F)["repli_quand"] == "plafond"


@pytest.mark.parametrize("corps", [{"when": "always"}, {"when": None}, {}])
def test_une_valeur_hors_contrat_est_refusee(client, org, corps):
    from oto_mcp.db import org_subscription_pool as P
    P.set_repli_quand(org["id"], _F, "plafond", org["admin"])
    r = client.put(org["quand"], json=corps, headers=_h(org["admin"]))
    assert 400 <= r.status_code < 500 and r.status_code != 403, r.text
    assert P.get_mode(org["id"], _F)["repli_quand"] == "plafond"


def test_famille_inconnue_400(client, org):
    r = client.put(f"/api/orgs/{org['id']}/model-subscriptions/anthropic/fallback-when",
                   json={"when": "limit"}, headers=_h(org["admin"]))
    assert r.status_code == 400 and r.json()["error"] == "unknown_family"


def test_la_console_le_regle_seul(live, org):
    """Face MCP (`oto_org_settings`) : un réglage par appel, une valeur au contrat."""
    from oto_mcp.capabilities import org_console as oc
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
    ctx = ResolvedCtx(sub=org["admin"], org_id=org["id"])
    S = oc.OrgSettingsInput
    out = oc._org_settings(ctx, S(op="set", domain="model_subscriptions", org_id=org["id"],
                                  family=_F, fallback_when="limit_or_signed_out"))
    assert out["fallback_when"] == "limit_or_signed_out"
    lu = oc._org_settings(ctx, S(op="get", domain="model_subscriptions",
                                 org_id=org["id"], family=_F))
    assert lu["fallback_when"] == "limit_or_signed_out"
    for autre in ({"api_fallback": True}, {"platform_key_fallback": False},
                  {"mode": "pool"}, {"limit_pct": 50}):
        with pytest.raises(AuthzDenied) as e:
            oc._org_settings(ctx, S(op="set", domain="model_subscriptions",
                                    org_id=org["id"], family=_F, fallback_when="limit",
                                    **autre))
        assert e.value.code == "one_setting_per_call"
    with pytest.raises(AuthzDenied) as e:
        oc._org_settings(ctx, S(op="set", domain="model_subscriptions", org_id=org["id"],
                                family=_F, fallback_when="plafond_ou_deconnexion"))
    assert (e.value.status, e.value.code) == (400, "invalid_fallback_when"), (
        "la surface parle `limit` / `limit_or_signed_out`, jamais le mot de la base")
    out = oc._org_settings(ctx, S(op="set", domain="model_subscriptions", org_id=org["id"],
                                  family=_F, fallback_when="limit"))
    assert out["fallback_when"] == "limit"


def test_la_console_d_un_membre_ne_regle_rien(live, org):
    from oto_mcp.capabilities import org_console as oc
    from oto_mcp.capabilities._types import AuthzDenied, RawCtx
    from oto_mcp.capabilities.registry import CAPABILITIES
    (console,) = [c for c in CAPABILITIES if c.key == "org.settings.console"]
    S = oc.OrgSettingsInput
    poser = S(op="set", domain="model_subscriptions", org_id=org["id"], family=_F,
              fallback_when="limit_or_signed_out")
    with pytest.raises(AuthzDenied) as e:
        console.authz(RawCtx(sub=org["membre"]), poser)
    assert e.value.status == 403
    assert console.authz(RawCtx(sub=org["admin"]), poser).org_id == org["id"]
    lire = S(op="get", domain="model_subscriptions", org_id=org["id"], family=_F)
    assert console.authz(RawCtx(sub=org["membre"]), lire).org_id == org["id"]
