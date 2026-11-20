"""Écrire dans une colonne NON DÉCLARÉE ne la crée pas (oto#124).

**Le fait.** Une écriture qui nommait une colonne inconnue la créait à la volée, et le
schéma d'un tableau s'étendait au fil des écritures. **Décidé le 05/10/2026 : une seule
règle, plus de modes.** Une colonne que le schéma ne déclare pas est REFUSÉE
(`unknown_column`), sur tous les tableaux, tableau sans schéma compris ; le refus dit
le geste (`data_patch_schema`).

Chaque face est jouée contre un vrai PostgreSQL : on lit la BASE, pas seulement ce que
l'appel a bien voulu rendre.
"""
from __future__ import annotations

import time
import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oto_mcp.datastore import colonnes_non_declarees as cnd

SUB = "usr_colonnes124"
SCHEMA = {"fields": [{"key": "nom", "type": "text"}, {"key": "ville", "type": "text"}]}


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@colonnes.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


def _h() -> dict:
    return {"Authorization": f"Bearer {SUB}"}


# ── le prédicat, le texte servi (sans base) ──────────────────────────────────

def test_le_predicat_juge_ce_qui_est_pose_contre_ce_qui_est_declare():
    # Sans schéma, ou sans colonne déclarée : TOUTE colonne posée est non déclarée.
    assert cnd.non_declarees(None, {"b": 1, "a": 2}) == ["a", "b"]
    assert cnd.non_declarees({"key": "a"}, {"a": 1}) == ["a"]
    # Une couche d'une colonne déclarée n'est pas une colonne.
    assert cnd.non_declarees(SCHEMA, {"nom": "x", "nom.comment": "y", "age": 3}) == ["age"]
    # Le lot : clés BRUTES, base des clés pointées et par rang, `null` et `_…` exclus.
    lot = [{"nom": "a", "contacts[+]": {"x": 1}}, {"site.comment": "c", "_id": "r1"},
           {"ville.comment": "ok", "efface": None, "vide": {}}, "pas un objet"]
    assert cnd.non_declarees_du_lot(SCHEMA, lot) == ["contacts", "site"]


def test_le_texte_servi_dit_le_refus_au_present():
    ecriture, schema = cnd.DESCRIPTION_ECRITURE, cnd.description_schema()
    assert "is REFUSED" in ecriture and "`unknown_column`" in ecriture
    assert "data_patch_schema" in ecriture
    assert "is REFUSED" in schema and "data_patch_schema" in schema
    for texte in (ecriture, schema):
        assert "2026-10-21" not in texte and "until" not in texte.lower(), texte


def test_la_description_servie_dit_la_regle():
    """`data_write`, `data_set_schema`, `data_patch_schema` et les faces REST d'écriture
    disent la règle — le texte servi pilote l'agent."""
    import asyncio

    from fastmcp import FastMCP

    from oto_mcp.capabilities.datastore import columns
    from oto_mcp.capabilities.registry import CAPABILITIES
    from oto_mcp.tools import datastore as tools_ds
    mcp = FastMCP("t")
    tools_ds.register(mcp)
    for nom in ("data_set_schema", "data_write"):
        desc = asyncio.run(mcp.get_tool(nom)).description
        assert "<<" not in desc, nom
        assert "does NOT declare is REFUSED" in desc, nom
    patch = next(c for c in columns.CAPABILITIES if c.key == "me.datastore.patch_schema")
    assert "does NOT declare is REFUSED" in patch.description
    assert "unknown_columns" not in patch.Input.model_fields, "retiré (oto#124)"
    for cle in ("me.datastore.append_row", "me.datastore.write_rows",
                "me.datastore.update_row"):
        cap = next(c for c in CAPABILITIES if c.key == cle)
        assert "does NOT declare is REFUSED" in cap.description, cle
        assert "unknown_column" in {e.code for e in cap.errors}, cle


# ── la base ──────────────────────────────────────────────────────────────────

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
        db.upsert_user(SUB, email=f"{SUB}@colonnes.invalid", name=SUB)
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
def data_write(live, monkeypatch):
    import asyncio

    from fastmcp import FastMCP

    from oto_mcp import access
    from oto_mcp.tools import datastore as tools_ds
    monkeypatch.setattr(access, "current_user_sub_from_token", lambda: SUB)
    mcp = FastMCP("test")
    tools_ds.register(mcp)
    return asyncio.run(mcp.get_tool("data_write")).fn


def _table(schema=SCHEMA, lignes=()):
    """Un tableau neuf, ses lignes (déclarées) posées AVANT le geste jugé — rend
    (adresse = numéro en texte, ns_id, [_id])."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns_id = db.create_datastore("user", SUB, "t-" + uuid.uuid4().hex[:6])
    adr = str(ns_id)
    store = make_store(SUB)
    if schema is not None:
        store.set_schema(adr, schema)
    ids = [store.append_row(adr, dict(ligne))["_id"] for ligne in lignes]
    return adr, ns_id, ids


def _base(ns_id: int) -> list[dict]:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        rows = conn.execute("SELECT row_id, data FROM datastore_rows WHERE ns_id = %s "
                            "ORDER BY created_at, row_id", (ns_id,)).fetchall()
    return [{"_id": r["row_id"], **r["data"]} for r in rows]


def _refus_mcp(data_write, **kw) -> str:
    from oto_mcp.mcp_errors import McpError
    with pytest.raises(McpError) as exc:
        data_write(**kw)
    return exc.value.error.message


# ── refusée, rien n'est écrit ────────────────────────────────────────────────

@pytest.mark.parametrize("schema", [None, SCHEMA], ids=["sans schéma", "avec schéma"])
def test_la_ligne_seule_est_refusee_avec_son_code(client, data_write, schema):
    ns, ns_id, _ = _table(schema)
    ligne = {"nom": "A", "age": 41}
    msg = _refus_mcp(data_write, datastore=ns, row=ligne)
    r = client.post(f"/api/datastores/{ns}/rows", headers=_h(), json=ligne)
    assert r.status_code == 400, r.text
    corps = r.json()
    assert corps["error"] == "unknown_column"
    assert "age" in corps["details"]["colonnes"]
    for texte in (msg, corps["detail"]):
        assert "`age`" in texte and "nothing was written" in texte
        assert "data_patch_schema" in texte and "PATCH /api/datastores/" in texte
    if schema is None:
        assert "does not declare any column yet" in corps["detail"]
    else:
        assert "Table columns: `nom`, `ville`" in corps["detail"]
    assert _base(ns_id) == []


def test_une_colonne_declaree_et_sa_couche_passent(data_write):
    ns, ns_id, _ = _table()
    data_write(datastore=ns, row={"nom": "A", "nom.comment": "source : registre"})
    assert _base(ns_id)[0]["nom"] == {"valeur": "A", "comment": "source : registre"}


def test_declarer_la_colonne_ouvre_l_ecriture(client, data_write):
    ns, ns_id, _ = _table(None)
    r = client.patch(f"/api/datastores/{ns}/schema", headers=_h(),
                     json={"fields": [{"key": "nom"}, {"key": "age", "type": "number"}]})
    assert r.status_code == 200, r.text
    data_write(datastore=ns, row={"nom": "A", "age": 41})
    assert _base(ns_id)[0]["age"] == 41


def test_le_lot_est_refuse_entier_avant_sa_premiere_ligne(client, data_write):
    ns, ns_id, _ = _table()
    lot = [{"nom": "A"}, {"nom": "B", "age": 2}]
    msg = _refus_mcp(data_write, datastore=ns, rows=lot)
    r = client.post(f"/api/datastores/{ns}/rows/batch", headers=_h(), json={"rows": lot})
    assert r.status_code == 400 and r.json()["error"] == "unknown_column", r.text
    for texte in (msg, r.json()["detail"]):
        assert "this batch is refused ENTIRELY" in texte and "`age`" in texte
    assert _base(ns_id) == []


def test_le_patch_juge_le_geste_pas_le_passe(client):
    """Une ligne qui porte une colonne ANCIENNE non déclarée reste écrivable sur ses
    colonnes déclarées, et la colonne ancienne s'efface (`null`) ; la réécrire, non."""
    from oto_mcp import db
    ns, ns_id, _ = _table()
    db.datastore_insert_row(ns_id, "r-ancienne", {"nom": "A", "vieille": "v"})
    url = f"/api/datastores/{ns}/rows/r-ancienne"
    r = client.patch(url, headers=_h(), json={"ville": "Lyon"})
    assert r.status_code == 200, r.text
    r = client.patch(url, headers=_h(), json={"vieille": "w"})
    assert r.status_code == 400 and r.json()["error"] == "unknown_column", r.text
    r = client.patch(url, headers=_h(), json={"vieille": None})
    assert r.status_code == 200, r.text
    ligne = _base(ns_id)[0]
    assert ligne.get("vieille") is None and ligne["ville"] == "Lyon"


def test_une_couche_mal_ecrite_est_reconnue(client):
    ns, _, _ = _table()
    r = client.post(f"/api/datastores/{ns}/rows", headers=_h(),
                    json={"nom": "A", "nom_comment": "source"})
    assert r.status_code == 400 and r.json()["error"] == "unknown_column", r.text
    assert r.json()["details"]["expected_column"] == "nom.comment"
    assert "LAYER whose name is written with a DOT" in r.json()["detail"]


def test_l_upload_et_l_import_sont_refuses_avant_la_premiere_ligne(live, monkeypatch):
    from oto_mcp import upload_tokens
    ns, ns_id, _ = _table()
    cible = {"kind": "datastore", "ns_id": ns_id, "namespace": ns, "format": "ndjson"}
    corps = b'{"nom": "A"}\n{"nom": "B", "age": 2}\n'
    with pytest.raises(upload_tokens.UploadError) as e:
        upload_tokens.materialize(SUB, cible, corps, "application/x-ndjson")
    assert (e.value.status, e.value.code) == (400, "unknown_column")
    monkeypatch.setattr(upload_tokens, "IMPORT_SLICE", 1)
    with pytest.raises(upload_tokens.UploadError) as e:
        upload_tokens.import_rows(SUB, cible, [{"nom": "A"}, {"nom": "B", "age": 2}],
                                  deadline=time.monotonic() + 60)
    assert (e.value.code, e.value.details["written"]) == ("unknown_column", 0)
    assert _base(ns_id) == []


def test_l_import_ndjson_declare_ses_cles_neuves_typees(live):
    """`oto_import` déclare les en-têtes CSV neufs ; le NDJSON déclare ses clés neuves,
    typées d'après leurs valeurs — la règle du gel (`types_inferes`)."""
    from oto_mcp import upload_tokens
    corps = (b'{"nom": "A", "age": 1, "actif": true, "vu": "2026-01-02"}\n'
             b'{"nom": "B", "age": "?", "x.comment": "c", "_id": "r"}\n')
    parsed = upload_tokens.parse_import(corps, "ndjson", SCHEMA, declare_columns=True)
    assert parsed["new_columns"] == [{"key": "age"}, {"key": "actif", "type": "bool"},
                                     {"key": "vu", "type": "date"}]
    assert upload_tokens.parse_import(corps, "ndjson", SCHEMA)["new_columns"] == []
