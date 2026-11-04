"""`GET /api/orgs/{id}/context/preview` (#1194) sur la route SERVIE — contre PostgreSQL.

Ce que ce fichier prouve, de la requête HTTP au stockage réel :
1. un org_admin de l'org et l'opérateur plateforme lisent l'aperçu ; un simple membre,
   un tiers, reçoivent le 403 nommé ;
2. les couches arrivent dans l'ordre d'injection, variables communes résolues, datées ;
3. les connecteurs sont ceux EXPOSÉS au membre de l'équipe (coupure d'équipe comprise) ;
4. les refus déclarés (`unknown_org`, `group_not_in_org`) sont rejoués.

Porteur identifié par un vérifieur factice dont le bearer EST le sub.
"""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

ADMIN, MEMBRE, TIERS, OPERATEUR = "am-admin", "am-membre", "am-tiers", "am-operateur"


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@apercu.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


def _h(sub: str) -> dict:
    return {"Authorization": f"Bearer {sub}"}


@pytest.fixture(scope="module")
def monde(live):
    from oto_mcp import db, group_store, guide_store, org_store, providers
    from oto_mcp.connectors import activation as connector_activation
    for sub in (ADMIN, MEMBRE, TIERS, OPERATEUR):
        db.upsert_user(sub, email=f"{sub}@apercu.invalid", name=sub.upper())
    db.set_user_role(OPERATEUR, "admin")
    o = org_store.create_org("Org Aperçu", created_by=ADMIN)
    org_store.add_org_member(o, ADMIN, "org_admin", actor=ADMIN)
    org_store.add_org_member(o, MEMBRE, "org_member", actor=ADMIN)
    autre = org_store.create_org("Org du tiers", created_by=TIERS)
    org_store.add_org_member(autre, TIERS, "org_admin", actor=TIERS)
    g = group_store.create_group(o, "Ventes", created_by=ADMIN)
    g_ailleurs = group_store.create_group(autre, "Ailleurs", created_by=TIERS)
    guide_store.set_init_guide("org", o, "Règles de {{org}} pour {{user}}.", edited_by=None)
    guide_store.set_init_guide("group", g, "Équipe {{équipe}} : on appelle avant.", edited_by=None)
    a, b = list(providers.REGISTRY)[:2]
    for nom in (a, b):
        connector_activation.set_activation(nom, True)
    connector_activation.set_group_activation(g, b, False, set_by=ADMIN)
    yield {"o": o, "autre": autre, "g": g, "g_ailleurs": g_ailleurs, "a": a, "b": b}


@pytest.fixture(scope="module")
def client(monde):
    from oto_mcp.api import routes as api_routes
    return TestClient(Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None)))


def test_l_admin_voit_ce_que_recoit_un_membre_de_l_equipe(client, monde):
    r = client.get(f"/api/orgs/{monde['o']}/context/preview", headers=_h(ADMIN),
                   params={"group_id": monde["g"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"org_id", "group_id", "layers", "connectors"}
    assert (body["org_id"], body["group_id"]) == (monde["o"], monde["g"])
    assert [c["scope"] for c in body["layers"]] == ["platform", "org", "group"]
    org, equipe = body["layers"][1], body["layers"][2]
    assert org["body_md"] == ("## Your organization's README (Org Aperçu)\n\n"
                              "Règles de Org Aperçu pour {{user}}.")
    assert equipe["body_md"].endswith("Équipe Ventes : on appelle avant.")
    assert org["updated_at"] and equipe["updated_at"]
    noms = [c["connector"] for c in body["connectors"]]
    assert monde["a"] in noms and monde["b"] not in noms, "la coupure d'équipe s'applique"
    assert set(body["connectors"][0]) == {"connector", "label", "category"}


def test_sans_equipe_l_org_seule(client, monde):
    r = client.get(f"/api/orgs/{monde['o']}/context/preview", headers=_h(ADMIN))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["group_id"] is None
    assert [c["scope"] for c in body["layers"]] == ["platform", "org"]
    assert {monde["a"], monde["b"]} <= {c["connector"] for c in body["connectors"]}


def test_l_operateur_plateforme_lit_l_apercu(client, monde):
    r = client.get(f"/api/orgs/{monde['o']}/context/preview", headers=_h(OPERATEUR))
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("qui", [MEMBRE, TIERS], ids=["simple-membre", "tiers"])
def test_refuse_a_qui_n_administre_pas_l_org(client, monde, qui):
    r = client.get(f"/api/orgs/{monde['o']}/context/preview", headers=_h(qui))
    assert r.status_code == 403, r.text
    assert r.json()["error"] == "forbidden"
    assert "Règles de" not in r.text


@pytest.mark.parametrize("cle", ["g_ailleurs", None], ids=["equipe-d-une-autre-org",
                                                          "equipe-absente"])
def test_equipe_hors_de_l_org(client, monde, cle):
    gid = monde[cle] if cle else 987654321
    r = client.get(f"/api/orgs/{monde['o']}/context/preview", headers=_h(ADMIN),
                   params={"group_id": gid})
    assert r.status_code == 404, r.text
    assert r.json()["error"] == "group_not_in_org"
    assert "Ailleurs" not in r.text


def test_org_inconnue(client, monde):
    r = client.get("/api/orgs/987654321/context/preview", headers=_h(OPERATEUR))
    assert r.status_code == 404, r.text
    assert r.json()["error"] == "unknown_org"
