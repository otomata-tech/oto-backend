"""Agrégat serveur `data_aggregate` (feedback #191). Le store convertit le `filter`
plat en clauses SQL et délègue à `db.datastore_aggregate` (seam PG monkeypatché —
la construction SQL réelle est validée en preprod)."""
from __future__ import annotations

from oto_mcp import datastore as D


def test_aggregate_delegates_and_converts_filter(monkeypatch):
    seen = {}

    def _fake_agg(ns_id, *, group_by=None, metrics=None, q=None, filters=None, limit=1000):
        seen.update(ns_id=ns_id, group_by=group_by, metrics=metrics, filters=filters)
        return [{"departement": "69", "sum_kwc": 1200.0, "count": 3}]

    monkeypatch.setattr(D.db, "datastore_aggregate", _fake_agg)
    s = D.DatastorePg("u1")
    monkeypatch.setattr(s, "_resolve", lambda ns, write=False: 7)

    out = s.aggregate(
        "vivier",
        group_by="departement",
        metrics=[{"op": "sum", "field": "kwc"}, {"op": "count"}],
        filter={"statut": "qualified"},
    )
    assert out == [{"departement": "69", "sum_kwc": 1200.0, "count": 3}]
    assert seen["ns_id"] == 7
    assert seen["group_by"] == "departement"
    assert seen["metrics"] == [{"op": "sum", "field": "kwc"}, {"op": "count"}]
    assert seen["filters"] == [{"field": "statut", "op": "eq", "value": "qualified"}]


def test_aggregate_default_metrics_none_passthrough(monkeypatch):
    seen = {}

    def _fake_agg(ns_id, *, group_by=None, metrics=None, q=None, filters=None, limit=1000):
        seen["metrics"] = metrics
        return [{"count": 301}]

    monkeypatch.setattr(D.db, "datastore_aggregate", _fake_agg)
    s = D.DatastorePg("u1")
    monkeypatch.setattr(s, "_resolve", lambda ns, write=False: 1)

    assert s.aggregate("vivier") == [{"count": 301}]
    # metrics non fourni → le défaut ([{op:count}]) est appliqué côté db, pas ici.
    assert seen["metrics"] is None


def test_aggregate_combines_exact_filter_and_rich_filters(monkeypatch):
    """`filter` exact (MCP) + `q`/`filters` riches (dashboard, mêmes clauses que
    /rows) se CUMULENT — les tuiles metric agrègent le jeu filtré affiché."""
    seen = {}

    def _fake_agg(ns_id, *, group_by=None, metrics=None, q=None, filters=None, limit=1000):
        seen.update(q=q, filters=filters)
        return []

    monkeypatch.setattr(D.db, "datastore_aggregate", _fake_agg)
    s = D.DatastorePg("u1")
    monkeypatch.setattr(s, "_resolve", lambda ns, write=False: 7)

    s.aggregate("vivier", filter={"statut": "qualified"},
                q="lyon", filters=[{"field": "bp", "op": "gte", "value": "100"}])
    assert seen["q"] == "lyon"
    assert seen["filters"] == [
        {"field": "statut", "op": "eq", "value": "qualified"},
        {"field": "bp", "op": "gte", "value": "100"},
    ]


# ── construction SQL pure (_build_aggregate), sans PG ──

from oto_mcp.db import datastore as DB  # noqa: E402


def test_build_global_count_default():
    sql, params, names = DB._build_aggregate(7, None, None, None, None, 1000)
    assert "COUNT(*) AS m0" in sql
    assert "GROUP BY" not in sql
    assert params == [7, 1000]              # WHERE ns_id, LIMIT
    assert names == [("m0", "count")]


def test_build_group_by_sum_then_count_param_order():
    sql, params, names = DB._build_aggregate(
        7, "departement",
        [{"op": "sum", "field": "kwc"}, {"op": "count"}],
        None, None, 500)
    # Ordre des %s : group field, (sum: field, regex, field), WHERE ns_id, LIMIT
    assert params == ["departement", "kwc", DB._NUMERIC_RE, "kwc", 7, 500]
    assert "GROUP BY grp ORDER BY m0 DESC NULLS LAST, grp ASC" in sql
    assert names == [("m0", "sum_kwc"), ("m1", "count")]
    # champ jamais interpolé en dur → pas de nom de colonne dans le SQL
    assert "departement" not in sql and "kwc" not in sql


def test_build_filter_params_after_select():
    filters = [{"field": "statut", "op": "eq", "value": "qualified"}]
    sql, params, names = DB._build_aggregate(
        7, None, [{"op": "avg", "field": "score"}], None, filters, 1000)
    # select params (score×2 + regex) puis ns_id puis filter value puis limit
    assert params[:3] == ["score", DB._NUMERIC_RE, "score"]
    assert params[3] == 7
    assert "qualified" in params and params[-1] == 1000


def test_build_rejects_unknown_op():
    import pytest
    with pytest.raises(ValueError):
        DB._build_aggregate(7, None, [{"op": "median", "field": "x"}], None, None, 1000)


def test_build_sum_requires_field():
    import pytest
    with pytest.raises(ValueError):
        DB._build_aggregate(7, None, [{"op": "sum"}], None, None, 1000)
