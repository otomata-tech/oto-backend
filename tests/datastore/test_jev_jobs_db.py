"""`jev_rows(background=true)` on a real store: one call queues the job, the worker
judges the whole table slice by slice under the caller, bills every slice in the call
ledger, and stops when the key is refused. Jev itself is mocked."""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

SUB = "sub-jev-jobs"
AUTRE = "sub-jev-jobs-autre"
SCHEMA = {"fields": [
    {"key": "company", "type": "text"},
    {"key": "q_fit", "type": "number"}, {"key": "q_fit_p", "type": "number"},
    {"key": "q_model", "type": "text"}]}
QS = {"fit": {"type": "score", "instructions": "fit?", "criteria": ["no", "yes"]}}
ANSWER = {"model": "typesafe/jev-1.13-20260917",
          "answers": {"fit": {"type": "score", "score": 0.7, "confidence": 0.9}},
          "usage": {"input_tokens": 300, "cost": 1.2e-05}}


def _sql(requete: str, *params):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        cur = conn.execute(requete, params or None)
        return cur.fetchall() if cur.description else None


@pytest.fixture(scope="module")
def compte(live):
    from oto_mcp import db
    for s in (SUB, AUTRE):
        db.upsert_user(s, email=f"{s}@jev.invalid", name=s)
    return SUB


class _Rung:
    mode, key, is_platform = "tenant", "sk-or-test", False


@pytest.fixture
def monte(compte, monkeypatch):
    """The tools behind the served chain; the key resolves as the tenant's (and says so
    on the call trace, like the real cascade). `etat["qui"]` = the caller."""
    from fastmcp import FastMCP

    from oto_mcp import access, call_axes, session_org
    from oto_mcp.calllog import ToolCallLogger
    from oto_mcp.middleware.call_context import CallContextMiddleware
    from oto_mcp.tools import jev
    from oto_mcp.tools import jev_rows as jr

    etat = {"qui": SUB, "refus": None, "resolu_pour": []}

    def _resoudre(*a, **kw):
        etat["resolu_pour"].append(access.current_user_sub_or_raise())
        if etat["refus"] is not None:
            raise etat["refus"]
        session_org.note_call_trace(key_mode="tenant")
        return _Rung()

    reel = access.current_user_sub_or_raise
    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: etat["qui"])
    monkeypatch.setattr(access, "current_user_sub_or_raise",
                        lambda: etat["qui"] or reel())
    monkeypatch.setattr(access, "resolve_credential", _resoudre)
    # Two rows per slice: a 5-row table takes three slices.
    monkeypatch.setattr(jr, "MAX_ROWS", 2)

    async def sink(row):
        pass

    async def identite():
        return {"sub": etat["qui"]}

    with patch("oto.tools.jev.client.JevClient") as cls:
        cls.return_value.decide.return_value = ANSWER
        m = FastMCP("t-jev-jobs")
        jev.register(m)
        m.add_middleware(CallContextMiddleware())
        m.add_middleware(ToolCallLogger(sink, server="t", identity=identite))
        yield m, cls.return_value, etat


def _table(n: int) -> tuple[str, int, list[str]]:
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns_id = db.create_datastore("user", SUB, f"jev-jobs-{uuid.uuid4().hex[:6]}")
    ns = str(ns_id)
    store = make_store(SUB)
    store.set_schema(ns, SCHEMA)
    ids = [store.append_row(ns, {"company": f"Co {i}"})["_id"] for i in range(n)]
    return ns, ns_id, ids


def _run() -> str:
    from oto_mcp import db
    run_id = uuid.uuid4().hex
    db.insert_run(run_id, sub=SUB, org_id=None, label="t-jev-jobs")
    return run_id


async def _appeler(m, nom: str, arguments: dict, *, erreur: bool = False):
    from fastmcp import Client
    async with Client(m) as c:
        r = await c.call_tool(nom, arguments, raise_on_error=not erreur)
    if erreur:
        assert r.is_error
        return r.content[0].text
    return r.structured_content or r.data


def _args(ns: str, **kw) -> dict:
    return {"datastore": ns, "questions": QS, "state_fields": ["company"],
            "output": {"fit": "q_fit"}, "model_column": "q_model",
            "background": True, **kw}


def _vider() -> int:
    """Run the worker until the queue is empty; the number of slices run."""
    from oto_mcp import jev_jobs_worker
    n = 0
    while jev_jobs_worker._un_tour():
        n += 1
        assert n < 50, "the job never ends"
    return n


def _factures(job_id: int) -> list[dict]:
    return _sql("SELECT kind, tool, ok, quantity, key_mode, run_id, call_uid, sub "
                "FROM tool_calls WHERE tool = 'jev_rows' AND args->>'jev_job_id' = %s "
                "ORDER BY id", str(job_id))


@pytest.mark.asyncio
async def test_one_call_qualifies_the_whole_table_and_bills_each_slice(monte):
    m, client, _ = monte
    ns, ns_id, _ = _table(5)
    run = _run()
    r = await _appeler(m, "jev_rows", _args(ns, _run_id=run))
    assert r["status"] == "pending" and r["remaining"] == 5
    assert client.decide.call_count == 0, "nothing is judged in the call itself"

    slices = _vider()
    assert slices >= 3

    st = await _appeler(m, "jev_job", {"op": "status", "job_id": r["job_id"]})
    assert st["status"] == "done" and st["decided"] == 5 and st["remaining"] == 0
    assert client.decide.call_count == 5, "each row judged once"
    assert st["cost"] == pytest.approx(5 * 1.2e-05)

    rows = _sql("SELECT data FROM datastore_rows WHERE ns_id = %s", ns_id)
    assert all(x["data"]["q_fit"] == 0.7 and x["data"]["q_model"] == ANSWER["model"]
               for x in rows)
    # Billed like calls: kind mcp, the tenant's key, the job's run, µ$ of real cost.
    factures = _factures(r["job_id"])
    payees = [f for f in factures if f["quantity"]]
    assert {(f["kind"], f["ok"], f["key_mode"], f["run_id"], f["sub"])
            for f in payees} == {("mcp", True, "tenant", run, SUB)}
    assert sum(f["quantity"] for f in payees) == 5 * 12
    # Each write is stamped with the slice that made it, under the caller.
    revs = _sql("SELECT acteur, run_id, source, geste_id FROM datastore_row_revisions "
                "WHERE ns_id = %s AND diff ? %s", ns_id, "q_fit")
    assert {(x["source"], x["acteur"], x["run_id"]) for x in revs} == {("agent", SUB, run)}
    assert {x["geste_id"] for x in revs} <= {f["call_uid"] for f in factures}


@pytest.mark.asyncio
async def test_a_refused_key_stops_the_job_and_keeps_what_was_judged(monte):
    from mcp.types import ErrorData, INVALID_PARAMS

    from oto_mcp import jev_jobs_worker
    from oto_mcp.access import CredentialUnavailable
    from oto_mcp.db import jev_jobs as db_jev
    m, client, etat = monte
    ns, ns_id, _ = _table(5)
    r = await _appeler(m, "jev_rows", _args(ns))
    assert jev_jobs_worker._un_tour() == 1          # first slice: 2 rows
    etat["refus"] = CredentialUnavailable(ErrorData(code=INVALID_PARAMS,
                                                    message="No `jev` key for your org"))
    _vider()
    job = db_jev.get_jev_job(r["job_id"])
    assert job["status"] == "failed" and "No `jev` key" in job["error"]
    assert client.decide.call_count == 2, "nothing sent once the key is refused"
    decided = _sql("SELECT count(*) AS n FROM datastore_rows WHERE ns_id = %s "
                   "AND data->>'q_model' IS NOT NULL", ns_id)[0]["n"]
    assert decided == 2


@pytest.mark.asyncio
async def test_one_active_job_per_table_and_column(monte):
    m, _, _ = monte
    ns, _, _ = _table(3)
    r = await _appeler(m, "jev_rows", _args(ns))
    msg = await _appeler(m, "jev_rows", _args(ns), erreur=True)
    assert f"Job {r['job_id']} is already running" in msg
    await _appeler(m, "jev_job", {"op": "cancel", "job_id": r["job_id"]})
    r2 = await _appeler(m, "jev_rows", _args(ns))
    assert r2["job_id"] != r["job_id"]
    _vider()


@pytest.mark.asyncio
async def test_cancel_stops_a_job_before_its_next_slice(monte):
    from oto_mcp import jev_jobs_worker
    m, client, _ = monte
    ns, _, _ = _table(5)
    r = await _appeler(m, "jev_rows", _args(ns))
    assert jev_jobs_worker._un_tour() == 1
    st = await _appeler(m, "jev_job", {"op": "cancel", "job_id": r["job_id"]})
    assert st["status"] == "cancelled" and st["decided"] == 2
    assert _vider() == 0 and client.decide.call_count == 2


@pytest.mark.asyncio
async def test_overwrite_resumes_from_its_cursor_and_never_judges_twice(monte):
    m, client, _ = monte
    ns, _, _ = _table(5)
    await _appeler(m, "jev_rows", _args(ns))
    _vider()
    n = client.decide.call_count
    r = await _appeler(m, "jev_rows", _args(ns, overwrite=True))
    _vider()
    st = await _appeler(m, "jev_job", {"op": "status", "job_id": r["job_id"]})
    assert st["status"] == "done" and st["decided"] == 5
    assert client.decide.call_count - n == 5


@pytest.mark.asyncio
async def test_another_account_sees_no_job(monte):
    m, _, etat = monte
    ns, _, _ = _table(1)
    r = await _appeler(m, "jev_rows", _args(ns))
    etat["qui"] = AUTRE
    msg = await _appeler(m, "jev_job", {"op": "status", "job_id": r["job_id"]},
                         erreur=True)
    manquant = await _appeler(m, "jev_job", {"op": "status", "job_id": 10 ** 9},
                              erreur=True)
    assert msg.replace(str(r["job_id"]), "N") == manquant.replace(str(10 ** 9), "N")
    etat["qui"] = SUB
    _vider()


@pytest.mark.asyncio
async def test_background_refuses_a_dry_run_and_a_cursor(monte):
    m, client, _ = monte
    ns, _, _ = _table(1)
    assert "don't go together" in await _appeler(m, "jev_rows", _args(ns, dry_run=True),
                                                 erreur=True)
    assert "takes no `cursor`" in await _appeler(m, "jev_rows", _args(ns, cursor="x"),
                                                 erreur=True)
    assert client.decide.call_count == 0


def test_the_worker_acts_on_a_third_party():
    from oto_mcp import boucles_de_fond
    assert next(b for b in boucles_de_fond.BOUCLES if b.nom == "jev_jobs_worker").tiers


@pytest.mark.asyncio
async def test_the_worker_acts_as_the_caller_without_any_request(monte):
    """No token in the worker: the identity is the job's, set by `sub_override`."""
    m, _, etat = monte
    ns, ns_id, _ = _table(3)
    r = await _appeler(m, "jev_rows", _args(ns))
    etat["qui"], etat["resolu_pour"] = None, []
    try:
        _vider()
    finally:
        etat["qui"] = SUB
    assert etat["resolu_pour"] and set(etat["resolu_pour"]) == {SUB}
    from oto_mcp.db import jev_jobs as db_jev
    assert db_jev.get_jev_job(r["job_id"])["status"] == "done"
    acteurs = _sql("SELECT DISTINCT acteur FROM datastore_row_revisions "
                   "WHERE ns_id = %s AND diff ? %s", ns_id, "q_fit")
    assert [x["acteur"] for x in acteurs] == [SUB]


@pytest.mark.asyncio
@pytest.mark.parametrize("overwrite", [False, True])
async def test_a_row_that_keeps_failing_upstream_never_rebills_the_others(monte, overwrite):
    from oto.tools.common.errors import UpstreamHTTPError
    m, client, _ = monte
    ns, _, _ = _table(5)
    if overwrite:
        await _appeler(m, "jev_rows", _args(ns))
        _vider()

    def decide(state, questions, **kw):
        if state["company"] == "Co 1":
            raise UpstreamHTTPError(500, "boom")
        return ANSWER

    client.decide.side_effect = decide
    client.decide.reset_mock()
    r = await _appeler(m, "jev_rows", _args(ns, overwrite=overwrite))
    _vider()
    st = await _appeler(m, "jev_job", {"op": "status", "job_id": r["job_id"]})
    assert st["status"] == "done" and st["decided"] == 4
    sent = [c.args[0]["company"] for c in client.decide.call_args_list]
    assert sorted(x for x in sent if x != "Co 1") == ["Co 0", "Co 2", "Co 3", "Co 4"]
    # Retried once by the last pass without `overwrite`; never with it.
    assert sent.count("Co 1") == (1 if overwrite else 2)
    assert {e["code"] for e in st["errors"]} == {"upstream_500"}


@pytest.mark.asyncio
async def test_a_closed_run_does_not_stop_its_job(monte):
    from oto_mcp import db
    m, _, _ = monte
    ns, _, _ = _table(3)
    run = _run()
    r = await _appeler(m, "jev_rows", _args(ns, _run_id=run))
    db.finish_run(run, "done")
    _vider()
    st = await _appeler(m, "jev_job", {"op": "status", "job_id": r["job_id"]})
    assert st["status"] == "done" and st["decided"] == 3
