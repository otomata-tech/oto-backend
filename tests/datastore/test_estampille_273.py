"""L'ESTAMPILLE du journal des révisions de ligne, jalon M2 (oto#273).

Chaque révision dit qui a écrit (`acteur`), par quelle face (`source`), dans quel geste
(`geste_id`) et sous quel run (`run_id`). Tout se juge sur ce que porte la BASE, par
une connexion fraîche, après être passé par le VRAI chemin de chaque face :

1. **la garde** — aucune requête du paquet qui écrit `datastore_rows.data`, ni qui
   supprime une ligne, n'échappe au point de passage `db.estampille.ecriture_de_lignes` ;
2. **sans contexte** — `system`, acteur NULL, un geste quand même ;
3. **MCP** — `agent`, le sub, le run de `_run_id`, et le geste = `call_uid` de la ligne
   `tool_calls` ; un lot partage UN geste, deux appels en ont deux ; un import
   (`donnees_d_origine=true`) porte `import` ;
4. **REST** — session (JWT) = `console`, jeton `oto_` = `api`, `X-Oto-Run` = le run,
   et le geste est versé à la ligne `tool_calls` de la requête ;
5. **upload signé** — `upload`, le compte scellé au jeton, le `jti` pour geste ;
   `import` seulement si le jeton a scellé `donnees_d_origine=true` ;
6. **interne** — `system`, `service:<nom>`.
"""
from __future__ import annotations

import ast
import json
import pathlib
import re
import uuid

import pytest

SUB = "sub-estampille-273"


def _sql(requete: str, *params):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        cur = conn.execute(requete, params or None)
        return cur.fetchall() if cur.description else None


@pytest.fixture(scope="module")
def compte(live):
    from oto_mcp import db
    db.upsert_user(SUB, email=f"{SUB}@estampille.invalid", name=SUB)
    return SUB


def _table(nom: str = "estampille") -> tuple[str, int]:
    from oto_mcp import db
    ns_id = db.create_datastore("user", SUB, f"{nom}-{uuid.uuid4().hex[:6]}")
    ns = str(ns_id)
    # `n` déclarée : une colonne non déclarée est refusée à l'écriture (oto#124).
    db.set_datastore_schema(ns_id, {"fields": [{"key": "n", "type": "number"}]})
    return ns, ns_id


def _revisions(ns_id: int) -> list[dict]:
    return _sql("SELECT row_id, rev, acteur, run_id, source, geste_id "
                "FROM datastore_row_revisions WHERE ns_id = %s ORDER BY id", ns_id)


def _run() -> str:
    from oto_mcp import db
    run_id = uuid.uuid4().hex
    db.insert_run(run_id, sub=SUB, org_id=None, label="t-273")
    return run_id


# ── 1. la garde ────────────────────────────────────────────────────────────────

_ECRIT_DATA = re.compile(
    r"INSERT\s+INTO\s+datastore_rows\b"
    r"|DELETE\s+FROM\s+datastore_rows\b"
    r"|UPDATE\s+datastore_rows\s+SET\b(?:(?!\bWHERE\b)[\s\S])*?\bdata\s*=",
    re.IGNORECASE)

#: Les fonctions qui écrivent `data` aujourd'hui. Une nouvelle apparaît ici par un
#: échec, pas en silence : elle doit passer par le point de passage, et se nommer.
_ECRIVAINS = {
    "oto_mcp/db/datastore.py": {
        "datastore_insert_row",
        "datastore_drop_column", "datastore_merge_key_duplicates",
        "datastore_merge_row_locked", "datastore_delete_row"},
    "oto_mcp/db/rowabandon.py": {"abandonner_les_lignes_a_bout"},
    # oto#95 : le relâchement AVANCE une ligne que sa passe a écrite.
    "oto_mcp/db/rowlock.py": {"_relacher"},
}


def _appelle(noeud: ast.AST, nom: str) -> bool:
    for n in ast.walk(noeud):
        if isinstance(n, ast.Call):
            f = n.func
            if (isinstance(f, ast.Name) and f.id == nom) or \
                    (isinstance(f, ast.Attribute) and f.attr == nom):
                return True
    return False


def test_aucune_ecriture_de_data_n_echappe_au_point_de_passage():
    racine = pathlib.Path(__file__).resolve().parents[2]
    trouves: dict = {}
    for chemin in sorted((racine / "oto_mcp").rglob("*.py")):
        rel = chemin.relative_to(racine).as_posix()
        arbre = ast.parse(chemin.read_text(encoding="utf-8"))
        for fn in ast.walk(arbre):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            ecrit = any(isinstance(c, ast.Constant) and isinstance(c.value, str)
                        and _ECRIT_DATA.search(c.value)
                        for c in ast.walk(fn))
            if ecrit:
                trouves.setdefault(rel, set()).add(fn.name)
                assert _appelle(fn, "ecriture_de_lignes"), (
                    f"{rel}::{fn.name} écrit `datastore_rows.data` sans passer par "
                    "`db.estampille.ecriture_de_lignes` : ses révisions n'auraient "
                    "ni acteur, ni source, ni geste.")
    assert trouves == _ECRIVAINS, trouves


def test_le_principal_est_lu_sous_le_nom_ou_l_authentification_le_depose():
    from oto_mcp.api import base
    from oto_mcp.capabilities import _rest_adapter
    assert _rest_adapter._CLE_PRINCIPAL == base.CLE_PRINCIPAL


# ── 2. sans contexte, et le travail de fond ───────────────────────────────────

def test_sans_contexte_system_acteur_null_et_un_geste(compte):
    from oto_mcp import db
    _, ns_id = _table()
    db.datastore_insert_row(ns_id, "r1", {"a": 1})
    db.datastore_insert_row(ns_id, "r2", {"a": 2})
    r1, r2 = _revisions(ns_id)
    assert (r1["source"], r1["acteur"], r1["run_id"]) == ("system", None, None)
    assert r1["geste_id"] and r2["geste_id"] and r1["geste_id"] != r2["geste_id"], \
        "sans contexte, chaque transaction est son propre geste"


def test_le_travail_de_fond_se_nomme(compte):
    from oto_mcp import db, geste
    _, ns_id = _table()
    with geste.interne("maintenance") as g:
        db.datastore_insert_row(ns_id, "r1", {"a": 1})
        db.datastore_merge_row_locked(ns_id, "r1", lambda _d: {"a": 2},
                                      "2026-10-05T00:00:00+00:00")
    revs = _revisions(ns_id)
    assert [(r["source"], r["acteur"], r["geste_id"]) for r in revs] == \
        [("system", "service:maintenance", g.geste_id)] * 2


def test_l_estampille_ne_survit_pas_a_sa_transaction(compte):
    """`set_config(…, true)` : la connexion rendue au pool ne garde rien. Une écriture
    faite ensuite HORS du point de passage, sur la même connexion, porte NULL."""
    from oto_mcp import db, geste
    from oto_mcp.db._conn import _connect
    _, ns_id = _table()
    with geste.portee(geste.CONSOLE, SUB):
        db.datastore_insert_row(ns_id, "r1", {"a": 1})
    with _connect() as conn:
        conn.execute("UPDATE datastore_rows SET data = '{\"a\": 2}'::jsonb "
                     "WHERE ns_id = %s", (ns_id,))
    avant, apres = _revisions(ns_id)
    assert avant["source"] == "console"
    assert (apres["source"], apres["acteur"], apres["geste_id"]) == (None, None, None)


# ── 3. la face MCP ─────────────────────────────────────────────────────────────

@pytest.fixture
def mcp(compte, monkeypatch):
    """`data_write` tel que le boot le monte, derrière la chaîne servie : le contexte
    d'appel (`_run_id=`), puis le journal des appels, qui frappe `call_uid`."""
    from fastmcp import FastMCP

    from oto_mcp import call_axes
    from oto_mcp.calllog import ToolCallLogger
    from oto_mcp.datastore.core import make_store
    from oto_mcp.middleware.call_context import CallContextMiddleware
    from oto_mcp.tools import datastore as T
    from oto_mcp.tools import register_all

    monkeypatch.setattr(T, "_acting_store", lambda: make_store(SUB))
    monkeypatch.setattr(T, "_ns", lambda ns: ns)
    monkeypatch.setattr(T, "_project_hint", lambda ns: None)
    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: SUB)
    appels: list = []

    async def sink(row):
        if row.get("kind") != "protocol":
            appels.append(row)

    async def identite():
        return {"sub": SUB}

    m = FastMCP("t-273")
    register_all(m)
    m.add_middleware(CallContextMiddleware(frozenset()))
    m.add_middleware(ToolCallLogger(sink, server="t", identity=identite))
    return m, appels


async def _appeler(m, arguments: dict):
    import asyncio

    from fastmcp import Client

    from oto_mcp import calllog
    async with Client(m) as c:
        await c.call_tool("data_write", arguments)
    while calllog._PENDING:
        await asyncio.gather(*list(calllog._PENDING), return_exceptions=True)


@pytest.mark.asyncio
async def test_face_mcp_agent_sub_run_et_geste_du_journal_des_appels(mcp):
    m, appels = mcp
    ns, ns_id = _table()
    run = _run()
    await _appeler(m, {"datastore": ns, "rows": [{"n": 1}, {"n": 2}, {"n": 3}],
                       "_run_id": run})
    await _appeler(m, {"datastore": ns, "row": {"n": 4}})
    revs = _revisions(ns_id)
    assert len(revs) == 4
    assert {(r["source"], r["acteur"]) for r in revs} == {("agent", SUB)}
    lot, seul = revs[:3], revs[3]
    assert {r["run_id"] for r in lot} == {run}
    assert seul["run_id"] is None
    assert len({r["geste_id"] for r in lot}) == 1, "un lot = UN geste"
    assert seul["geste_id"] != lot[0]["geste_id"], "deux appels = deux gestes"
    assert [a["call_uid"] for a in appels] == [lot[0]["geste_id"], seul["geste_id"]], \
        "le geste EST la ligne `tool_calls` de l'appel"


@pytest.mark.asyncio
async def test_face_mcp_donnees_d_origine_est_un_import(mcp):
    m, appels = mcp
    ns, ns_id = _table()
    await _appeler(m, {"datastore": ns, "rows": [{"n": 1}, {"n": 2}],
                       "donnees_d_origine": True})
    await _appeler(m, {"datastore": ns, "row": {"n": 3}, "donnees_d_origine": True})
    revs = _revisions(ns_id)
    assert {(r["source"], r["acteur"]) for r in revs} == {("import", SUB)}
    assert [r["geste_id"] for r in revs] == [appels[0]["call_uid"]] * 2 + \
        [appels[1]["call_uid"]]


# ── 4. la face REST ────────────────────────────────────────────────────────────

class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@estampille.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


@pytest.fixture(scope="module")
def rest(compte):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from oto_mcp.api import routes as api_routes
    app = Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None))
    return TestClient(api_routes.RestCallLogger(app))


def test_face_rest_session_console_jeton_api_et_run(rest, monkeypatch):
    from oto_mcp import db
    from oto_mcp.api import routes as api_routes
    lignes: list = []
    monkeypatch.setattr(api_routes.db, "insert_tool_call", lignes.append)
    ns, ns_id = _table()
    run = _run()
    jeton = db.create_api_token(SUB, label="t-273")

    r = rest.post(f"/api/datastores/{ns}/rows/batch",
                  headers={"Authorization": f"Bearer {SUB}", "X-Oto-Run": run},
                  json={"rows": [{"n": 1}, {"n": 2}]})
    assert r.status_code == 200, r.text
    r = rest.post(f"/api/datastores/{ns}/rows",
                  headers={"Authorization": f"Bearer {jeton}"}, json={"n": 3})
    assert r.status_code == 201, r.text

    revs = _revisions(ns_id)
    assert [(r["source"], r["acteur"], r["run_id"]) for r in revs] == [
        ("console", SUB, run), ("console", SUB, run), ("api", SUB, None)]
    assert revs[0]["geste_id"] == revs[1]["geste_id"] != revs[2]["geste_id"]
    ecritures = [l_ for l_ in lignes if l_["tool"].startswith("POST")]
    assert [l_["call_uid"] for l_ in ecritures] == \
        [revs[0]["geste_id"], revs[2]["geste_id"]], \
        "le geste est versé à la ligne `tool_calls` de la requête"


def test_face_rest_donnees_d_origine_est_un_import(rest):
    ns, ns_id = _table()
    r = rest.post(f"/api/datastores/{ns}/rows/batch",
                  headers={"Authorization": f"Bearer {SUB}"},
                  json={"rows": [{"n": 1}], "donnees_d_origine": True})
    assert r.status_code == 200, r.text
    (rev,) = _revisions(ns_id)
    assert (rev["source"], rev["acteur"]) == ("import", SUB)


# ── 5. l'upload signé ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("declare, source", [(False, "upload"), (True, "import")])
def test_upload_signe_compte_scelle_et_jti(rest, monkeypatch, declare, source):
    """Un fichier n'est pas une origine parce qu'il est un fichier : `import` seulement
    quand le mint a scellé `donnees_d_origine=true`, sinon `upload`."""
    from oto_mcp import upload_tokens
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "s" * 32)
    ns, ns_id = _table()
    jeton, _ = upload_tokens.sign(SUB, None, {"kind": "datastore", "ns_id": ns_id,
                                              "namespace": ns, "format": "ndjson",
                                              "donnees_d_origine": declare})
    corps = "\n".join(json.dumps({"n": i}) for i in range(3)).encode()
    r = rest.put(f"/api/upload/{jeton}", content=corps,
                 headers={"Content-Type": "application/x-ndjson"})
    assert r.status_code == 200, r.text
    revs = _revisions(ns_id)
    jti = upload_tokens.verify(jeton)["jti"]
    assert [(r["source"], r["acteur"], r["geste_id"]) for r in revs] == \
        [(source, SUB, jti)] * 3
