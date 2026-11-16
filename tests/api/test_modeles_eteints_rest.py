"""Les modèles qu'une org éteint pour ses agents, SUR LA ROUTE et sur une vraie base :
`GET|PUT /api/orgs/{id}/models`. Lire est un geste de membre, basculer un geste d'admin
d'org ; tous allumés par défaut.
"""
from __future__ import annotations

import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@modeles.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


def _h(sub: str) -> dict:
    return {"Authorization": f"Bearer {sub}"}


@pytest.fixture(scope="module")
def live(pg_dsn):
    import os

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    nom = "oto_modeles_rest_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{nom}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + nom
    url_avant, pool_avant = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield dsn
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = pool_avant
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant
        root.execute(f'DROP DATABASE IF EXISTS "{nom}" WITH (FORCE)')
        root.close()


@pytest.fixture(scope="module")
def client(live):
    from oto_mcp.api import routes as api_routes
    return TestClient(Starlette(routes=api_routes.make_routes(_Verifier(),
                                                              mcp_instance=None)))


@pytest.fixture(scope="module")
def org(live):
    from oto_mcp import db, org_store
    admin, membre, dehors = "usr_modeles_admin", "usr_modeles_membre", "usr_modeles_dehors"
    for sub in (admin, membre, dehors):
        db.upsert_user(sub, email=f"{sub}@modeles.invalid", name=sub)
    oid = org_store.create_org("Org des modèles", created_by=admin)
    org_store.add_org_member(oid, admin, "org_admin")
    org_store.add_org_member(oid, membre, "org_member")
    return {"id": oid, "admin": admin, "membre": membre, "dehors": dehors,
            "route": f"/api/orgs/{oid}/models"}


def test_un_membre_lit_tous_allumes(client, org):
    r = client.get(org["route"], headers=_h(org["membre"]))
    assert r.status_code == 200, r.text
    assert r.json() == {"org_id": org["id"], "disabled": []}


def test_un_membre_ne_bascule_pas_et_un_etranger_ne_lit_pas(client, org):
    r = client.put(org["route"], json={"model": "claude-opus-5", "enabled": False},
                   headers=_h(org["membre"]))
    assert r.status_code == 403, r.text
    assert client.get(org["route"], headers=_h(org["dehors"])).status_code in (403, 404)


def test_un_admin_eteint_puis_rallume(client, org):
    r = client.put(org["route"], json={"model": "claude-opus-5", "enabled": False},
                   headers=_h(org["admin"]))
    assert r.status_code == 200, r.text
    assert r.json()["disabled"] == ["claude-opus-5"]
    assert r.json()["agents_on_model"] == {"count": 0, "ids": []}
    assert client.get(org["route"], headers=_h(org["membre"])).json()["disabled"] == [
        "claude-opus-5"]
    r = client.put(org["route"], json={"model": "claude-opus-5", "enabled": True},
                   headers=_h(org["admin"]))
    assert r.json()["disabled"] == []


def test_un_modele_inconnu_est_400(client, org):
    r = client.put(org["route"], json={"model": "sub:opus", "enabled": False},
                   headers=_h(org["admin"]))
    assert r.status_code == 400, r.text
    assert "unknown_model" in r.text
