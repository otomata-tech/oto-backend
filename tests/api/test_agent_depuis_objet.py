"""Basculer une procédure en agent programmé — depuis l'OBJET, pas depuis rien.

Direction du 02/09 : *« depuis le dashboard, sur une feuille de nœud, sur le
nœud, on puisse décider de le mettre en agent programmé »*. **L'agent autonome
est une propriété de ce qui existe déjà**, pas un objet séparé qu'on déclare.

Deux choix produit tranchés le 03/09 : la liste d'outils **se déduit de la
procédure**, et un objet ne porte **qu'un seul** agent programmé.

⚠️ Sans la déduction, le bouton demanderait une liste d'outils à l'utilisateur —
c'est-à-dire ne serait pas un bouton.
"""
from __future__ import annotations

import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

ROUTE = "/api/me/runner/triggers"


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@obj.invalid", "name": sub}


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

    nom = "oto_objet_" + uuid.uuid4().hex[:8]
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
    membre = "usr_objet"
    db.upsert_user(membre, email=f"{membre}@obj.invalid", name=membre)
    oid = org_store.create_org("Org des objets", created_by=membre)
    org_store.add_org_member(oid, membre, "org_admin")
    org_store.set_active_org(membre, oid)
    # Un worker a sondé : sans ça le serveur refuse de PROMETTRE une exécution. Il
    # nomme son dépôt, pour servir la famille du modèle que chaque agent déclare
    # désormais (24/09/2026) — ligne retirée à la sortie : la table est globale.
    db.claim_next_job(oid, "worker-du-banc")
    # Seul un worker de PLATEFORME déclare une famille servie (`runner_arme`).
    db.claim_next_job(None, "worker:banc-objet", depot="mistral")
    yield {"id": oid, "membre": membre}
    from oto_mcp.db._conn import _connect
    with _connect() as c:
        c.execute("DELETE FROM runner_platform_depots WHERE worker_sub = %s",
                  ("worker:banc-objet",))
        c.execute("DELETE FROM runner_platform_workers WHERE worker_sub = %s",
                  ("worker:banc-objet",))
        c.commit()


@pytest.fixture(scope="module")
def procedure(org):
    """Une procédure qui CITE ses outils — c'est d'elle qu'ils se déduisent."""
    from oto_mcp import db
    slug = "veille-du-matin"
    db.set_guide_db("org", str(org["id"]), slug,
                    "Relis le tableau et enrichis chaque ligne.\n"
                    "Utilise <tool:data_rows> pour lire et <tool:data_write> "
                    "pour écrire. En cas de doute, <tool:oto_doc>.",
                    title="Veille du matin")
    return slug


def test_les_outils_se_DEDUISENT_de_la_procedure(client, org, procedure):
    """⚠️ LE test du lot. Aucune liste d'outils n'est fournie : le serveur lit
    ceux que la procédure cite."""
    r = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "create", "model": "mistral-small-2603", "procedure": procedure, "cron": "0 7 * * *"})
    assert r.status_code == 200, r.text
    outils = r.json()["trigger"]["tools"]
    assert set(outils) == {"data_rows", "data_write", "oto_doc"}, (
        f"les outils n'ont pas été déduits de la procédure : {outils}")


def test_l_instruction_est_DERIVEE_et_pointe_l_objet(client, org, procedure):
    """⚠️ Une instruction rédigée à la main est un SECOND DOMICILE du métier : la
    même règle vivrait dans la procédure et dans l'instruction, et l'une des deux
    finirait par mentir. Une instruction qui POINTE l'objet ne peut pas diverger."""
    from oto_mcp import db
    t = db.triggers_for_procedure(org["id"], procedure)[0]
    assert procedure in (t["input"] or ""), "l'instruction ne nomme pas l'objet"
    assert len(t["input"] or "") < 400, (
        "l'instruction n'est pas minimale — elle recommence à porter du métier")


def test_PLUSIEURS_agents_par_objet(client, org, procedure):
    """Levé le 24/10/2026 : un second agent programmé sur le même objet est accepté —
    chacun a ses instructions, et une procédure en sections se partage entre eux.
    L'objet les rend tous les deux."""
    r = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "create", "model": "mistral-small-2603", "procedure": procedure,
                          "cron": "0 9 * * *", "input": "Applique UNIQUEMENT la section B."})
    assert r.status_code == 200, r.text
    from oto_mcp import db
    crons = sorted(t["cron"] for t in db.triggers_for_procedure(org["id"], procedure))
    assert crons == ["0 7 * * *", "0 9 * * *"]


def test_l_etat_se_lit_DEPUIS_l_objet(client, org, procedure):
    """L'écran d'une procédure demande « celle-ci tourne-t-elle ? ». Filtrer côté
    client devient faux dès qu'il y a plus d'une page de déclencheurs."""
    r = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "list", "procedure": procedure})
    assert r.status_code == 200, r.text
    lus = r.json()["triggers"]
    assert lus and all(t["procedure"] == procedure for t in lus)


def test_une_procedure_SANS_outil_cite_est_refusee_avec_la_raison(client, org):
    """⚠️ Le cas qui ne doit pas passer en silence : un agent sans outil n'exécute
    rien. Et le refus dit les DEUX issues — citer les outils, ou les passer."""
    from oto_mcp import db
    db.set_guide_db("org", str(org["id"]), "note-sans-outil",
                    "Réfléchis et conclus.", title="Note")
    r = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "create", "model": "mistral-small-2603", "procedure": "note-sans-outil",
                          "cron": "0 8 * * *"})
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "ne cite aucun outil" in detail
    assert "tools" in detail, "le refus ne dit pas l'autre issue"


def test_une_liste_FOURNIE_gagne_sur_la_deduction(client, org):
    """La déduction est un défaut, pas une contrainte : qui sait ce qu'il veut
    doit pouvoir le dire."""
    from oto_mcp import db
    db.set_guide_db("org", str(org["id"]), "avec-outils",
                    "Utilise <tool:data_rows>.", title="Avec outils")
    r = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "create", "model": "mistral-small-2603", "procedure": "avec-outils",
                          "cron": "0 10 * * *", "tools": ["oto_kb"]})
    assert r.status_code == 200, r.text
    assert r.json()["trigger"]["tools"] == ["oto_kb"]
