"""Run rattaché au projet actif, gelé à l'ouverture (ADR 0032 §5/§6, B3.1).

`_persist_open` (tools/doctrine_run) lit `access.current_project()` au start et le
passe à `db.insert_run` — exactement comme `org_id`. On monkeypatche les seams
(sub, current_org, current_project, insert_run), pas de DB.
"""
import asyncio

import oto_mcp.access as access
import oto_mcp.auth_hooks as ah
import oto_mcp.db as db
from oto_mcp.tools import doctrine_run as drt


def _wire(monkeypatch, *, sub, org, project, rec):
    monkeypatch.setattr(ah, "current_user_sub_from_token", lambda: sub)
    monkeypatch.setattr(access, "current_org", lambda s: org)
    monkeypatch.setattr(access, "current_project", lambda: project)
    monkeypatch.setattr(db, "insert_run",
                        lambda run_id, **kw: rec.update(kw, run_id=run_id))


def test_persist_open_freezes_active_project(monkeypatch):
    rec = {}
    _wire(monkeypatch, sub="u1", org=3, project=7, rec=rec)
    asyncio.run(drt._persist_open("r1", "prospection Q3", "prospection"))
    assert rec["run_id"] == "r1"
    assert rec["org_id"] == 3 and rec["project_id"] == 7 and rec["doctrine"] == "prospection"


def test_persist_open_no_project(monkeypatch):
    rec = {}
    _wire(monkeypatch, sub="u1", org=3, project=None, rec=rec)
    asyncio.run(drt._persist_open("r2", "run ad-hoc", None))
    assert rec["project_id"] is None and rec["org_id"] == 3


def test_persist_open_no_sub_no_scope(monkeypatch):
    rec = {}
    _wire(monkeypatch, sub=None, org=3, project=7, rec=rec)   # stdio local
    asyncio.run(drt._persist_open("r3", "local", None))
    assert rec["org_id"] is None and rec["project_id"] is None
