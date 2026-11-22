"""`jev_rows` on a real store: writes stamped like any agent write (actor, run, call uid),
leased rows skipped, a re-run judges nothing. Jev itself is mocked."""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import patch

import pytest

SUB = "sub-jev-rows"
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
    db.upsert_user(SUB, email=f"{SUB}@jev.invalid", name=SUB)
    return SUB


class _Rung:
    mode, key, is_platform = "tenant", "sk-or-test", False


@pytest.fixture
def monte(compte, monkeypatch):
    """`jev_rows` behind the served chain: call context (`_run_id=`), then the call log."""
    from fastmcp import FastMCP

    from oto_mcp import access, call_axes
    from oto_mcp.calllog import ToolCallLogger
    from oto_mcp.middleware.call_context import CallContextMiddleware
    from oto_mcp.tools import jev

    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: SUB)
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: SUB)
    monkeypatch.setattr(access, "resolve_credential", lambda *a, **kw: _Rung())
    appels: list = []

    async def sink(row):
        if row.get("kind") != "protocol":
            appels.append(row)

    async def identite():
        return {"sub": SUB}

    with patch("oto.tools.jev.client.JevClient") as cls:
        cls.return_value.decide.return_value = ANSWER
        m = FastMCP("t-jev-rows")
        jev.register(m)
        m.add_middleware(CallContextMiddleware())
        m.add_middleware(ToolCallLogger(sink, server="t", identity=identite))
        yield m, appels, cls.return_value


def _table(n: int) -> tuple[str, int, list[str]]:
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns_id = db.create_datastore("user", SUB, f"jev-rows-{uuid.uuid4().hex[:6]}")
    ns = str(ns_id)
    store = make_store(SUB)
    store.set_schema(ns, SCHEMA)
    ids = [store.append_row(ns, {"company": f"Co {i}"})["_id"] for i in range(n)]
    return ns, ns_id, ids


def _run() -> str:
    from oto_mcp import db
    run_id = uuid.uuid4().hex
    db.insert_run(run_id, sub=SUB, org_id=None, label="t-jev-rows")
    return run_id


async def _appeler(m, arguments: dict) -> dict:
    from fastmcp import Client

    from oto_mcp import calllog
    async with Client(m) as c:
        r = await c.call_tool("jev_rows", arguments)
    while calllog._PENDING:
        await asyncio.gather(*list(calllog._PENDING), return_exceptions=True)
    return r.structured_content or r.data


def _args(ns: str, **kw) -> dict:
    return {"datastore": ns, "questions": QS, "state_fields": ["company"],
            "output": {"fit": "q_fit"}, "model_column": "q_model", **kw}


@pytest.mark.asyncio
async def test_writes_are_stamped_like_any_agent_write(monte):
    m, appels, _ = monte
    ns, ns_id, ids = _table(3)
    run = _run()
    r = await _appeler(m, _args(ns, _run_id=run))
    assert r["decided"] == 3 and r["done"] is True
    revs = _sql("SELECT row_id, acteur, run_id, source, geste_id FROM "
                "datastore_row_revisions WHERE ns_id = %s AND diff ? %s ORDER BY id",
                ns_id, "q_fit")
    assert len(revs) == 3
    assert {(x["source"], x["acteur"], x["run_id"]) for x in revs} == {("agent", SUB, run)}
    uids = [a["call_uid"] for a in appels if a.get("tool") == "jev_rows"]
    assert {x["geste_id"] for x in revs} == set(uids), "the write is THIS call's gesture"
    rows = _sql("SELECT data FROM datastore_rows WHERE ns_id = %s", ns_id)
    assert all(x["data"]["q_fit"] == 0.7 and x["data"]["q_fit_p"] == 0.9
               and x["data"]["q_model"] == ANSWER["model"] for x in rows)


@pytest.mark.asyncio
async def test_leased_row_is_skipped_and_a_rerun_judges_nothing(monte):
    from oto_mcp.datastore.core import make_store
    m, _, client = monte
    ns, ns_id, ids = _table(3)
    make_store(SUB).claim_row(ns, ids[1], worker="someone-else")
    r = await _appeler(m, _args(ns))
    assert r["decided"] == 2 and r["skipped_leased"] == 1 and r["remaining"] == 1
    assert r["done"] is False
    sent = [c.args[0]["company"] for c in client.decide.call_args_list]
    assert "Co 1" not in sent
    n = client.decide.call_count
    r = await _appeler(m, _args(ns))
    assert r["decided"] == 0 and client.decide.call_count == n


@pytest.mark.asyncio
async def test_missing_columns_are_created_on_a_real_schema(monte):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    m, _, _ = monte
    ns = str(db.create_datastore("user", SUB, f"jev-rows-{uuid.uuid4().hex[:6]}"))
    store = make_store(SUB)
    store.set_schema(ns, {"fields": [{"key": "company", "type": "text"}]})
    store.append_row(ns, {"company": "Co"})
    r = await _appeler(m, _args(ns))
    assert r["created_columns"] == ["q_fit", "q_fit_p", "q_model"] and r["decided"] == 1
    keys = {f["key"]: f["type"] for f in store.get_schema(ns)["fields"]}
    assert keys == {"company": "text", "q_fit": "number", "q_fit_p": "number",
                    "q_model": "text"}
    r = await _appeler(m, _args(ns))
    assert r["created_columns"] == [] and r["decided"] == 0
