"""Agrégat serveur `data_aggregate` (feedback #191). Le store convertit le `filter`
plat en clauses SQL et délègue à `db.datastore_aggregate` (seam PG monkeypatché —
la construction SQL réelle est validée en preprod)."""
from __future__ import annotations

from oto_mcp.datastore import core as D


def test_aggregate_delegates_and_converts_filter(monkeypatch):
    seen = {}

    def _fake_agg(ns_id, *, group_by=None, metrics=None, q=None, filters=None, limit=1000):
        seen.update(ns_id=ns_id, group_by=group_by, metrics=metrics, filters=filters)
        return [{"departement": "69", "sum_kwc": 1200.0, "count": 3}]

    monkeypatch.setattr(D.db, "datastore_aggregate", _fake_agg)
    s = D.DatastorePg("u1")
    monkeypatch.setattr(s, "_resolve", lambda ns, write=False: 7)
    monkeypatch.setattr(s, "_schema_of", lambda ns_id: None)  # pas de schéma sur ce banc

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
    monkeypatch.setattr(s, "_schema_of", lambda ns_id: None)  # pas de schéma sur ce banc

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
    monkeypatch.setattr(s, "_schema_of", lambda ns_id: None)  # pas de schéma sur ce banc

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
    # Ordre des %s — chaque champ compte DOUBLE depuis #318 (un `%s` par branche
    # du COALESCE qui lit une colonne plate ou à couches) : group ×2,
    # (sum : champ ×2, regex, champ ×2), WHERE ns_id, LIMIT.
    assert params == ["departement"] * 2 + ["kwc"] * 2 + [DB._NUMERIC_RE] \
        + ["kwc"] * 2 + [7, 500]
    assert "GROUP BY grp ORDER BY m0 DESC NULLS LAST, grp ASC" in sql
    assert names == [("m0", "sum_kwc"), ("m1", "count")]
    # champ jamais interpolé en dur → pas de nom de colonne dans le SQL
    assert "departement" not in sql and "kwc" not in sql


def test_build_filter_params_after_select():
    filters = [{"field": "statut", "op": "eq", "value": "qualified"}]
    sql, params, names = DB._build_aggregate(
        7, None, [{"op": "avg", "field": "score"}], None, filters, 1000)
    # select params (score ×4 + regex, cf. #318) puis ns_id puis filter puis limit
    assert params[:5] == ["score"] * 2 + [DB._NUMERIC_RE] + ["score"] * 2
    assert params[5] == 7
    assert "qualified" in params and params[-1] == 1000


def test_build_rejects_unknown_op():
    import pytest
    with pytest.raises(ValueError):
        DB._build_aggregate(7, None, [{"op": "median", "field": "x"}], None, None, 1000)


def test_build_sum_requires_field():
    import pytest
    with pytest.raises(ValueError):
        DB._build_aggregate(7, None, [{"op": "sum"}], None, None, 1000)


# ── oto#50 : un `group_by` composé se refuse, il ne s'agrège pas sur NULL ──────

def test_un_group_by_compose_est_REFUSE_pas_agrege_sur_null(monkeypatch):
    """Le fait mesuré : une mission cliente appelle `group_by="lot_test,statut"` et
    reçoit **200, un unique groupe de clé `null`** contenant toutes ses lignes.

    La chaîne partait telle quelle jusqu'au SQL, où `data->>'lot_test,statut'` vaut
    NULL sur chaque ligne — donc un seul groupe. C'est la forme la plus coûteuse de la
    classe oto#42 : l'agent lit « un groupe, clé nulle » et conclut que la donnée est
    vide ou mal remplie. Il part corriger un tableau qui n'a rien, et rien dans la
    réponse ne peut le détromper.

    ⚠️ On vérifie que la base n'est même pas INTERROGÉE : un refus qui laisserait
    partir la requête coûterait le scan, et surtout laisserait croire qu'il existe un
    chemin où cette forme fonctionne."""
    appels = []
    monkeypatch.setattr(D.db, "datastore_aggregate",
                        lambda *a, **k: appels.append(k) or [])
    s = D.DatastorePg("u1")
    monkeypatch.setattr(s, "_resolve", lambda ns, write=False: 7)
    monkeypatch.setattr(s, "_schema_of", lambda ns_id: None)

    import pytest
    with pytest.raises(ValueError) as e:
        s.aggregate("t", group_by="lot_test,statut")
    msg = str(e.value)
    assert appels == [], "la requête ne doit pas partir : le refus est AVANT le SQL"

    # Le refus nomme les DEUX gestes possibles — sans quoi il ne fait qu'arrêter.
    assert "`lot_test`, `statut`" in msg, "les champs demandés sont rendus, un par un"
    assert "LISTE" in msg, "la forme liste existe et fait autre chose : elle est nommée"
    assert "fusionne, elle ne croise pas" in msg, (
        "sans cette phrase, on essaie la liste en croyant obtenir un croisement — "
        "c'est l'erreur naturelle de qui vient d'écrire une virgule")
    # Et ce qu'il faudra trancher le jour où quelqu'un le demande vraiment : le refus
    # ouvre la porte au lieu de la murer.
    assert "clé jointe, tuple, ou objet" in msg.replace("—", "").replace("  ", " ") \
        or "clé " in msg and "tuple" in msg


def test_un_group_by_SIMPLE_passe_toujours(monkeypatch):
    """Pas d'écart, pas de refus : le cas nominal ne doit rien coûter. Une garde qui
    refuserait une virgule dans un nom de champ légitime casserait un usage réel."""
    seen = {}
    monkeypatch.setattr(D.db, "datastore_aggregate",
                        lambda ns_id, **k: seen.update(k) or [{"statut": "ok"}])
    s = D.DatastorePg("u1")
    monkeypatch.setattr(s, "_resolve", lambda ns, write=False: 7)
    monkeypatch.setattr(s, "_schema_of", lambda ns_id: None)
    s.aggregate("t", group_by="statut")
    assert seen["group_by"] == "statut"


def test_la_forme_LISTE_reste_acceptee(monkeypatch):
    """⚠️ Le garde-fou du garde-fou. La liste EXISTE et met les valeurs en commun ;
    le refus ne vise que la chaîne à virgules. Les confondre retirerait une forme
    servie en croyant fermer un défaut."""
    seen = {}
    monkeypatch.setattr(D.db, "datastore_aggregate",
                        lambda ns_id, **k: seen.update(k) or [])
    s = D.DatastorePg("u1")
    monkeypatch.setattr(s, "_resolve", lambda ns, write=False: 7)
    monkeypatch.setattr(s, "_schema_of", lambda ns_id: None)
    s.aggregate("t", group_by=["lot_test", "statut"])
    assert seen["group_by"] == ["lot_test", "statut"]
