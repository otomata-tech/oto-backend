"""`layers=nested` (oto#53) sur les DEUX faces servies, contre un vrai PostgreSQL.

Une colonne s'écrit `{"champ": {"valeur", "comment", "origine", "link"}}` et se relit à
plat (`champ` + `champ.comment`) — oto#47. Palier 1 : l'option `layers` rend la forme
écrite ; le défaut reste `flat`. Ce banc prouve quatre choses, sur la table de routes
réelle (`make_routes` + adaptateur) et sur le tool MCP monté (`register` + `.fn`) :

1. la MÊME ligne lue en `flat` et en `nested` porte le MÊME contenu — dans les deux
   sens (rien de plat sans place imbriquée, rien d'imbriqué sans son plat) ;
2. une cellule SANS couche est identique dans les deux formes (scalaire, liste) ;
3. une valeur inconnue est refusée en NOMMANT le paramètre et les formes admises ;
4. le défaut reste `flat` — épreuve SÉLECTIVE : elle tombe seule si quelqu'un bascule
   le défaut (palier 3), sans que les épreuves 1-3 ne bougent.
"""
from __future__ import annotations

import inspect
import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

LAYERS = ("origine", "comment", "link")


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@nested.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


SUB = "usr_nested"


def _h() -> dict:
    return {"Authorization": f"Bearer {SUB}"}


@pytest.fixture(scope="module")
def live(pg_dsn):
    import os

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_nested_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    previous_url, previous_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        from oto_mcp import db
        db.upsert_user(SUB, email=f"{SUB}@nested.invalid", name=SUB)
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = previous_pool
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


@pytest.fixture(scope="module")
def client(live):
    from oto_mcp.api import routes as api_routes
    return TestClient(Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None)))


# Une ligne qui a TOUT : une cellule à couches (valeur + comment), une cellule à couche
# sans valeur (import de socle : `origine` seule), une cellule scalaire, une liste de
# fiches dont un attribut porte une couche, une liste de scalaires.
LIGNE = {
    "siren": "552032534",
    "suivi": {"valeur": "a_traiter", "comment": "à rappeler"},
    "socle": {"origine": "import"},
    "ville": "Lyon",
    "contacts": [{"nom": "X", "email": {"valeur": "x@y.z", "link": "https://l"}},
                 {"nom": "Y", "email": "y@y.z"}],
    "tags": ["a", "b"],
}


@pytest.fixture(scope="module")
def table(live):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = "t-" + uuid.uuid4().hex[:6]
    db.create_datastore_namespace("user", SUB, ns)
    row = make_store(SUB).append_row(ns, dict(LIGNE))
    return ns, row["_id"]


def _est_couches(v) -> bool:
    return isinstance(v, dict) and "valeur" in v and set(v) <= {"valeur", *LAYERS}


def _meme_contenu(flat: dict, nested: dict) -> None:
    """Les deux formes portent le MÊME contenu — et dans les DEUX sens, parce qu'un
    contrat peut mentir des deux côtés : servir à plat ce que le nested tait, ou
    imbriquer ce que le plat n'a jamais servi."""
    # imbriqué → plat : chaque cellule imbriquée a son nom nu ET ses couches à plat.
    for k, v in nested.items():
        if _est_couches(v):
            assert flat[k] == v["valeur"], k
            for layer in LAYERS:
                if layer in v:
                    assert flat[f"{k}.{layer}"] == v[layer], (k, layer)
                else:
                    assert f"{k}.{layer}" not in flat, (k, layer)
        elif isinstance(v, list):
            assert len(flat[k]) == len(v), k
            for fi, ni in zip(flat[k], v):
                if isinstance(ni, dict):
                    _meme_contenu(fi, ni)
                else:
                    assert fi == ni, k
        else:
            assert flat[k] == v, k
    # plat → imbriqué : rien de servi à plat qui n'ait sa place imbriquée.
    for k, v in flat.items():
        base, _, layer = k.rpartition(".")
        if layer in LAYERS and base:
            assert nested[base][layer] == v, k
        else:
            assert k in nested, k


# ---------------------------------------------------------------- face REST

def test_REST_meme_ligne_en_flat_et_en_nested_meme_contenu(client, table):
    ns, rid = table
    url = f"/api/datastore/namespaces/{ns}/rows/{rid}"
    flat = client.get(url, headers=_h(), params={"layers": "flat"}).json()
    nested = client.get(url, headers=_h(), params={"layers": "nested"}).json()
    # La forme imbriquée est la forme ÉCRITE : ce qu'on a posé revient tel quel.
    assert nested["suivi"] == {"valeur": "a_traiter", "comment": "à rappeler"}
    assert nested["socle"] == {"valeur": None, "origine": "import"}
    assert nested["contacts"][0]["email"] == {"valeur": "x@y.z", "link": "https://l"}
    # Et le plat est resté ce qu'il était.
    assert flat["suivi"] == "a_traiter" and flat["suivi.comment"] == "à rappeler"
    assert flat["socle"] is None and flat["socle.origine"] == "import"
    _meme_contenu(flat, nested)


def test_REST_cellule_sans_couche_identique_dans_les_deux_formes(client, table):
    ns, rid = table
    url = f"/api/datastore/namespaces/{ns}/rows/{rid}"
    flat = client.get(url, headers=_h(), params={"layers": "flat"}).json()
    nested = client.get(url, headers=_h(), params={"layers": "nested"}).json()
    for k in ("siren", "ville", "tags", "_id", "_created_at", "_updated_at"):
        assert flat[k] == nested[k], k
    assert nested["ville"] == "Lyon"           # un scalaire, pas `{"valeur": "Lyon"}`
    assert nested["contacts"][1]["email"] == "y@y.z"


def test_REST_la_liste_sert_la_meme_forme_que_la_fiche(client, table):
    ns, rid = table
    url = f"/api/datastore/namespaces/{ns}/rows"
    flat = client.get(url, headers=_h(), params={"layers": "flat"}).json()["rows"]
    nested = client.get(url, headers=_h(), params={"layers": "nested"}).json()["rows"]
    assert [r["_id"] for r in flat] == [r["_id"] for r in nested] == [rid]
    _meme_contenu(flat[0], nested[0])
    assert nested[0]["suivi"] == {"valeur": "a_traiter", "comment": "à rappeler"}


@pytest.mark.parametrize("route", ["rows", "rows/{rid}"])
def test_REST_valeur_inconnue_refusee_en_nommant_le_parametre(client, table, route):
    ns, rid = table
    r = client.get(f"/api/datastore/namespaces/{ns}/" + route.format(rid=rid),
                   headers=_h(), params={"layers": "plat"})
    assert r.status_code == 400, r.text
    corps = r.json()
    assert corps.get("error") == "invalid_layers", corps
    detail = corps.get("detail") or corps.get("message") or r.text
    for mot in ("layers", "plat", "flat", "nested"):
        assert mot in detail, (mot, detail)


def test_REST_la_suppression_ne_prend_pas_layers(client, table):
    """`layers` ne vit que sur les lectures : l'`Input` partagé de la suppression ne
    l'a pas reçu, donc la garde de champ inconnu le refuse là."""
    ns, rid = table
    r = client.delete(f"/api/datastore/namespaces/{ns}/rows/{rid}", headers=_h(),
                      params={"layers": "nested"})
    assert r.status_code == 400 and r.json().get("error") == "unknown_fields", r.text
    # Et la ligne est toujours là (le refus a précédé toute écriture).
    assert client.get(f"/api/datastore/namespaces/{ns}/rows/{rid}",
                      headers=_h()).status_code == 200


def test_REST_le_defaut_reste_flat(client, table):
    """SÉLECTIF : c'est CE test qui tombe si quelqu'un bascule le défaut (palier 3)."""
    ns, rid = table
    sans = client.get(f"/api/datastore/namespaces/{ns}/rows/{rid}", headers=_h()).json()
    assert isinstance(sans["suivi"], str) and sans["suivi.comment"] == "à rappeler", sans
    page = client.get(f"/api/datastore/namespaces/{ns}/rows", headers=_h()).json()["rows"]
    assert isinstance(page[0]["suivi"], str) and "suivi.comment" in page[0]


# ---------------------------------------------------------------- face MCP

@pytest.fixture(scope="module")
def data_rows(live):
    import asyncio

    from fastmcp import FastMCP

    from oto_mcp.tools import datastore as tools_ds
    mcp = FastMCP("test")
    tools_ds.register(mcp)
    return asyncio.run(mcp.get_tool("data_rows")).fn


@pytest.fixture
def acteur(monkeypatch):
    from oto_mcp import access
    monkeypatch.setattr(access, "current_user_sub_from_token", lambda: SUB)


def test_MCP_meme_ligne_en_flat_et_en_nested_meme_contenu(data_rows, table, acteur):
    ns, rid = table
    flat = data_rows(namespace=ns, id=rid, layers="flat")
    nested = data_rows(namespace=ns, id=rid, layers="nested")
    assert nested["suivi"] == {"valeur": "a_traiter", "comment": "à rappeler"}
    assert nested["ville"] == "Lyon"
    _meme_contenu(flat, nested)
    # En liste aussi, et une projection garde la cellule imbriquée ENTIÈRE.
    page = data_rows(namespace=ns, layers="nested")
    _meme_contenu(data_rows(namespace=ns)["rows"][0], page["rows"][0])
    projete = data_rows(namespace=ns, layers="nested", fields=["suivi"])["rows"][0]
    assert projete == {"_id": rid, "suivi": {"valeur": "a_traiter", "comment": "à rappeler"}}


def test_MCP_valeur_inconnue_refusee_en_nommant_le_parametre(data_rows, table, acteur):
    from oto_mcp.mcp_errors import McpError
    ns, rid = table
    with pytest.raises(McpError) as exc:
        data_rows(namespace=ns, id=rid, layers="plat")
    for mot in ("layers", "plat", "flat", "nested"):
        assert mot in str(exc.value), (mot, str(exc.value))


def test_MCP_le_defaut_reste_flat(data_rows, table, acteur):
    """SÉLECTIF, comme son jumeau REST — et le témoin DIRECT sur les signatures : le
    défaut est UN nom (`layers.DEFAUT`) que les deux faces recopient."""
    from oto_mcp.capabilities.datastore.rows import GetRowInput, ListRowsInput
    from oto_mcp.datastore import layers as dsl
    ns, rid = table
    sans = data_rows(namespace=ns, id=rid)
    assert isinstance(sans["suivi"], str) and sans["suivi.comment"] == "à rappeler", sans
    assert dsl.DEFAUT == "flat"
    assert inspect.signature(data_rows).parameters["layers"].default == "flat"
    assert ListRowsInput.model_fields["layers"].default == "flat"
    assert GetRowInput.model_fields["layers"].default == "flat"
