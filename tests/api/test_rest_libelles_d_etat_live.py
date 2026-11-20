"""`lifecycle.labels` sur les faces SERVIES (oto#140) : posé, relu tel quel, complété
par `PATCH …/schema` sans rien effacer, refusé quand il nomme un état inconnu.

Contre un vrai PostgreSQL et la table de routes réelle (`make_routes`) : c'est ce que
le dashboard lit, pas ce que le store fait quand on l'appelle à la main.
"""
from __future__ import annotations

import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@libelles.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


SUB = "usr_libelles"


def _h() -> dict:
    return {"Authorization": f"Bearer {SUB}"}


@pytest.fixture(scope="module")
def live(pg_dsn):
    import os

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_libelles_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    previous_url, previous_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        from oto_mcp import db
        db.upsert_user(SUB, email=f"{SUB}@libelles.invalid", name=SUB)
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = previous_pool
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


@pytest.fixture(scope="module")
def client(live):
    from oto_mcp.api import routes as api_routes
    return TestClient(Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None)))


_CYCLE = {"states": ["a_qualifier", "en_cours", "perdu"],
          "transitions": {"a_qualifier": ["en_cours"], "en_cours": ["perdu"]},
          "terminal": ["perdu"]}


def _table(client, lifecycle) -> str:
    from oto_mcp import db
    ns = "t-" + uuid.uuid4().hex[:6]
    ns = str(db.create_datastore("user", SUB, ns))
    r = client.put(f"/api/datastores/{ns}/schema", headers=_h(), json={"schema": {
        "fields": [{"key": "ref", "type": "text"},
                   {"key": "statut", "type": "text", "role": "status",
                    "lifecycle": lifecycle}]}})
    assert r.status_code == 200, r.text
    return ns


def _cycle_servi(client, ns) -> dict:
    r = client.get(f"/api/datastores/{ns}/schema", headers=_h())
    assert r.status_code == 200, r.text
    return next(f for f in r.json()["schema"]["fields"] if f["key"] == "statut")["lifecycle"]


def test_poses_puis_servis_TELS_QUELS(client):
    labels = {"a_qualifier": "À qualifier", "perdu": "Perdu"}
    ns = _table(client, {**_CYCLE, "labels": labels})
    assert _cycle_servi(client, ns)["labels"] == labels


def test_le_PATCH_les_pose_sur_un_tableau_existant_sans_rien_effacer(client):
    ns = _table(client, dict(_CYCLE))
    r = client.patch(f"/api/datastores/{ns}/schema", headers=_h(), json={
        "fields": [{"key": "statut", "lifecycle": {"labels": {"a_qualifier": "À qualifier"}}}]})
    assert r.status_code == 200, r.text
    r = client.patch(f"/api/datastores/{ns}/schema", headers=_h(), json={
        "fields": [{"key": "statut", "lifecycle": {"labels": {"en_cours": "En cours"}}}]})
    assert r.status_code == 200, r.text
    lc = _cycle_servi(client, ns)
    assert lc == {**_CYCLE, "labels": {"a_qualifier": "À qualifier", "en_cours": "En cours"}}


def test_un_etat_inconnu_est_refuse_en_400_et_nomme(client):
    ns = _table(client, dict(_CYCLE))
    r = client.patch(f"/api/datastores/{ns}/schema", headers=_h(), json={
        "fields": [{"key": "statut", "lifecycle": {"labels": {"gagne": "Gagné"}}}]})
    assert r.status_code == 400, r.text
    assert "'gagne'" in r.text and "état inconnu" in r.text
    assert "labels" not in _cycle_servi(client, ns), "un refus ne pose rien"
