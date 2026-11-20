"""Mettre à jour des lignes PAR FILTRE (`data_update_where`) — contre un vrai PostgreSQL.

Ce qu'on vérifie est ce que la BASE porte après le geste, pas ce que l'appel a bien
voulu rendre : les valeurs posées, le journal des révisions de chaque ligne, la
colonne déclarée, et rien d'écrit quand la règle est refusée.
"""
from __future__ import annotations

import time
import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oto_mcp.datastore import par_filtre

SUB = "usr_updatewhere"
SCHEMA = {"fields": [{"key": "nom", "type": "text"},
                     {"key": "fit", "type": "number"},
                     {"key": "client", "type": "bool"},
                     {"key": "tier", "type": "text",
                      "options": ["Hot", "Tier 1", "Tier 2", "Tier 3"]}]}


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@updatewhere.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


@pytest.fixture(scope="module")
def live(pg_module_dsn):
    import os

    from oto_mcp.db import _conn as dbconn
    previous_url, previous_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = pg_module_dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        from oto_mcp import db
        db.upsert_user(SUB, email=f"{SUB}@updatewhere.invalid", name=SUB)
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = previous_pool
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


@pytest.fixture(scope="module")
def client(live):
    from oto_mcp.api import routes as api_routes
    return TestClient(Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None)))


@pytest.fixture
def store(live):
    from oto_mcp.datastore.core import make_store
    return make_store(SUB)


def _table(lignes, schema=SCHEMA):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns_id = db.create_datastore("user", SUB, "uw-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    st = make_store(SUB)
    if schema is not None:
        st.set_schema(ns, schema)
    if lignes:
        st.write_rows(ns, [dict(l) for l in lignes])
    return ns, ns_id


def _base(ns_id: int) -> dict:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        rows = conn.execute("SELECT data FROM datastore_rows WHERE ns_id = %s",
                            (ns_id,)).fetchall()
    return {r["data"]["nom"]: r["data"] for r in rows}


def _ids(ns_id: int) -> dict:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        rows = conn.execute("SELECT row_id, data FROM datastore_rows WHERE ns_id = %s",
                            (ns_id,)).fetchall()
    return {r["data"]["nom"]: r["row_id"] for r in rows}


def _revisions(ns_id: int) -> int:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM datastore_row_revisions WHERE ns_id = %s "
            "AND diff ? 'tier'", (ns_id,)).fetchone()["n"]


LIGNES = [{"nom": "a", "fit": 9, "client": True},
          {"nom": "b", "fit": 9, "client": False},
          {"nom": "c", "fit": 6, "client": False},
          {"nom": "d", "fit": 2, "client": False}]


def test_dry_run_counts_and_writes_nothing(store):
    ns, ns_id = _table(LIGNES)
    out = store.update_where(ns, {"tier": "Tier 1"},
                             filters=[{"field": "fit", "op": "gte", "value": 8}],
                             dry_run=True)
    assert out["dry_run"] is True and out["matched"] == 2
    assert {s["from"]["tier"] for s in out["sample"]} == {None}
    assert all(s["to"] == {"tier": "Tier 1"} for s in out["sample"])
    assert all("tier" not in d for d in _base(ns_id).values())


def test_ordered_rules_first_match_wins_and_land_in_history(store):
    ns, ns_id = _table(LIGNES)
    regles = [({"client": True}, None, "Hot"),
              (None, [{"field": "fit", "op": "gte", "value": 8}], "Tier 1"),
              (None, [{"field": "fit", "op": "gte", "value": 5}], "Tier 2"),
              (None, None, "Tier 3")]
    comptes = []
    for filt, filts, tier in regles:
        out = store.update_where(ns, {"tier": tier}, filter=filt, filters=filts,
                                 only_if_empty=True)
        assert out["next_cursor"] is None and out["refused"] == 0
        comptes.append(out["updated"])
    assert comptes == [1, 1, 1, 1]
    assert {n: d["tier"] for n, d in _base(ns_id).items()} == {
        "a": "Hot", "b": "Tier 1", "c": "Tier 2", "d": "Tier 3"}
    assert _revisions(ns_id) == 4
    # Rejouer toute la règle ne touche plus rien : chaque case est remplie.
    assert store.update_where(ns, {"tier": "Tier 3"}, only_if_empty=True)["matched"] == 0


def test_same_value_is_unchanged_not_rewritten(store):
    ns, ns_id = _table(LIGNES)
    store.update_where(ns, {"tier": "Tier 3"})
    out = store.update_where(ns, {"tier": "Tier 3"})
    assert out["updated"] == 0 and out["unchanged"] == 4
    assert _revisions(ns_id) == 4


def test_a_missing_column_is_declared_before_writing(store):
    ns, ns_id = _table(LIGNES)
    dry = store.update_where(ns, {"segment": "industrie"}, dry_run=True)
    assert dry["would_declare"] == [{"key": "segment", "type": "text"}]
    out = store.update_where(ns, {"segment": "industrie"})
    assert out["declared"] == [{"key": "segment", "type": "text"}]
    assert any(f["key"] == "segment" for f in store.get_schema(ns)["fields"])
    assert {d["segment"] for d in _base(ns_id).values()} == {"industrie"}


def test_a_value_the_schema_refuses_is_refused_whole(store):
    from oto_mcp.datastore.errors import RowValidationError
    ns, ns_id = _table(LIGNES)
    with pytest.raises(RowValidationError):
        store.update_where(ns, {"tier": "Tier 9"})
    assert all("tier" not in d for d in _base(ns_id).values())


def test_out_of_time_returns_a_cursor_that_resumes(store, monkeypatch):
    ns, ns_id = _table(LIGNES)
    monkeypatch.setattr(par_filtre, "BUDGET_S", -1.0)
    out = store.update_where(ns, {"tier": "Tier 2"})
    # Une ligne au moins par appel : on avance toujours.
    assert out["updated"] == 1 and out["next_cursor"]
    vus = 1
    while out["next_cursor"]:
        out = store.update_where(ns, {"tier": "Tier 2"}, cursor=out["next_cursor"])
        vus += out["updated"]
    assert vus == 4
    assert {d["tier"] for d in _base(ns_id).values()} == {"Tier 2"}


def test_bad_set_is_refused(store):
    ns, _ = _table(LIGNES)
    for mauvais in ({}, {"_id": "x"}, {"tags[+]": "x"}):
        with pytest.raises(ValueError):
            store.update_where(ns, mauvais)


def test_rest_face(client):
    ns, ns_id = _table(LIGNES)
    h = {"Authorization": f"Bearer {SUB}"}
    r = client.post(f"/api/datastores/{ns_id}/rows/update_where", headers=h,
                    json={"set": {"tier": "Tier 1"}, "filter": {"nom": "a"}})
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 1 and r.json()["ns_id"] == ns_id
    assert _base(ns_id)["a"]["tier"] == "Tier 1"


def test_two_thousand_rows_through_the_cursor(store):
    """La taille d'un tableau réel noté. Le temps se MESURE, il ne s'affirme pas : une
    machine chargée rend la main plus tôt, le curseur fait le reste."""
    ns, ns_id = _table([{"nom": f"n{i}", "fit": i % 10} for i in range(2000)])
    t = time.monotonic()
    out = store.update_where(ns, {"tier": "Tier 3"})
    total, appels = out["updated"], 1
    while out["next_cursor"]:
        out = store.update_where(ns, {"tier": "Tier 3"}, cursor=out["next_cursor"])
        total, appels = total + out["updated"], appels + 1
    print(f"\n2000 lignes : {time.monotonic() - t:.1f} s, {appels} appel(s)")
    assert total == 2000


def test_null_and_business_key_are_refused(store):
    ns, ns_id = _table(LIGNES, schema={**SCHEMA, "key": "nom"})
    with pytest.raises(ValueError, match="WOULD CLEAR"):
        store.update_where(ns, {"tier": None})
    with pytest.raises(ValueError, match="business key"):
        store.update_where(ns, {"nom": "x"})
    assert all("tier" not in d for d in _base(ns_id).values())


@pytest.mark.parametrize("vide", [None, "", [], {}, "@empty", {"valeur": None},
                                  {"valeur": "x", "comment": ""}])
def test_every_value_that_would_erase_is_refused(store, vide):
    ns, ns_id = _table(LIGNES)
    with pytest.raises(ValueError, match="WOULD CLEAR"):
        store.update_where(ns, {"tier": vide})


def test_only_if_empty_fills_an_assumed_empty_like_data_rows(store):
    ns, ns_id = _table(LIGNES)
    store.update_row(ns, _ids(ns_id)["a"], {"tier": {"valeur": "@empty",
                                                     "comment": "cherché"}})
    out = store.update_where(ns, {"tier": "Hot"}, filter={"nom": "a"}, only_if_empty=True)
    assert out["updated"] == 1
    assert _base(ns_id)["a"]["tier"] == "Hot"


def test_unchanged_is_judged_on_the_unwrapped_value_and_its_type(store):
    ns, ns_id = _table(LIGNES)
    store.update_where(ns, {"tier": {"valeur": "Tier 1", "comment": "regle A"}})
    avant = _revisions(ns_id)
    out = store.update_where(ns, {"tier": "Tier 1"})
    assert out["unchanged"] == 4 and out["updated"] == 0
    assert _revisions(ns_id) == avant
    # Au type près : `1` n'est pas `True`, la ligne n'est PAS sautée (colonne sans type).
    ns2, ns2_id = _table(LIGNES, schema={"fields": SCHEMA["fields"][:2] + [
        {"key": "client"}]})
    out = store.update_where(ns2, {"client": 1}, filter={"nom": "a"})
    assert out["unchanged"] == 0


def test_readonly_is_refused_unless_filling_empties(store):
    ns, ns_id = _table(LIGNES, schema={"fields": SCHEMA["fields"][:3] + [
        {"key": "tier", "type": "text", "readonly": True}]})
    with pytest.raises(ValueError, match="readonly"):
        store.update_where(ns, {"tier": "Hot"})
    assert store.update_where(ns, {"tier": "Hot"}, only_if_empty=True)["updated"] == 4


def test_origine_layer_and_layer_only_empty_rule_are_refused(store):
    ns, _ = _table(LIGNES)
    with pytest.raises(ValueError, match="origine"):
        store.update_where(ns, {"tier": {"valeur": "Hot", "origine": "x"}})
    with pytest.raises(ValueError, match="only_if_empty"):
        store.update_where(ns, {"tier.comment": "x"}, only_if_empty=True)


def test_rule_refused_on_every_row_is_refused_whole_and_undeclared(store):
    """Une faute que le jugement d'avant la boucle ne voit pas : un `required` que
    les lignes en place ne portent pas. Trois refus, rien d'écrit → la règle est
    refusée, et la colonne déclarée pour elle est retirée."""
    ns, ns_id = _table(LIGNES)
    store.patch_schema(ns, fields=[{"key": "pays", "type": "text", "required": True}])
    with pytest.raises(ValueError):
        store.update_where(ns, {"segment": "industrie"})
    assert not any(f["key"] == "segment" for f in store.get_schema(ns)["fields"])
    assert all("segment" not in d for d in _base(ns_id).values())


def test_matched_counts_after_the_cursor(store):
    ns, ns_id = _table(LIGNES)
    ids = sorted(_ids(ns_id).values())
    assert store.update_where(ns, {"tier": "Hot"}, dry_run=True,
                              cursor=ids[1])["matched"] == 2


def test_mcp_face(live, monkeypatch):
    """La face agent : la signature dérivée (un paramètre nommé `set`), l'appel par
    l'adaptateur, la base écrite."""
    import asyncio

    import fastmcp.server.context as _fc
    from fastmcp import FastMCP

    from oto_mcp.capabilities import _mcp_adapter
    from oto_mcp.capabilities.registry import CAPABILITIES
    monkeypatch.setattr(_mcp_adapter, "current_user_sub_from_token", lambda: SUB)
    ns, ns_id = _table(LIGNES)
    mcp = FastMCP("test")
    _mcp_adapter.register(mcp, [c for c in CAPABILITIES
                                if c.key == "me.datastore.update_where"])

    async def _appel():
        tool = await mcp.get_tool("data_update_where")
        assert "set" in tool.parameters["properties"]
        async with _fc.Context(fastmcp=mcp):
            return (await tool.run({"datastore": str(ns_id), "set": {"tier": "Hot"},
                                    "filter": {"client": True}})).structured_content

    out = asyncio.run(_appel())
    assert out["updated"] == 1 and out["matched"] == 1
    assert _base(ns_id)["a"]["tier"] == "Hot"


# ── l'ajout `col[+]` ─────────────────────────────────────────────────────────────

SCHEMA_JOURNAL = {"fields": SCHEMA["fields"] + [
    {"key": "journal", "type": "text"},
    {"key": "tags", "type": "list", "of": {"type": "text"}},
    {"key": "fige", "type": "text", "readonly": True}]}


def _valeur(cellule):
    return cellule.get("valeur") if isinstance(cellule, dict) else cellule


def test_append_adds_a_line_to_each_matching_row_without_reading_it(store):
    ns, ns_id = _table([{**l, "journal": "debut"} for l in LIGNES], SCHEMA_JOURNAL)
    out = store.update_where(ns, {"journal[+]": "relance J+3", "tags[+]": "relance"},
                             filter={"fit": {"gte": 8}})
    assert (out["matched"], out["updated"], out["unchanged"]) == (2, 2, 0)
    base = _base(ns_id)
    assert _valeur(base["a"]["journal"]) == "debut\nrelance J+3"
    assert _valeur(base["a"]["tags"]) == ["relance"]
    assert _valeur(base["c"]["journal"]) == "debut"
    # Un ajout n'est jamais « déjà en place » : rejouer la règle ajoute encore.
    again = store.update_where(ns, {"journal[+]": "relance J+3"}, filter={"fit": {"gte": 8}})
    assert (again["updated"], again["unchanged"]) == (2, 0)
    assert _valeur(_base(ns_id)["b"]["journal"]) == "debut\nrelance J+3\nrelance J+3"


def test_concurrent_append_rules_all_land_without_conflicts(store):
    import threading

    from oto_mcp.datastore.core import make_store
    ns, ns_id = _table([{"nom": "a", "fit": 9}], SCHEMA_JOURNAL)
    tours, sorties, erreurs = 6, [], []

    def courir(texte):
        barriere.wait()
        try:
            sorties.append(make_store(SUB).update_where(ns, {"journal[+]": texte},
                                                        filter={"nom": "a"}))
        except BaseException as e:  # noqa: BLE001 — rendue au test
            erreurs.append(e)

    for i in range(tours):
        barriere = threading.Barrier(2)
        fils = [threading.Thread(target=courir, args=(f"{p}{i}",)) for p in "xy"]
        for f in fils:
            f.start()
        for f in fils:
            f.join(timeout=60)
    assert erreurs == []
    assert all(o["conflicts"] == 0 and o["updated"] == 1 for o in sorties)
    lignes = _valeur(_base(ns_id)["a"]["journal"]).split("\n")
    assert sorted(lignes) == sorted(f"{p}{i}" for p in "xy" for i in range(tours))


@pytest.mark.parametrize("set_, kw, attendu", [
    ({"inconnue[+]": "x"}, {}, "Declare it first"),
    ({"journal[+]": "x"}, {"only_if_empty": True}, "only_if_empty"),
    ({"fige[+]": "x"}, {}, "readonly"),
    ({"tags[0]": "x"}, {}, "list INDEX"),
    ({"tags[-]": "x"}, {}, "list INDEX"),
])
def test_append_rule_faults_are_refused_before_any_row(store, set_, kw, attendu):
    ns, ns_id = _table([{"nom": "a", "fit": 9, "journal": "debut"}], SCHEMA_JOURNAL)
    with pytest.raises(ValueError) as exc:
        store.update_where(ns, set_, **kw)
    assert attendu in str(exc.value)
    assert _valeur(_base(ns_id)["a"]["journal"]) == "debut"


def test_append_past_the_bound_is_refused_per_row(store, monkeypatch):
    from oto_mcp.datastore import rangs
    monkeypatch.setattr(rangs, "TAILLE_MAX_APRES_AJOUT", 30)
    ns, ns_id = _table([{"nom": "a", "fit": 9, "journal": "court"},
                        {"nom": "b", "fit": 9, "journal": "z" * 28}], SCHEMA_JOURNAL)
    out = store.update_where(ns, {"journal[+]": "suite"}, filter={"fit": 9})
    assert (out["updated"], out["refused"]) == (1, 1)
    assert "TABLE" in out["refused_sample"][0]["error"]
    assert _valeur(_base(ns_id)["b"]["journal"]) == "z" * 28
