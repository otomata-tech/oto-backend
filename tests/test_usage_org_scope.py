"""Anti-fuite cross-org de l'activité « mes appels » (page dashboard /activity).

`list_tool_calls(org_id=…)` doit ajouter un filtre `l.org_id = %s` — sinon un user
voit ses appels de TOUTES ses orgs sous l'org chargée (même classe que e030f5c, mais
sur tool_calls). On mocke `_connect` pour capturer le SQL/params sans vraie DB.
"""
from oto_mcp.db import usage


class _FakeCur:
    def __init__(self, sink):
        self._sink = sink

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, sink):
        self._sink = sink

    def execute(self, sql, params):
        self._sink["sql"] = sql
        self._sink["params"] = params
        return _FakeCur(self._sink)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wire(monkeypatch):
    sink: dict = {}
    monkeypatch.setattr(usage, "_connect", lambda: _FakeConn(sink))
    return sink


def test_org_id_adds_scope_clause(monkeypatch):
    sink = _wire(monkeypatch)
    usage.list_tool_calls(sub="u1", org_id=35, limit=50)
    assert "l.org_id = %s" in sink["sql"]
    # sub puis org_id puis limit (ordre d'append des clauses)
    assert sink["params"][0] == "u1"
    assert 35 in sink["params"]


def test_without_org_id_no_scope_clause(monkeypatch):
    sink = _wire(monkeypatch)
    usage.list_tool_calls(sub="u1", limit=50)
    assert "l.org_id" not in sink["sql"]        # rétro-compat : admin monitoring non scopé
    assert tuple(sink["params"]) == ("u1", 50)
