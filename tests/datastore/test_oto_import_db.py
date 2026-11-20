"""`oto_import` on a real store: an empty table gets its columns and rows from a file
nobody retyped, a re-run with a key updates, a tight budget resumes where it stopped,
and a refused row names its absolute position. Only the fetch is stubbed."""
from __future__ import annotations

import uuid

import pytest

SUB = "sub-oto-import"


def _sql(requete: str, *params):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        cur = conn.execute(requete, params or None)
        return cur.fetchall() if cur.description else None


@pytest.fixture
def table(live):
    from oto_mcp import db
    db.upsert_user(SUB, email=f"{SUB}@import.invalid", name=SUB)
    ns_id = db.create_datastore("user", SUB, "imp-" + uuid.uuid4().hex[:6])
    return str(ns_id), ns_id


@pytest.fixture
def servir(monkeypatch):
    from oto_mcp import access, file_source
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: SUB)
    fichier = {}
    monkeypatch.setattr(file_source, "resolve", lambda source, **kw: file_source.ResolvedFile(
        fichier["data"], fichier.get("name", "leads.csv"), "text/csv"))
    return fichier


def _importer(ns, **kw):
    from oto_mcp.capabilities import uploads as up

    class Ctx:
        sub, org_id = SUB, None
    return up._import(Ctx(), up.ImportInput(
        source={"kind": "url", "url": "https://files.example/leads.csv?sig=x"},
        datastore=ns, **kw))


def _lignes(ns_id):
    return [r["data"] for r in _sql(
        "SELECT data FROM datastore_rows WHERE ns_id = %s ORDER BY row_id", ns_id)]


CSV = ("﻿SIREN;Raison sociale;Pays;Date de création\n"
       "1;ACME;France;2001\n2;Beta;Italie;1999\n").encode("utf-8")


def test_an_empty_table_is_declared_and_filled_from_the_file(table, servir):
    ns, ns_id = table
    servir["data"] = CSV
    r = _importer(ns, key="SIREN")
    assert r["done"] and r["inserted"] == 2 and r["separator"] == ";"
    assert set(r["created_columns"]) == {"siren", "raison_sociale", "pays",
                                         "date_de_creation"}
    assert r["source"]["url"] == "https://files.example/leads.csv"
    from oto_mcp.datastore.core import make_store
    schema = make_store(SUB)._schema_of(ns_id)
    labels = {f["key"]: f.get("label") for f in schema["fields"]}
    assert labels["raison_sociale"] == "Raison sociale"
    assert _lignes(ns_id)[0]["raison_sociale"] == "ACME"


def test_the_imported_table_is_ready_for_a_server_side_qualification(table, servir):
    """What `jev_rows` needs before its first call: every column it will read is a
    DECLARED column, under a stable key. The full chain (import → jev_rows dry run)
    lives on the jev branch, where `jev_rows` exists."""
    ns, ns_id = table
    servir["data"] = CSV
    _importer(ns, key="SIREN")
    from oto_mcp.datastore.core import make_store
    declared = {f["key"]: f["type"] for f in make_store(SUB)._schema_of(ns_id)["fields"]}
    assert declared == {"siren": "text", "raison_sociale": "text", "pays": "text",
                        "date_de_creation": "text"}
    assert all(set(row) <= set(declared) for row in _lignes(ns_id))


def test_a_re_run_with_a_key_updates_and_labels_match_declared_columns(table, servir):
    ns, ns_id = table
    servir["data"] = CSV
    _importer(ns, key="SIREN")
    servir["data"] = "siren,RAISON SOCIALE\n1,ACME SA\n".encode()
    r = _importer(ns, key="siren")
    assert (r["inserted"], r["updated"]) == (0, 1)
    assert r["matched_by_label"] == {"RAISON SOCIALE": "raison_sociale"}
    assert "created_columns" not in r
    assert len(_lignes(ns_id)) == 2


def test_every_slice_is_stamped_with_the_calls_gesture(table, servir, monkeypatch):
    """The face (MCP or REST) opens the gesture; the sliced write must stay inside it."""
    from oto_mcp import geste, upload_tokens
    ns, ns_id = table
    monkeypatch.setattr(upload_tokens, "IMPORT_SLICE", 1)
    servir["data"] = CSV
    gid = geste.nouvel_identifiant()
    with geste.portee("agent", SUB, geste_id=gid):
        _importer(ns, key="SIREN")
    revs = _sql("SELECT DISTINCT acteur, source, geste_id FROM datastore_row_revisions "
                "WHERE ns_id = %s", ns_id)
    assert [(r["acteur"], r["source"], r["geste_id"]) for r in revs] == [(SUB, "agent", gid)]


def test_a_tight_budget_stops_between_slices_and_resumes(table, servir, monkeypatch):
    from oto_mcp import upload_tokens
    from oto_mcp.capabilities import uploads as up
    ns, ns_id = table
    servir["data"] = ("id,name\n" + "".join(f"{i},n{i}\n" for i in range(1200))).encode()
    monkeypatch.setattr(upload_tokens, "IMPORT_SLICE", 500)
    monkeypatch.setattr(up, "IMPORT_BUDGET_S", 0.0)  # every call stops after one slice
    r1 = _importer(ns)
    assert (r1["done"], r1["count"], r1["resume_from"]) == (False, 500, 500)
    assert "no_key" in r1
    r2 = _importer(ns, resume_from=r1["resume_from"], source_sha256=r1["source"]["sha256"])
    r3 = _importer(ns, resume_from=r2["resume_from"], source_sha256=r1["source"]["sha256"])
    assert r3["done"] and r1["count"] + r2["count"] + r3["count"] == 1200
    assert len(_lignes(ns_id)) == 1200, "a resume without a key never duplicates"


def test_a_refused_row_names_its_absolute_position(table, servir, monkeypatch):
    from oto_mcp import upload_tokens
    from oto_mcp.capabilities._types import AuthzDenied
    from oto_mcp.datastore.core import make_store
    ns, ns_id = table
    make_store(SUB).set_schema(ns, {"fields": [{"key": "id", "type": "text"},
                                               {"key": "n", "type": "number"}]})
    monkeypatch.setattr(upload_tokens, "IMPORT_SLICE", 3)
    servir["data"] = b"id,n\na,1\nb,2\nc,3\nd,4\ne,oops\nf,6\n"
    with pytest.raises(AuthzDenied) as e:
        _importer(ns, key="id")
    assert e.value.code == "bad_row"
    assert e.value.details == {"row": 5, "written": 4, "resume_from": 5}
    assert len(_lignes(ns_id)) == 4


def test_a_sheet_with_dotted_and_empty_headers_declares_every_key(table, servir):
    ns, ns_id = table
    servir["data"] = ("Buy,,acme.company,acme-demo.com,acme-tool.com\n"
                      "yes,junk,ACME,1,2\nno,,Beta,3,4\n").encode()
    r = _importer(ns)
    assert r["dropped_empty_headers"] == [2]
    from oto_mcp.datastore.core import make_store
    declared = {f["key"] for f in make_store(SUB)._schema_of(ns_id)["fields"]}
    assert declared == {"buy", "acme_company", "acme_demo_com", "acme_tool_com"}
    assert all(set(row) == declared for row in _lignes(ns_id)), "no undeclared key"


def test_re_importing_the_same_sheet_reuses_its_columns(table, servir):
    ns, ns_id = table
    servir["data"] = "Buy,acme.company,acme-demo.com\nyes,ACME,1\n".encode()
    _importer(ns)
    r = _importer(ns)
    assert "created_columns" not in r and r["inserted"] == 1
    from oto_mcp.datastore.core import make_store
    declared = {f["key"] for f in make_store(SUB)._schema_of(ns_id)["fields"]}
    assert declared == {"buy", "acme_company", "acme_demo_com"}
    assert all(set(row) == declared for row in _lignes(ns_id))
