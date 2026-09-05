"""Le rattachement d'un travail à sa flotte, créé PAR LE CHEMIN SERVI (#791).

⚠️ **Ce fichier existe parce que le précédent trichait.** Le lot R4 a livré la
colonne `runner_jobs.fleet_id`, son index, sa clé étrangère et l'agrégat qui la
lit — **sans le moindre écrivain servi**. `op=state` répondait donc
`no_jobs_attached` pour toute flotte, toujours, et le test qui prouvait le
contraire fabriquait le lien par un `INSERT` SQL direct qu'aucun client ne peut
faire :

```sql
INSERT INTO runner_jobs (org_id, kind, fleet_id, status, result) VALUES (…)
```

> **Un harnais qui prouve un chemin de LECTURE ne prouve pas qu'il existe un
> chemin d'ÉCRITURE pour ce qu'il lit.** C'est la même faute que celle que R4
> venait de fermer — vérifier ailleurs que là où le geste se produit — appliquée
> à l'autre moitié du trajet.

Ici tout passe par HTTP : la flotte se déclare par sa route, les travaux
s'enfilent par la leur avec leur `fleet_id`, et l'état se lit par la sienne. **Si
un maillon manque, ces tests le disent** — c'est leur seule raison d'être.

Et le maillon de sécurité qui va avec : **la clé étrangère garantit qu'une flotte
EXISTE, pas qu'elle soit celle de cette org.** Sans garde, un travail se
rattacherait à la flotte d'autrui et ferait entrer son coût dans l'état d'un
passage étranger — état faux des deux côtés, et observabilité qui fuit.
"""
from __future__ import annotations

import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

FLEETS = "/api/me/runner/fleets"
JOBS = "/api/me/runner/jobs"


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@jf.invalid", "name": sub}


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

    nom = "oto_jobs_fleet_" + uuid.uuid4().hex[:8]
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


def _org(nom: str, sub: str) -> int:
    from oto_mcp import db, org_store
    db.upsert_user(sub, email=f"{sub}@jf.invalid", name=sub)
    oid = org_store.create_org(nom, created_by=sub)
    org_store.add_org_member(oid, sub, "org_admin")
    org_store.set_active_org(sub, oid)
    # Les flottes sont une surface bêta : la route refuse `beta_required` sans
    # l'option — le banc parle de travaux, pas de la porte, il l'ouvre donc.
    db.set_option_comp("org", str(oid), "beta", granted_by="test")
    return oid


@pytest.fixture(scope="module")
def maison(live):
    return {"id": _org("Org du passage", "usr_jf_maison"), "sub": "usr_jf_maison"}


@pytest.fixture(scope="module")
def voisin(live):
    return {"id": _org("Org voisine", "usr_jf_voisin"), "sub": "usr_jf_voisin"}


@pytest.fixture(scope="module")
def flotte(client, maison):
    r = client.post(FLEETS, headers=_h(maison["sub"]), json={
        "op": "create", "label": "passage", "procedure": "enrichissement",
        "tools": ["oto_kb"], "max_rows": 10})
    assert r.status_code == 200, r.text
    return r.json()["fleet"]


# ── le trajet complet, uniquement par les routes ─────────────────────────────

def test_un_travail_enfile_pour_une_flotte_la_declare_et_la_rend(client, maison, flotte):
    """Le maillon qui manquait : `enqueue` accepte le rattachement, et le REDIT.

    Le redire n'est pas cosmétique — sans ça l'appelant suppose que son
    rattachement a été pris, et découvre au bilan qu'un passage est vide."""
    r = client.post(JOBS, headers=_h(maison["sub"]), json={
        "op": "enqueue", "kind": "start", "payload": {"procedure": "p"},
        "fleet_id": flotte["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["fleet_id"] == flotte["id"]


def _etat(client, maison, flotte):
    return client.post(FLEETS, headers=_h(maison["sub"]),
                       json={"op": "state", "fleet_id": flotte["id"]}).json()["state"]


def test_l_etat_du_passage_compte_les_travaux_enfiles_par_la_route(client, maison, flotte):
    """LE test que le lot précédent ne pouvait pas écrire : plus une seule
    écriture SQL, tout le trajet passe par les routes servies.

    ⚠️ Il MESURE SON PROPRE DELTA, et ne suppose rien de ce que la flotte porte
    déjà. Il attendait `>= 3` en n'enfilant que 2 : le troisième venait d'un test
    voisin, donc il ne passait que dans un certain ordre — et un cliquet qui ne
    tient que dans un ordre ne tient pas. Le delta exact dit d'ailleurs plus que
    le seuil : il prouve que la route compte CE qu'elle enfile, ni plus ni moins.
    """
    avant = _etat(client, maison, flotte)
    base = 0 if avant["no_jobs_attached"] else avant["jobs_total"]
    base_pending = 0 if avant["no_jobs_attached"] else (avant.get("pending") or 0)

    for _ in range(2):
        assert client.post(JOBS, headers=_h(maison["sub"]), json={
            "op": "enqueue", "kind": "start", "payload": {"procedure": "p"},
            "fleet_id": flotte["id"]}).status_code == 200

    etat = _etat(client, maison, flotte)
    assert etat["no_jobs_attached"] is False, (
        "le passage doit compter ses travaux — s'il dit encore « aucun travail », "
        "c'est qu'aucun écrivain ne pose le rattachement")
    assert etat["jobs_total"] == base + 2
    assert (etat.get("pending") or 0) == base_pending + 2


def test_un_travail_dit_a_quel_passage_il_appartient(client, maison, flotte):
    """La projection le rend : sinon le lien n'existe que dans la base, et un
    écran qui liste les travaux ne peut pas les grouper par passage."""
    lot = client.post(JOBS, headers=_h(maison["sub"]),
                      json={"op": "list"}).json()["jobs"]
    assert lot, "la file n'est pas vide"
    assert any(j.get("fleet_id") == flotte["id"] for j in lot), (
        f"aucun travail ne déclare son passage — projeté : {sorted(lot[0])}")


def test_un_travail_sans_flotte_reste_possible(client, maison):
    """Un déclencheur ou un appel direct n'appartient à aucun passage : le
    rattachement est facultatif, et son absence se dit `null`, pas `0`."""
    r = client.post(JOBS, headers=_h(maison["sub"]), json={
        "op": "enqueue", "kind": "start", "payload": {"procedure": "p"}})
    assert r.status_code == 200 and r.json()["fleet_id"] is None


# ── la garde d'appartenance : exister ne suffit pas ───────────────────────────

def test_on_ne_rattache_pas_un_travail_a_la_flotte_d_une_autre_org(
        client, voisin, flotte):
    """⚠️ La clé étrangère laisserait passer : la flotte EXISTE. Ce qu'elle ne
    dit pas, c'est à QUI elle est.

    Sans cette garde, le coût et l'avancement d'un travail entreraient dans
    l'état du passage d'une autre organisation — faux des deux côtés, et une
    fuite d'observabilité vers quelqu'un qui n'a rien demandé."""
    r = client.post(JOBS, headers=_h(voisin["sub"]), json={
        "op": "enqueue", "kind": "start", "payload": {"procedure": "p"},
        "fleet_id": flotte["id"]})
    assert (r.status_code, r.json().get("error")) == (404, "fleet_not_found")


def test_une_flotte_inexistante_est_refusee_de_la_meme_facon(client, maison):
    """Même refus, sans oracle : un identifiant inconnu et un identifiant qui
    appartient à autrui ne doivent pas se distinguer par la réponse."""
    r = client.post(JOBS, headers=_h(maison["sub"]), json={
        "op": "enqueue", "kind": "start", "payload": {"procedure": "p"},
        "fleet_id": 999_999_999})
    assert (r.status_code, r.json().get("error")) == (404, "fleet_not_found")


def test_l_etat_du_voisin_ne_voit_rien_de_ces_travaux(client, voisin, maison, flotte):
    """La contre-épreuve de la garde : le passage du voisin reste vide, et le
    sien à lui n'est pas atteignable depuis l'autre org."""
    r = client.post(FLEETS, headers=_h(voisin["sub"]),
                    json={"op": "state", "fleet_id": flotte["id"]})
    assert (r.status_code, r.json().get("error")) == (404, "fleet_not_found")


# ── le cœur de la garde « un déroulé n'arrête pas celle qui l'exécute » ───────
#
# ⚠️ La garde complète vit dans la capacité et ne mord que sur la face MCP : en
# REST il n'y a pas de déroulé, donc `_run_courant()` y rend toujours `None`.
# Tester le refus par HTTP prouverait qu'un appel sans run n'est pas bloqué —
# vrai, et sans rapport. Ce qui se teste ici, c'est le PRÉDICAT sur lequel elle
# repose : ce déroulé tourne-t-il pour CETTE flotte ?

def test_le_predicat_distingue_SA_flotte_des_autres(client, maison, flotte):
    """Un agent doit pouvoir arrêter une AUTRE flotte de son org — c'est même le
    cas utile : un opérateur qui pilote par la conversation. Ce qu'on interdit,
    c'est qu'il coupe celle qui l'exécute. Un prédicat trop large fermerait les
    deux."""
    from oto_mcp import db
    from oto_mcp.db._conn import _connect

    autre = client.post(FLEETS, headers=_h(maison["sub"]), json={
        "op": "create", "label": "une-autre", "procedure": "p",
        "tools": ["oto_kb"]}).json()["fleet"]

    with _connect() as c:
        c.execute("INSERT INTO runs (run_id, sub, org_id, label) "
                  "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                  ("run-garde", maison["sub"], maison["id"], "garde"))
        c.execute("INSERT INTO runner_jobs (org_id, kind, run_id, fleet_id) "
                  "VALUES (%s, 'start', %s, %s)",
                  (maison["id"], "run-garde", flotte["id"]))
        c.commit()

    assert db.run_appartient_a_flotte("run-garde", flotte["id"]) is True, (
        "le déroulé tourne bien POUR cette flotte — il ne doit pas pouvoir la couper")
    assert db.run_appartient_a_flotte("run-garde", autre["id"]) is False, (
        "mais il doit pouvoir arrêter une AUTRE flotte : un prédicat trop large "
        "fermerait le cas utile en même temps que le cas dangereux")
    assert db.run_appartient_a_flotte("run-inconnu", flotte["id"]) is False
