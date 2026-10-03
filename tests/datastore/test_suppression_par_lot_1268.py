"""`data_delete_row(ids=[…])` : un lot de suppressions en UN appel (#1268).

Un agent qui vidait 45 lignes faisait 45 appels — 23 minutes d'un run. Le lot garde,
ligne par ligne, ce que la suppression unitaire garantit (verrou, bail, révision) :
un refus sur une ligne n'arrête pas les autres, et un lot mal formé ne supprime RIEN.
Jugé sur ce que porte la BASE, par l'outil tel que le boot le charge.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

SUB = "sub-1268"


def _store():
    from oto_mcp.datastore.core import make_store
    return make_store(SUB)


def _table(n: int = 4) -> tuple[str, int]:
    from oto_mcp import db
    ns = "p1268-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", SUB, ns)
    _store().set_schema(ns, {"fields": [{"key": "titre", "type": "text"}]})
    for i in range(1, n + 1):
        db.datastore_insert_row(ns_id, f"r{i}", {"titre": f"t{i}"})
    return ns, ns_id


def _rev(ns_id: int, row_id: str):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        row = conn.execute("SELECT rev FROM datastore_rows WHERE ns_id = %s "
                           "AND row_id = %s", (ns_id, row_id)).fetchone()
        return str(row["rev"]) if row else None


def _restantes(ns_id: int) -> set:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return {r["row_id"] for r in conn.execute(
            "SELECT row_id FROM datastore_rows WHERE ns_id = %s", (ns_id,)).fetchall()}


_OUTIL: list = []


@pytest.fixture
def mcp(live, monkeypatch):
    from fastmcp import FastMCP

    from oto_mcp.tools import datastore as T
    from oto_mcp.tools import register_all
    monkeypatch.setattr(T, "_store_for", lambda sub: _store())
    monkeypatch.setattr(T.access, "current_user_sub_or_raise", lambda: SUB)
    monkeypatch.setattr(T, "_ns", lambda ns: ns)
    if not _OUTIL:
        m = FastMCP("t1268")
        register_all(m)
        _OUTIL.append(asyncio.run(m.get_tool("data_delete_row")))
    return lambda **arguments: asyncio.run(
        _OUTIL[0].run(arguments)).structured_content


def test_un_lot_supprime_tout_en_un_appel(mcp):
    ns, ns_id = _table()
    corps = mcp(datastore=ns, ids=["r1", "r2", {"id": "r3",
                                                "expected_revision": _rev(ns_id, "r3")}])
    assert corps == {"ok": True, "count": 3, "deleted": ["r1", "r2", "r3"],
                     "not_found": [], "refused": []}
    assert _restantes(ns_id) == {"r4"}


def test_une_ligne_refusee_n_arrete_pas_le_lot(mcp):
    """r2 a changé depuis la lecture, r3 est réservée par un autre travail : elles
    restent, avec leur raison ; r1 part, r9 (absente) est dite absente."""
    ns, ns_id = _table()
    lue = _rev(ns_id, "r2")
    _store().update_row(ns, "r2", {"titre": "changé"})
    _store().claim_row(ns, "r3", worker="autre", lease_s=600)
    corps = mcp(datastore=ns, ids=["r1", {"id": "r2", "expected_revision": lue},
                                   "r3", "r9"])
    assert corps["ok"] is False
    assert (corps["deleted"], corps["not_found"]) == (["r1"], ["r9"])
    refus = {r["id"]: r for r in corps["refused"]}
    assert set(refus) == {"r2", "r3"}
    assert "revision_conflict" in refus["r2"]["error"]
    assert refus["r2"]["current_revision"] == _rev(ns_id, "r2")
    assert "current_revision" not in refus["r3"]
    assert _restantes(ns_id) == {"r2", "r3", "r4"}


@pytest.mark.parametrize("ids", [
    ["r1", "r2", 42],
    ["r1", {"id": "r2", "expected_revision": "x"}],
    ["r1", {"id": "r2", "revision": "1"}],
    ["r1", "r1"],
    [],
])
def test_un_lot_mal_forme_ne_supprime_rien(mcp, ids):
    ns, ns_id = _table()
    with pytest.raises(Exception):
        mcp(datastore=ns, ids=ids)
    assert _restantes(ns_id) == {"r1", "r2", "r3", "r4"}


def test_ids_ne_se_melange_pas_a_id(mcp):
    ns, ns_id = _table()
    with pytest.raises(Exception, match="ids"):
        mcp(datastore=ns, id="r1", ids=["r2"])
    with pytest.raises(Exception, match="ids"):
        mcp(datastore=ns, ids=["r2"], expected_revision="1")
    with pytest.raises(Exception, match="required"):
        mcp(datastore=ns)
    assert _restantes(ns_id) == {"r1", "r2", "r3", "r4"}


def test_le_geste_unitaire_reste_identique(mcp):
    ns, ns_id = _table()
    assert mcp(datastore=ns, id="r1") == {"ok": True, "id": "r1"}
    assert _restantes(ns_id) == {"r2", "r3", "r4"}
