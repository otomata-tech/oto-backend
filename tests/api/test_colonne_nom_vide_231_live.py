"""Un nom de colonne vide est refusé sur TOUTES les faces d'écriture (oto#231).

Relevé au labo : `PATCH …/rows/{id}` avec `{"": "x"}` rendait `200` et la clé `""`
entrait dans les données de la ligne. Une colonne au nom vide ne se déclare pas au
schéma, ne s'adresse par aucun filtre et ne se lit proprement nulle part : elle ne doit
pas naître. Même chose pour un nom fait seulement d'espaces.

Le refus vit à UN endroit — la validation des noms de colonne (`points._refuse_dotted_
names`), dont la sonde de `test_lot_refuse_cles_pointees.py` exige l'appel devant
chaque porte d'écriture en base. Ce banc joue chaque face servie, sur une vraie base, et
lit le STOCKAGE pour établir que rien n'est écrit.
"""
from __future__ import annotations

import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

SUB = "usr_nom_vide"
NOMS_VIDES = ("", "   ")


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@vide.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


def _h() -> dict:
    return {"Authorization": f"Bearer {SUB}"}


@pytest.fixture(scope="module")
def base(live):
    from oto_mcp import db
    db.upsert_user(SUB, email=f"{SUB}@vide.invalid", name=SUB)


@pytest.fixture(scope="module")
def client(base):
    from oto_mcp.api import routes as api_routes
    return TestClient(Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None)))


@pytest.fixture
def table(base):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = "vide-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", SUB, ns)
    ns = str(ns_id)
    st = make_store(SUB)
    st.set_schema(ns, {"fields": [{"key": "siren"}, {"key": "statut"}]})
    row = st.append_row(ns, {"siren": "552032534"})
    return ns, ns_id, row["_id"]


def _colonnes(ns_id: int) -> set:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        lignes = conn.execute("SELECT data FROM datastore_rows WHERE ns_id = %s",
                              (ns_id,)).fetchall()
    return {k for ligne in lignes for k in (ligne["data"] or {})}


def _sans_nom_vide(ns_id: int) -> None:
    assert not [c for c in _colonnes(ns_id) if not c.strip()], "rien n'est écrit"


async def _data_write(monkeypatch):
    """Le tool MCP tel qu'il est MONTÉ, pas le store appelé à la main."""
    from fastmcp import FastMCP

    from oto_mcp import access
    from oto_mcp.tools import datastore as tools_ds
    monkeypatch.setattr(access, "current_user_sub_from_token", lambda: SUB)
    mcp = FastMCP("test")
    tools_ds.register(mcp)
    return (await mcp.get_tool("data_write")).fn


# ── face agent : data_write, ligne, par id, en lot ──────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("nom", NOMS_VIDES)
async def test_data_write_refuse_le_nom_vide_sur_ses_trois_gestes(table, monkeypatch, nom):
    from oto_mcp.mcp_errors import McpError
    data_write = await _data_write(monkeypatch)
    ns, ns_id, rid = table
    gestes = (
        {"row": {"siren": "301234567", nom: "x"}},
        {"id": rid, "row": {nom: "x"}},
        {"rows": [{"siren": "301234567", nom: "x"}]},
    )
    for geste in gestes:
        with pytest.raises(McpError) as e:
            data_write(datastore=ns, **geste)
        assert "is not a column name" in str(e.value), (geste, str(e.value))
        assert repr(nom) in str(e.value), "le refus cite la clé fautive"
    _sans_nom_vide(ns_id)


# ── face REST : POST ligne, PATCH ligne, lot ────────────────────────────────

@pytest.mark.parametrize("nom", NOMS_VIDES)
def test_REST_refuse_le_nom_vide_par_un_refus_declare(client, table, nom):
    ns, ns_id, rid = table
    appels = (
        ("POST", f"/api/datastores/{ns}/rows", {"siren": "301234567", nom: "x"}),
        ("PATCH", f"/api/datastores/{ns}/rows/{rid}", {nom: "x"}),
        ("POST", f"/api/datastores/{ns}/rows/batch",
         {"rows": [{"siren": "301234567", nom: "x"}]}),
    )
    for verbe, chemin, corps in appels:
        r = client.request(verbe, chemin, headers=_h(), json=corps)
        assert r.status_code == 400, (verbe, chemin, r.text)
        assert r.json().get("error") == "row_invalid", r.text
        assert "is not a column name" in r.json().get("detail", ""), r.text
    _sans_nom_vide(ns_id)


def test_le_refus_est_declare_au_contrat_de_chaque_route_REST():
    from oto_mcp.capabilities import registry
    for cle in ("me.datastore.append_row", "me.datastore.update_row",
                "me.datastore.write_rows"):
        cap = next(c for c in registry.CAPABILITIES if c.key == cle)
        assert (400, "row_invalid") in {(e.status, e.code) for e in cap.errors}, cle


# ── dépôt de fichier : l'import NDJSON passe par le lot ─────────────────────

@pytest.mark.parametrize("nom", NOMS_VIDES)
def test_le_depot_de_fichier_refuse_le_nom_vide(table, nom):
    import json
    import time

    from oto_mcp.upload_tokens import UploadError, import_rows, parse_import
    ns, ns_id, _ = table
    corps = json.dumps({"siren": "301234567", nom: "x"}).encode()
    lignes = parse_import(corps, "ndjson")["rows"]
    with pytest.raises(UploadError) as e:
        import_rows(SUB, {"kind": "datastore", "ns_id": ns_id}, lignes,
                    deadline=time.monotonic() + 30)
    assert e.value.code == "bad_row" and "is not a column name" in str(e.value)
    _sans_nom_vide(ns_id)


def test_un_nom_ordinaire_passe_toujours(client, table):
    """Le témoin négatif : on ferme le nom vide, pas l'écriture."""
    ns, ns_id, rid = table
    r = client.patch(f"/api/datastores/{ns}/rows/{rid}", headers=_h(),
                     json={"statut": "x"})
    assert r.status_code == 200, r.text
    assert "statut" in _colonnes(ns_id)
