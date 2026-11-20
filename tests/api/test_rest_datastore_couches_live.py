"""Le geste exact de la flotte, sur les DEUX faces servies (29/08/2026, v1.166.0).

Rejoué par le superviseur à 19:57Z sur `mcp.oto.cx` : `PATCH …/rows/{id}` avec
`{"suivi": {"valeur": "a_traiter", "comment": "…"}}` → 200 ; puis `{"suivi": "a_traiter"}`
(valeur nue IDENTIQUE) → 200 ; relecture : **commentaire perdu**. Puis la même chose par
`data_write(id=…)` (MCP, le chemin réel des agents) à 19:58:05Z : même destruction.

Ce banc rejoue le geste sur la table de routes réelle (`make_routes` + adaptateur) et
sur le tool MCP monté (`register` + `.fn`), contre un vrai PostgreSQL — le seul niveau
qui prouve ce que la face SERT, pas ce que le store fait quand on l'appelle à la main.
Deux tableaux : sans schéma, et le tableau typique de la flotte (`suivi` = statut
énuméré à cycle de vie, format strict).
"""
from __future__ import annotations

import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@couches.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


SUB = "usr_couches"


def _h() -> dict:
    return {"Authorization": f"Bearer {SUB}"}


@pytest.fixture(scope="module")
def live(pg_dsn):
    import os

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_couches_" + uuid.uuid4().hex[:8]
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
        db.upsert_user(SUB, email=f"{SUB}@couches.invalid", name=SUB)
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


SCHEMA_FLOTTE = {
    "key": "siren",
    "fields": [
        {"key": "siren", "type": "text"},
        {"key": "suivi", "type": "enum", "options": ["nouveau", "a_traiter", "traite"],
         "role": "status",
         "lifecycle": {"states": ["nouveau", "a_traiter", "traite"],
                       "transitions": {"nouveau": ["a_traiter"], "a_traiter": ["traite"]},
                       "terminal": ["traite"]}},
        {"key": "note", "type": "text"},
    ],
}


#: Le minimum qu'une écriture exige : les colonnes déclarées, sans type ni cycle de vie.
SCHEMA_COLONNES = {"fields": [{"key": "siren"}, {"key": "suivi"}]}


@pytest.fixture(params=["colonnes_seules", "schema_flotte"])
def table(live, request):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = "t-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", SUB, ns)
    ns = str(ns_id)
    st = make_store(SUB)
    st.set_schema(ns, SCHEMA_FLOTTE if request.param == "schema_flotte"
                  else SCHEMA_COLONNES)
    row = st.append_row(ns, {"siren": "552032534", "suivi": "nouveau"})
    return ns, ns_id, row["_id"]


def _blob(ns_id: int, row_id: str) -> dict:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        r = conn.execute("SELECT data FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
                         (ns_id, row_id)).fetchone()
    return dict((r or {}).get("data") or {})


def test_face_REST_une_valeur_nue_identique_garde_le_comment(client, table):
    ns, ns_id, rid = table
    r1 = client.patch(f"/api/datastores/{ns}/rows/{rid}", headers=_h(),
                      json={"suivi": {"valeur": "a_traiter", "comment": "à rappeler"}})
    assert r1.status_code == 200, r1.text
    lu = client.get(f"/api/datastores/{ns}/rows/{rid}", headers=_h()).json()
    assert lu["suivi"] == "a_traiter" and lu["suivi.comment"] == "à rappeler"
    r2 = client.patch(f"/api/datastores/{ns}/rows/{rid}", headers=_h(),
                      json={"suivi": "a_traiter"})
    assert r2.status_code == 200, r2.text
    relu = client.get(f"/api/datastores/{ns}/rows/{rid}", headers=_h()).json()
    assert relu["suivi"] == "a_traiter"
    assert relu.get("suivi.comment") == "à rappeler", relu
    assert _blob(ns_id, rid)["suivi"] == {"valeur": "a_traiter", "comment": "à rappeler"}


@pytest.mark.asyncio
async def test_face_MCP_data_write_id_une_valeur_nue_identique_garde_le_comment(
        client, table, monkeypatch):
    from fastmcp import FastMCP

    from oto_mcp import access
    from oto_mcp.tools import datastore as tools_ds
    ns, ns_id, rid = table
    monkeypatch.setattr(access, "current_user_sub_from_token", lambda: SUB)
    mcp = FastMCP("test")
    tools_ds.register(mcp)
    fn = (await mcp.get_tool("data_write")).fn
    out1 = fn(datastore=ns, id=rid, row={"suivi": {"valeur": "a_traiter", "comment": "à rappeler"}})
    assert out1["suivi.comment"] == "à rappeler"
    out2 = fn(datastore=ns, id=rid, row={"suivi": "a_traiter"})
    assert out2["suivi"] == "a_traiter"
    assert out2.get("suivi.comment") == "à rappeler", out2
    relu = client.get(f"/api/datastores/{ns}/rows/{rid}", headers=_h()).json()
    assert relu.get("suivi.comment") == "à rappeler", relu
    assert _blob(ns_id, rid)["suivi"] == {"valeur": "a_traiter", "comment": "à rappeler"}
