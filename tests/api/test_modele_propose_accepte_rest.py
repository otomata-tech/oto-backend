"""Le modèle PROPOSÉ par défaut est un modèle ACCEPTÉ — sur la route servie.

Le défaut du catalogue était posé en dur sur `claude-sonnet-5`, alors que les workers
de production ne servent que `mistral`. Un agent qui suivait la marque `default` lue
sur `op=list` se faisait refuser `model_not_served` par la garde qui juge ce qui est
servi : la surface proposait ce qu'elle refusait.

Un seul banc, qui rejoue le PARCOURS dans l'ordre — lire la proposition, la suivre,
relire — pour ne dépendre d'aucun ordre entre tests. Le modèle proposé est LU dans la
réponse, jamais écrit en dur à l'appel : c'est ce que ferait l'agent.

⚠️ `runner_platform_depots` est GLOBALE, pas par org : le banc tourne dans une base
neuve (`pg_module_dsn`) et son worker porte un identifiant propre, nettoyé à la sortie.
"""
from __future__ import annotations

import os
import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

# Ce banc ne parle pas de l'existence de la procédure d'un agent (#1499, conftest).
pytestmark = pytest.mark.usefixtures("procedures_connues")

TRIGGERS = "/api/me/runner/triggers"
FLEETS = "/api/me/runner/fleets"


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@propose.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


def _h(sub: str) -> dict:
    return {"Authorization": f"Bearer {sub}"}


@pytest.fixture(scope="module")
def live(pg_module_dsn):
    url_avant = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = pg_module_dsn
    try:
        from oto_mcp.db import init_db
        init_db()
        yield pg_module_dsn
    finally:
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant


@pytest.fixture(scope="module")
def client(live):
    from oto_mcp.api import routes as api_routes
    return TestClient(Starlette(routes=api_routes.make_routes(_Verifier(),
                                                              mcp_instance=None)))


@pytest.fixture(scope="module")
def org(live):
    from oto_mcp import db, org_store
    membre = "usr_propose"
    db.upsert_user(membre, email=f"{membre}@propose.invalid", name=membre)
    oid = org_store.create_org("Org du modèle proposé", created_by=membre)
    org_store.add_org_member(oid, membre, "org_admin")
    org_store.set_active_org(membre, oid)
    # Les flottes sont une surface BÊTA (étape 4).
    db.set_option_comp("org", str(oid), "beta", granted_by="test")
    return {"id": oid, "membre": membre}


@pytest.fixture
def worker(live):
    """Un worker de PLATEFORME propre au banc — ses lignes de présence sont retirées
    à la sortie, pour ne rien laisser dans une table globale."""
    from oto_mcp.db._conn import _connect
    sub = "worker:banc-propose-" + uuid.uuid4().hex[:8]
    try:
        yield sub
    finally:
        with _connect() as c:
            c.execute("DELETE FROM runner_platform_depots WHERE worker_sub = %s", (sub,))
            c.execute("DELETE FROM runner_platform_workers WHERE worker_sub = %s", (sub,))
            c.commit()


def _appel(client, org, route, **corps):
    return client.post(route, headers=_h(org["membre"]), json=corps)


def _runner(client, org) -> dict:
    r = _appel(client, org, TRIGGERS, op="list")
    assert r.status_code == 200, r.text
    return r.json()["runner"]


def _proposes(runner) -> list[str]:
    return [m["id"] for m in runner["models"] if m["default"] is True]


def _declencheur(client, org, procedure, **champs):
    return _appel(client, org, TRIGGERS, op="create", procedure=procedure,
                  cron="0 7 * * *", tools=["oto_doc"], **champs)


def _refus(r) -> tuple[int, str]:
    return r.status_code, (r.json() or {}).get("error", "")


def test_le_modele_propose_est_accepte_et_rien_ne_se_propose_sans_famille(
        client, org, worker):
    from oto_mcp import db

    # ── 1. un worker sonde SANS dépôt : armé, aucune famille, rien à proposer ──
    db.claim_next_job(None, worker)
    runner = _runner(client, org)
    assert runner["armed"] is True and runner["families"] == [], runner
    assert _proposes(runner) == [], (
        "étape 1 : aucune famille servie, et un modèle est proposé quand même")
    # Un agent hébergé déclare son modèle (24/09/2026) : sans famille servie, rien
    # ne se pose — et le défaut ne s'écrit jamais à sa place.
    r = _declencheur(client, org, "propose-sans-modele")
    assert _refus(r) == (400, "model_required"), f"étape 1, création sans modèle : {r.text}"
    r = _declencheur(client, org, "propose-mistral-trop-tot",
                     model="mistral-large-2512")
    assert _refus(r) == (400, "model_not_served"), f"étape 1 : {r.text}"

    # ── 2. le worker sonde avec son dépôt : la proposition se suit et se relit ──
    db.claim_next_job(None, worker, depot="mistral")
    runner = _runner(client, org)
    proposes = _proposes(runner)
    assert len(proposes) == 1, f"étape 2 : un et un seul modèle proposé, pas {proposes}"
    propose = proposes[0]
    r = _declencheur(client, org, "propose-suivi", model=propose)
    assert r.status_code == 200, (
        f"étape 2 : le modèle PROPOSÉ `{propose}` est refusé — {r.text}")
    suivi = r.json()["trigger"]
    r = _appel(client, org, TRIGGERS, op="get", trigger_id=suivi["id"])
    assert r.status_code == 200, r.text
    assert r.json()["trigger"]["model"] == propose
    assert propose == "mistral-large-2512", "étape 2 : la seule famille servie"

    # ── 3. un choix explicite non servi reste refusé, rien d'autre ne bouge ──
    r = _declencheur(client, org, "propose-opus-explicite", model="claude-opus-5")
    assert _refus(r) == (400, "model_not_served"), f"étape 3 : {r.text}"
    r = _appel(client, org, TRIGGERS, op="list")
    assert r.status_code == 200, r.text
    poses = {t["id"]: t["model"] for t in r.json()["triggers"]}
    assert poses == {suivi["id"]: propose}, (
        f"étape 3 : les déclencheurs posés ont changé — {poses}")

    # ── 4. même parcours côté flottes : déclarer sur la proposition, armer ──
    r = _appel(client, org, FLEETS, op="create", label="passage proposé",
               procedure="propose-flotte", tools=["oto_doc"], model=propose)
    assert r.status_code == 200, f"étape 4, déclaration : {r.text}"
    flotte = r.json()["fleet"]
    assert flotte["model"] == propose
    r = _appel(client, org, FLEETS, op="launch", fleet_id=flotte["id"])
    assert r.status_code == 200, f"étape 4, armement : {r.text}"
    assert r.json()["fleet"]["status"] == "armed"
