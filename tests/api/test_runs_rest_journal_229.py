"""oto#229 — un appel REST fait sous `X-Oto-Run` est estampillé de son run au journal.

Suite de oto#227 : un intégrateur REST ouvre un run, réserve et écrit sous lui, le
clôt. Le journal REST (`api.routes.RestCallLogger`) est un middleware ASGI EXTERNE :
il écrit sa ligne APRÈS que l'adaptateur a remis la ContextVar du run à zéro, et
la colonne `run_id` des lignes `kind='rest'` restait vide. La timeline du run ne
montrait que son ouverture et sa clôture — alors qu'un appel MCP fait avec
`_run_id` y est rattaché.

Le run JUGÉ par l'adaptateur est publié dans le scope ASGI, comme le principal, et
relu par le journal. Tenu ici :

  1. une écriture REST sous `X-Oto-Run` apparaît dans la timeline de son run — et
     dans celle de son ORG, même sans `X-Oto-Org` ;
  2. une requête sans en-tête reste sans run au journal, y compris juste après une
     requête qui en portait un, dans la même tâche ;
  3. le journal n'ajoute aucune requête SQL pour le savoir.
"""
from __future__ import annotations

import uuid

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.testclient import TestClient

ALICE = "usr_runs_journal_alice"


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@runs.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


def _h(run: str | None = None) -> dict:
    h = {"Authorization": f"Bearer {ALICE}"}
    if run is not None:
        h["X-Oto-Run"] = run
    return h


@pytest.fixture(scope="module")
def org(live):
    from oto_mcp import db, org_store
    db.upsert_user(ALICE, email=f"{ALICE}@runs.invalid", name=ALICE)
    a = org_store.create_org("Org du journal", created_by=ALICE)
    org_store.add_org_member(a, ALICE, "org_member")
    org_store.set_active_org(ALICE, a)
    return a


@pytest.fixture(scope="module")
def client(live):
    from oto_mcp.api import routes as api_routes
    verifier = _Verifier()
    # Même ordre qu'en production (`server.py`) : le journal est le plus EXTÉRIEUR.
    app = Starlette(routes=api_routes.make_routes(verifier, mcp_instance=None),
                    middleware=[Middleware(api_routes.RestCallLogger),
                                Middleware(api_routes.ViewAsMiddleware,
                                           verifier=verifier)])
    return TestClient(app)


@pytest.fixture
def journal(monkeypatch):
    """Les lignes du journal REST, captées au moment où le middleware les émet — la
    tâche de fond de production pourrait survivre au test. Le banc les insère lui-même
    ensuite, HORS de la boucle (`_ecrire`)."""
    from oto_mcp.api import routes as api_routes
    lignes: list[dict] = []

    def _emet(row):
        lignes.append(dict(row))

        async def _fait():
            return None
        return _fait()

    monkeypatch.setattr(api_routes, "_emit_rest_event", _emet)
    return lignes


def _ecrire(lignes: list[dict]) -> None:
    from oto_mcp import db
    for row in lignes:
        db.insert_tool_call(dict(row))


def _table(org: int) -> str:
    from oto_mcp import db
    ns = "journal-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("org", str(org), ns)
    ns = str(ns_id)
    # `statut` déclarée : une colonne non déclarée est refusée à l'écriture (oto#124).
    db.set_datastore_schema(ns_id, {"fields": [{"key": "statut", "type": "text"}]})
    for i in range(2):
        db.datastore_insert_row(ns_id, f"f{i}", {"statut": "a_appeler"})
    return ns


def _ouvrir(client) -> str:
    r = client.post("/api/me/runs", headers=_h(), json={"label": "journal"})
    assert r.status_code == 201, r.text
    return r.json()["run_id"]


def test_une_ecriture_sous_X_Oto_Run_est_dans_la_timeline_de_son_run(client, org, journal):
    from oto_mcp import db
    ns = _table(org)
    run = _ouvrir(client)

    r = client.post(f"/api/datastores/{ns}/claim_next", headers=_h(run),
                    json={"worker": "scout-1"})
    assert r.status_code == 200, r.text
    row = r.json()["row"]
    w = client.patch(f"/api/datastores/{ns}/rows/{row['_id']}"
                     f"?expected_revision={row['_revision']}",
                     headers=_h(run), json={"statut": "appele"})
    assert w.status_code == 200, w.text

    sous_run = [l for l in journal if l.get("run_id") == run]
    assert [l["tool"].split(" ")[0] for l in sous_run] == ["POST", "PATCH"], journal
    # L'org de la ligne est celle du run, comme au calllog MCP : sans elle, la
    # timeline d'ORG ne verrait rien d'un appel fait sans `X-Oto-Org`.
    assert {l["org_id"] for l in sous_run} == {org}
    _ecrire(journal)
    outils = [c["tool"] for c in db.get_run(run, org_id=org)]
    for l in sous_run:
        assert l["tool"] in outils, outils


def test_sans_en_tete_pas_de_run_meme_juste_apres_une_requete_qui_en_portait(
        client, org, journal):
    """Enchaînées dans la MÊME tâche (transport ASGI) : c'est là qu'un état qui fuit se
    verrait. Le scope est celui de la requête — la suivante ne l'hérite pas."""
    import asyncio

    import httpx
    ns = _table(org)
    run = _ouvrir(client)

    async def _enchainees():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app),
                                     base_url="http://banc") as c:
            avec = await c.post(f"/api/datastores/{ns}/claim_next", headers=_h(run),
                                json={"worker": "scout-1"})
            sans = await c.post(f"/api/datastores/{ns}/claim_next", headers=_h(),
                                json={"worker": "scout-2"})
            return avec, sans

    del journal[:]
    avec, sans = asyncio.run(_enchainees())
    assert avec.status_code == 200 and sans.status_code == 200, (avec.text, sans.text)
    assert [l.get("run_id") for l in journal] == [run, None], journal


def test_le_journal_ne_fait_aucune_requete_pour_connaitre_le_run(monkeypatch):
    """Le run se RELIT dans le scope, publié par l'adaptateur avec ce que son jugement
    avait en main : ni le journal ni la publication n'interrogent la base."""
    import asyncio

    import psycopg

    from oto_mcp.api import routes as api_routes
    from oto_mcp.capabilities import _rest_adapter

    lignes: list[dict] = []

    def _emet(row):
        lignes.append(row)

        async def _fait():
            return None
        return _fait()

    requetes: list = []
    vraie = psycopg.Connection.execute
    monkeypatch.setattr(psycopg.Connection, "execute",
                        lambda self, q, *a, **k: requetes.append(q) or vraie(self, q, *a, **k))
    monkeypatch.setattr(api_routes, "_emit_rest_event", _emet)

    async def _route(scope, receive, send):
        scope[_rest_adapter.CLE_RUN] = {"run_id": "run-banc", "org_id": 7}
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def _recoit():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _envoie(_message):
        return None

    scope = {"type": "http", "method": "POST", "path": "/api/datastores/x/claim_next",
             "headers": [], "query_string": b""}
    asyncio.run(api_routes.RestCallLogger(_route)(scope, _recoit, _envoie))

    assert (lignes[0]["run_id"], lignes[0]["org_id"]) == ("run-banc", 7)
    assert requetes == []
