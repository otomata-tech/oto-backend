"""Le modèle d'un agent programmé SUR LA ROUTE SERVIE — refus rejoués, catalogue sérialisé.

Les deux refus neufs de `runner.triggers` (`invalid_model`, `model_not_served`) sont
DÉCLARÉS : une déclaration sans rejeu promet un statut que le serveur ne rend
peut-être pas. Et le bloc `runner.models` est ce qu'un écran lit pour proposer un
modèle — un champ qui ne passe pas la sérialisation n'atteint aucun écran.
"""
from __future__ import annotations

import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

ROUTE = "/api/me/runner/triggers"


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@modele.invalid", "name": sub}


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

    nom = "oto_modele_rest_" + uuid.uuid4().hex[:8]
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
    membre = "usr_modele"
    db.upsert_user(membre, email=f"{membre}@modele.invalid", name=membre)
    oid = org_store.create_org("Org des modèles", created_by=membre)
    org_store.add_org_member(oid, membre, "org_admin")
    org_store.set_active_org(membre, oid)
    # Un agent ne se pose que sur une procédure que son propriétaire lit par nom (#1499).
    for slug in ("veille-a", "veille-b", "veille-c"):
        org_store.set_instruction("org", oid, slug, "Veille.")
    # Un worker de PLATEFORME a sondé, en nommant son dépôt : la file est vide, le
    # sondage n'écrit que la présence — et la famille qu'il sert.
    db.claim_next_job(None, "worker:banc-modele", depot="anthropic")
    return {"id": oid, "membre": membre}


def _poser(client, org, **champs):
    return client.post(ROUTE, headers=_h(org["membre"]), json={
        "op": "create", "cron": "0 7 * * *", "tools": ["oto_doc"], **champs})


def test_un_modele_hors_catalogue_est_refuse_sur_la_route(client, org):
    r = _poser(client, org, procedure="veille-a", model="gpt-9")
    assert (r.status_code, r.json().get("error")) == (400, "invalid_model"), r.text


def test_un_modele_que_personne_ne_sert_est_refuse_sur_la_route(client, org):
    r = _poser(client, org, procedure="veille-b", model="mistral-large-2512")
    assert (r.status_code, r.json().get("error")) == (400, "model_not_served"), r.text


def test_un_modele_servi_se_pose_et_se_relit(client, org):
    r = _poser(client, org, procedure="veille-c", model="claude-opus-5")
    assert r.status_code == 200, r.text
    tid = r.json()["trigger"]["id"]
    assert r.json()["trigger"]["model"] == "claude-opus-5"
    r = client.post(ROUTE, headers=_h(org["membre"]), json={"op": "get", "trigger_id": tid})
    assert r.json()["trigger"]["model"] == "claude-opus-5"


def test_le_catalogue_marque_se_serialise_sur_list(client, org):
    r = client.post(ROUTE, headers=_h(org["membre"]), json={"op": "list"})
    assert r.status_code == 200, r.text
    runner = r.json()["runner"]
    assert runner["families"] == ["anthropic"]
    servis = {m["id"]: m["served"] for m in runner["models"]}
    assert servis["claude-sonnet-5"] is True
    assert servis["mistral-large-2512"] is False
