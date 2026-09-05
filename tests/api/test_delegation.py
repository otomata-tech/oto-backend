"""La délégation : le worker porte l'identité du demandeur, rien de plus.

Arbitré par Alexis le 02/09 : *« rien, il est juste un client MCP qui porte
l'identité du user »*. **Aucune primitive de sécurité n'est inventée** — le
serveur émet un jeton au nom du demandeur, borné à la durée du bail, et le worker
s'en sert comme n'importe quel client.

⚠️ Testé SUR LA ROUTE et sur une vraie base : un jeton est ce qui donne accès. Un
test qui lirait un dictionnaire de handler ne dirait rien de ce qui est
réellement servi — ni que le champ traverse la sérialisation, ni que le jeton
émis fonctionne.
"""
from __future__ import annotations

import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

ROUTE = "/api/me/runner/jobs"


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@deleg.invalid", "name": sub}


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

    nom = "oto_deleg_" + uuid.uuid4().hex[:8]
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
    membre = "usr_deleg_demandeur"
    db.upsert_user(membre, email=f"{membre}@deleg.invalid", name=membre)
    oid = org_store.create_org("Org de la délégation", created_by=membre)
    org_store.add_org_member(oid, membre, "org_admin")
    org_store.set_active_org(membre, oid)
    return {"id": oid, "membre": membre}


_DEFAUT = object()   # ⚠️ sentinelle : `None` est une VALEUR ici (pas de porteur
                     # connu), pas une absence d'argument. Les confondre rendait
                     # le cas « travail ancien » intestable — le test passait le
                     # porteur par défaut en croyant n'en passer aucun.


def _enfile(client, org, porteur=_DEFAUT):
    from oto_mcp import db
    return db.enqueue_job(org["id"], "start", payload={"procedure": "veille"},
                          sub=org["membre"] if porteur is _DEFAUT else porteur)


def test_la_reservation_rend_un_jeton_au_nom_du_demandeur(client, org):
    """Le fait central : le worker repart avec l'identité de QUELQU'UN D'AUTRE,
    sans avoir eu besoin d'un pouvoir propre."""
    from oto_mcp import db

    _enfile(client, org)
    r = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "claim", "lease_seconds": 60})
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    jeton = job["delegated_token"]
    assert jeton, "aucun jeton délégué : le worker n'a rien pour agir au nom du demandeur"

    # ⚠️ Le jeton MARCHE, et il porte le bon sub. Un jeton servi mais inopérant
    # serait pire que pas de jeton : le worker croirait avoir l'identité.
    vu = db.verify_api_token(jeton)
    assert vu is not None and vu["sub"] == org["membre"]


def test_le_jeton_delegue_EXPIRE_avec_le_bail(client, org):
    """⚠️ Un pouvoir emprunté doit se rendre. Sans échéance, chaque réservation
    laisserait derrière elle un accès permanent au nom d'un tiers — et personne
    ne compterait jamais combien il y en a."""
    from oto_mcp import db

    _enfile(client, org)
    r = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "claim", "lease_seconds": 60})
    jeton = r.json()["job"]["delegated_token"]
    with db._conn._connect() as conn:
        row = conn.execute(
            "SELECT expires_at IS NOT NULL AS borne FROM user_api_tokens "
            "WHERE token_hash = %s",
            (db.tokens._hash_token(jeton),),
        ).fetchone()
    assert row and row["borne"], "jeton de délégation SANS échéance"


def test_un_porteur_sans_role_arrete_le_travail_EN_LE_DISANT(client, org):
    """Les deux moitiés comptent : le travail s'arrête, **et** la raison est
    écrite. Un arrêt muet et un travail qui boucle se ressemblent de l'extérieur."""
    from oto_mcp import db, org_store

    parti = "usr_deleg_parti"
    db.upsert_user(parti, email=f"{parti}@deleg.invalid", name=parti)
    org_store.add_org_member(org["id"], parti, "org_member")
    job = _enfile(client, org, porteur=parti)
    org_store.remove_org_member(org["id"], parti)   # rôle retiré

    r = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "claim", "lease_seconds": 60})
    assert r.status_code == 200, r.text
    rendu = r.json()["job"]
    assert rendu["id"] == job["id"]
    assert rendu.get("delegated_token") is None, "un jeton a été émis malgré tout"
    assert "n'a plus de rôle" in (rendu.get("delegation_refusee") or "")

    # ⚠️ Et le travail est ARRÊTÉ, pas relâché : relâché, il repartirait au worker
    # suivant indéfiniment — une file qui tourne sans jamais aboutir.
    en_base = db.get_job(job["id"], org["id"])
    assert en_base["status"] == "failed"
    assert "identité invalide" in (en_base["last_error"] or "")


def test_un_travail_sans_porteur_connu_est_REFUSE(client, org):
    """⚠️ Renversement assumé du 05/09/2026. Ce banc disait « ce n'est pas un
    refus, c'est une absence » : les travaux d'avant le 02/09 n'ont pas de
    demandeur, on n'en inventait pas, et le worker retombait sur son propre
    jeton.

    Ce qui a changé n'est pas le cas, c'est le MODÈLE. Le worker est un serveur
    de boucles agentiques qui impersonnent chacune leur user ; il n'a **aucune
    identité métier**. « Retomber sur son propre jeton » n'est donc pas une
    absence de délégation : c'est une boucle qui agit au nom du compte hébergeant
    le runner, et tout ce qu'elle écrit signé par lui. Silencieux par
    construction — les écritures aboutissent, seule l'attribution est fausse.

    Une absence de porteur est donc bien un refus, et il s'écrit en base."""
    _enfile(client, org, porteur=None)
    r = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "claim", "lease_seconds": 60})
    job = r.json()["job"]
    assert job["sub"] is None
    assert job.get("delegated_token") is None
    assert "ne nomme personne" in (job.get("delegation_refusee") or "")

    from oto_mcp import db
    en_base = db.get_job(job["id"], org["id"])
    assert en_base["status"] == "failed", (
        "le refus se pose EN BASE : sinon le travail repart au worker suivant")


def test_une_file_vide_ne_delegue_rien_et_ne_casse_pas(client, org):
    """Le cas le plus fréquent en production : la file est vide. Le chemin de
    délégation ne doit pas s'y exécuter."""
    r = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "claim", "lease_seconds": 60})
    assert r.status_code == 200, r.text
    assert r.json()["job"] is None
