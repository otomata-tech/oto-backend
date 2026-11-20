"""oto#227 — le cycle du RUN en REST : ouvrir un run, réserver sous lui, écrire sous le
bail, libérer, clore. Sur la VRAIE frontière : routes montées par `make_routes`, middleware
de consultation d'org, vrai PostgreSQL.

Avant ce lot, un consommateur REST ne pouvait pas écrire la ligne qu'il venait de
réserver : la garde du bail ne reconnaît le titulaire QUE par son run, la réservation
REST en posait un vide, et ouvrir un run n'existait qu'en MCP. Ce qui est tenu ici :

  1. run ouvert → réservation → écriture à la révision rendue par la réservation ;
  2. sans en-tête, autre run, run d'un autre compte, org non membre, org contradictoire,
     refus métier : ÉCHEC, et la ligne ne bouge pas ;
  3. révision périmée : 409, la ligne ne bouge pas ;
  4. libération puis nouvelle réservation ; clôture qui rend les lignes et ferme le run ;
  5. aucun contexte ne passe d'une requête à l'autre, ni d'une session à l'autre ;
  6. le contrôle de l'en-tête coûte UNE requête SQL ;
  7. un jeton porté ouvre un run s'il ÉCRIT un tableau, et pas autrement.
"""
from __future__ import annotations

import uuid

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.testclient import TestClient

ALICE = "usr_runs_rest_alice"
BOB = "usr_runs_rest_bob"


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@runs.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


def _h(bearer: str, run: str | None = None, org: int | None = None) -> dict:
    h = {"Authorization": f"Bearer {bearer}"}
    if run is not None:
        h["X-Oto-Run"] = run
    if org is not None:
        h["X-Oto-Org"] = str(org)
    return h


@pytest.fixture(scope="module")
def monde(live):
    """Org A : Alice et Bob. Org C : Alice seule. Org B : ni l'un ni l'autre."""
    from oto_mcp import db, org_store
    for sub in (ALICE, BOB):
        db.upsert_user(sub, email=f"{sub}@runs.invalid", name=sub)
    a = org_store.create_org("Org A", created_by=ALICE)
    b = org_store.create_org("Org B", created_by="usr_runs_rest_tiers")
    c = org_store.create_org("Org C", created_by=ALICE)
    org_store.add_org_member(a, ALICE, "org_member")
    org_store.add_org_member(a, BOB, "org_member")
    org_store.add_org_member(c, ALICE, "org_member")
    org_store.set_active_org(ALICE, a)
    org_store.set_active_org(BOB, a)
    return {"a": a, "b": b, "c": c}


@pytest.fixture(scope="module")
def client(live):
    from oto_mcp.api import routes as api_routes
    verifier = _Verifier()
    app = Starlette(routes=api_routes.make_routes(verifier, mcp_instance=None),
                    middleware=[Middleware(api_routes.ViewAsMiddleware, verifier=verifier)])
    return TestClient(app)


def _table(org: int, n: int = 3) -> tuple[str, int]:
    from oto_mcp import db
    ns = "fiches-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("org", str(org), ns)
    ns = str(ns_id)
    db.set_datastore_schema(ns_id, {"fields": [{"key": "statut"}]})
    for i in range(n):
        db.datastore_insert_row(ns_id, f"f{i}", {"statut": "a_appeler"})
    return ns, ns_id


def _ligne(ns_id: int, rid: str) -> dict:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        row = conn.execute("SELECT data, rev, claimed_by, claimed_run FROM datastore_rows "
                           "WHERE ns_id = %s AND row_id = %s", (ns_id, rid)).fetchone()
    return dict(row)


def _ouvrir(client, bearer: str = ALICE, **headers) -> str:
    r = client.post("/api/me/runs", headers=_h(bearer, **headers),
                    json={"label": "appel scout"})
    assert r.status_code == 201, r.text
    return r.json()["run_id"]


def _reserver(client, ns: str, run: str, worker: str, bearer: str = ALICE) -> dict:
    r = client.post(f"/api/datastores/{ns}/claim_next", headers=_h(bearer, run=run),
                    json={"worker": worker})
    assert r.status_code == 200, r.text
    return r.json()["row"]


# ── 1. le parcours ────────────────────────────────────────────────────────────

def test_ouvrir_reserver_puis_ECRIRE_sous_le_bail(client, monde):
    ns, ns_id = _table(monde["a"])
    run = _ouvrir(client)

    row = _reserver(client, ns, run, "scout-session-1")
    assert row["_claimed_run"] == run
    r = client.patch(f"/api/datastores/{ns}/rows/{row['_id']}"
                     f"?expected_revision={row['_revision']}",
                     headers=_h(ALICE, run=run), json={"statut": "appele"})

    assert r.status_code == 200, r.text
    assert _ligne(ns_id, row["_id"])["data"]["statut"] == "appele"


# ── 2. ce qui échoue, sans rien écrire ────────────────────────────────────────

def test_sans_en_tete_ou_sous_un_AUTRE_run_l_ecriture_est_refusee(client, monde):
    ns, ns_id = _table(monde["a"])
    run = _ouvrir(client)
    autre = _ouvrir(client)
    row = _reserver(client, ns, run, "scout-session-1")
    avant = _ligne(ns_id, row["_id"])

    sans = client.patch(f"/api/datastores/{ns}/rows/{row['_id']}", headers=_h(ALICE),
                        json={"statut": "vole"})
    sous_autre = client.patch(f"/api/datastores/{ns}/rows/{row['_id']}",
                              headers=_h(ALICE, run=autre), json={"statut": "vole"})

    assert (sans.status_code, sans.json()["error"]) == (409, "row_locked")
    assert (sous_autre.status_code, sous_autre.json()["error"]) == (409, "row_locked")
    assert _ligne(ns_id, row["_id"]) == avant


def test_le_run_d_un_AUTRE_compte_est_inconnu(client, monde):
    ns, ns_id = _table(monde["a"])
    run_de_bob = _ouvrir(client, BOB)
    avant = _ligne(ns_id, "f0")

    r = client.post(f"/api/datastores/{ns}/rows/f0/claim",
                    headers=_h(ALICE, run=run_de_bob), json={"worker": "scout-session-1"})

    assert (r.status_code, r.json()["error"]) == (404, "run_not_found")
    assert _ligne(ns_id, "f0") == avant


def test_un_run_d_une_org_dont_le_porteur_n_est_pas_membre_est_refuse(client, monde):
    from oto_mcp import db
    ns, ns_id = _table(monde["a"])
    run = uuid.uuid4().hex
    db.insert_run(run, sub=ALICE, org_id=monde["b"], label="org étrangère")
    avant = _ligne(ns_id, "f0")

    r = client.post(f"/api/datastores/{ns}/rows/f0/claim", headers=_h(ALICE, run=run),
                    json={"worker": "scout-session-1"})

    assert (r.status_code, r.json()["error"]) == (403, "forbidden")
    assert _ligne(ns_id, "f0") == avant


def test_une_org_de_consultation_CONTRADICTOIRE_est_refusee(client, monde):
    ns, ns_id = _table(monde["a"])
    run = _ouvrir(client)
    avant = _ligne(ns_id, "f0")

    r = client.post(f"/api/datastores/{ns}/rows/f0/claim",
                    headers=_h(ALICE, run=run, org=monde["c"]), json={"worker": "s1"})

    assert (r.status_code, r.json()["error"]) == (400, "run_org_mismatch")
    assert _ligne(ns_id, "f0") == avant


def test_un_refus_metier_sous_le_bail_n_ecrit_rien(client, monde):
    """Le refus métier le plus probable d'un client REST : le run passé DANS le corps au
    lieu de l'en-tête. Même sous un bail valide, rien ne s'écrit."""
    ns, ns_id = _table(monde["a"])
    run = _ouvrir(client)
    row = _reserver(client, ns, run, "scout-session-1")
    avant = _ligne(ns_id, row["_id"])

    r = client.patch(f"/api/datastores/{ns}/rows/{row['_id']}", headers=_h(ALICE, run=run),
                     json={"statut": "appele", "_run_id": run})

    assert (r.status_code, r.json()["error"]) == (400, "jeton_mal_place")
    assert _ligne(ns_id, row["_id"]) == avant


# ── 3. révision ───────────────────────────────────────────────────────────────

def test_une_revision_PERIMEE_reste_un_409(client, monde):
    """La révision rendue par la réservation vaut pour UNE écriture : la suivante, faite à
    la même révision, arrive sur une ligne qui a bougé depuis."""
    ns, ns_id = _table(monde["a"])
    run = _ouvrir(client)
    row = _reserver(client, ns, run, "scout-session-1")
    adresse = f"/api/datastores/{ns}/rows/{row['_id']}?expected_revision={row['_revision']}"
    premiere = client.patch(adresse, headers=_h(ALICE, run=run), json={"statut": "appele"})
    assert premiere.status_code == 200, premiere.text
    avant = _ligne(ns_id, row["_id"])

    r = client.patch(adresse, headers=_h(ALICE, run=run), json={"statut": "rappele"})

    assert (r.status_code, r.json()["error"]) == (409, "revision_conflict")
    assert _ligne(ns_id, row["_id"]) == avant


# ── 4. libérer, clore ─────────────────────────────────────────────────────────

def test_liberer_rend_la_ligne_reservable_par_un_autre_run(client, monde):
    ns, _ = _table(monde["a"], n=1)
    run, suivant = _ouvrir(client), _ouvrir(client)
    row = _reserver(client, ns, run, "scout-session-1")

    r = client.post(f"/api/datastores/{ns}/rows/{row['_id']}/release",
                    headers=_h(ALICE, run=run), json={"worker": "scout-session-1"})
    assert r.status_code == 200 and r.json()["released"] is True, r.text

    repris = _reserver(client, ns, suivant, "scout-session-2")
    assert (repris["_id"], repris["_claimed_run"]) == (row["_id"], suivant)


def test_clore_rend_les_lignes_ferme_le_run_et_le_fait_voir(client, monde):
    from oto_mcp.db import usage
    ns, ns_id = _table(monde["a"])
    run = _ouvrir(client)
    row = _reserver(client, ns, run, "scout-session-1")

    r = client.patch(f"/api/me/runs/{run}", headers=_h(ALICE),
                     json={"outcome": "done", "note": "appel consigné"})

    assert r.status_code == 200, r.text
    assert (r.json()["outcome"], r.json()["rows_released"]) == ("done", 1)
    assert _ligne(ns_id, row["_id"])["claimed_by"] is None
    vu = {x["run_id"]: x for x in usage.my_runs(ALICE, limit=50)}
    assert vu[run]["outcome"] == "done", "le run existe par ses faits, clôture comprise"
    apres = client.post(f"/api/datastores/{ns}/claim_next", headers=_h(ALICE, run=run),
                        json={"worker": "scout-session-1"})
    assert (apres.status_code, apres.json()["error"]) == (409, "run_closed")


def test_un_run_fini_A_MOITIE_se_clot_partial(client, monde):
    """oto#91 : `partial` est une issue acceptée par la face REST comme par le MCP, et
    elle se relit telle quelle dans le suivi."""
    from oto_mcp.db import usage
    run = _ouvrir(client)
    r = client.patch(f"/api/me/runs/{run}", headers=_h(ALICE),
                     json={"outcome": "partial", "note": "40 fiches faites, 12 restent"})
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "partial"
    vu = {x["run_id"]: x for x in usage.my_runs(ALICE, limit=50)}
    assert vu[run]["outcome"] == "partial"


def test_un_partial_SANS_note_est_refuse(client, monde):
    run = _ouvrir(client)
    r = client.patch(f"/api/me/runs/{run}", headers=_h(ALICE), json={"outcome": "partial"})
    assert (r.status_code, r.json()["error"]) == (400, "note_required")


def test_une_issue_inconnue_est_refusee_en_listant_les_valides(client, monde):
    run = _ouvrir(client)
    r = client.patch(f"/api/me/runs/{run}", headers=_h(ALICE), json={"outcome": "abandoned"})
    assert (r.status_code, r.json()["error"]) == (400, "invalid_outcome")
    assert "partial" in r.text


def test_seul_le_proprietaire_clot_son_run(client, monde):
    run = _ouvrir(client)
    r = client.patch(f"/api/me/runs/{run}", headers=_h(BOB), json={"outcome": "done"})
    assert (r.status_code, r.json()["error"]) == (404, "run_not_found")


# ── 5. aucune contamination ──────────────────────────────────────────────────

def test_le_run_d_une_requete_ne_passe_pas_a_la_suivante(client, monde):
    """⚠️ Deux requêtes dans la MÊME tâche. Le client de test ouvre une tâche par requête,
    qui copie le contexte : une pose jamais remise à zéro y passait inaperçue — vérifié par
    mutation, ce banc restait vert sans le `finally`. Enchaînées dans une seule coroutine
    sur le transport ASGI, les deux requêtes partagent le contexte où une ContextVar fuit."""
    import asyncio

    import httpx
    ns, ns_id = _table(monde["a"])
    run = _ouvrir(client)
    row = _reserver(client, ns, run, "scout-session-1")
    adresse = f"/api/datastores/{ns}/rows/{row['_id']}"

    async def _enchainees():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app),
                                     base_url="http://banc") as c:
            ok = await c.patch(adresse, headers=_h(ALICE, run=run),
                               json={"statut": "en_cours"})
            suivante = await c.patch(adresse, headers=_h(ALICE), json={"statut": "vole"})
            return ok, suivante

    ok, suivante = asyncio.run(_enchainees())

    assert ok.status_code == 200, ok.text
    assert (suivante.status_code, suivante.json()["error"]) == (409, "row_locked")
    assert _ligne(ns_id, row["_id"])["data"]["statut"] == "en_cours"


def test_deux_sessions_du_meme_jeton_n_ecrivent_que_leurs_lignes(client, monde):
    ns, ns_id = _table(monde["a"])
    run_1, run_2 = _ouvrir(client), _ouvrir(client)
    ligne_1 = _reserver(client, ns, run_1, "scout-session-1")
    ligne_2 = _reserver(client, ns, run_2, "scout-session-2")

    croisee = client.patch(f"/api/datastores/{ns}/rows/{ligne_1['_id']}",
                           headers=_h(ALICE, run=run_2), json={"statut": "vole"})
    propre = client.patch(f"/api/datastores/{ns}/rows/{ligne_2['_id']}",
                          headers=_h(ALICE, run=run_2), json={"statut": "appele"})

    assert (croisee.status_code, croisee.json()["error"]) == (409, "row_locked")
    assert propre.status_code == 200, propre.text
    assert _ligne(ns_id, ligne_1["_id"])["data"]["statut"] == "a_appeler"


# ── 6. le coût du contrôle ────────────────────────────────────────────────────

def test_le_controle_de_l_en_tete_coute_UNE_requete_sql(client, monde, monkeypatch):
    import psycopg

    from oto_mcp.capabilities import run_thread
    run = _ouvrir(client)
    requetes: list = []
    vraie = psycopg.Connection.execute

    def _comptee(self, query, *a, **k):
        requetes.append(str(query))
        return vraie(self, query, *a, **k)

    monkeypatch.setattr(psycopg.Connection, "execute", _comptee)
    assert run_thread.run_de_l_en_tete(ALICE, run) == monde["a"]
    assert len(requetes) == 1, requetes


def test_l_appartenance_jugee_par_l_en_tete_est_celle_de_roles(client, monde, monkeypatch):
    """La requête unique RECOPIE la règle d'appartenance de `roles.effective_org_role` — un
    rôle dans l'org, ou l'escalade super_admin, de base ou d'amorçage. Une recopie diverge
    en silence au premier changement de la règle : ce banc les confronte, verdict par
    verdict."""
    from oto_mcp import db, roles
    from oto_mcp.capabilities import run_thread
    from oto_mcp.capabilities._types import AuthzDenied
    from oto_mcp.db._conn import _connect

    chef, amorce = "usr_runs_rest_super", "usr_runs_rest_amorce"
    for sub in (chef, amorce):
        db.upsert_user(sub, email=f"{sub}@runs.invalid", name=sub)
    with _connect() as conn:
        conn.execute("UPDATE users SET role = 'super_admin' WHERE sub = %s", (chef,))
    monkeypatch.setenv("OTO_MCP_ADMIN_SUB", amorce)

    for sub, org in ((ALICE, monde["a"]), (ALICE, monde["b"]), (BOB, monde["c"]),
                     (chef, monde["b"]), (amorce, monde["b"])):
        run = uuid.uuid4().hex
        db.insert_run(run, sub=sub, org_id=org, label="parité d'appartenance")
        try:
            accepte = run_thread.run_de_l_en_tete(sub, run) == org
        except AuthzDenied as e:
            assert e.code == "forbidden", (sub, org, e.code)
            accepte = False
        assert accepte == roles.is_org_member(sub, org), (sub, org)


# ── 7. jeton porté : la famille des tableaux, rien d'autre ────────────────────

def test_un_jeton_porte_qui_ECRIT_un_tableau_suit_tout_le_parcours(client, monde):
    from oto_mcp import db
    ns, ns_id = _table(monde["a"])
    # La portée nomme le tableau par son IDENTIFIANT (oto#158) ; le parcours l'adresse
    # encore par son nom, jugé sur l'identifiant que ce nom résout.
    jeton = db.create_api_token(ALICE, label="scout",
                                scopes={"namespaces": {str(ns_id): "write"}})

    run = _ouvrir(client, jeton)
    row = _reserver(client, ns, run, "scout-session-1", bearer=jeton)
    ecrit = client.patch(f"/api/datastores/{ns}/rows/{row['_id']}",
                         headers=_h(jeton, run=run), json={"statut": "appele"})
    clos = client.patch(f"/api/me/runs/{run}", headers=_h(jeton), json={"outcome": "done"})

    assert ecrit.status_code == 200, ecrit.text
    assert clos.status_code == 200, clos.text
    assert _ligne(ns_id, row["_id"])["data"]["statut"] == "appele"


def test_un_jeton_porte_en_LECTURE_n_ouvre_pas_de_run(client, monde):
    from oto_mcp import db
    _, ns_id = _table(monde["a"])
    jeton = db.create_api_token(ALICE, label="lecture",
                                scopes={"namespaces": {str(ns_id): "read"}})

    r = client.post("/api/me/runs", headers=_h(jeton), json={"label": "appel scout"})

    assert (r.status_code, r.json()["error"]) == (403, "token_scope_forbidden")
