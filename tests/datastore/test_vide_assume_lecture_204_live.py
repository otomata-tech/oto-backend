"""Une ligne au vide ASSUMÉ, posée à la main, sur toutes les faces servies (oto#204, étape 1).

L'étape 1 ne sait pas écrire le marqueur : ce banc le pose DIRECTEMENT en base, comme
l'étape 2 le fera, pour prouver que la version qui part en production avant elle le lit
sans le montrer, le valide, le garde quand on renvoie le vide servi, le perd quand on écrit
une vraie valeur, et ne compte pas la ligne comme bloquée. Faces : le store, REST (`flat` et
`nested`, fiche et page — l'export CSV du tableau de bord est bâti sur elles), l'outil MCP
`data_rows`, et la réservation `claim_next`.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

SUB = "usr_vide_assume"
MARQUEUR = "oto.vide_assume"
MARQUEE = {"valeur": "", MARQUEUR: True}
SCHEMA = {"strict": True, "fields": [
    {"key": "raison", "type": "text"},
    {"key": "fonction", "type": "text", "required": True},
    {"key": "contacts", "type": "list", "of": {"type": "object", "fields": [
        {"key": "nom", "type": "text"},
        {"key": "fonction", "type": "text", "required": True}]}},
]}


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@vide.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


def _h() -> dict:
    return {"Authorization": f"Bearer {SUB}"}


def _aucune_trace(obj) -> None:
    assert "vide_assume" not in json.dumps(obj, ensure_ascii=False, default=str), (
        "le marqueur interne FUIT dans ce qui est servi")


@pytest.fixture(scope="module")
def live(pg_dsn):
    import os
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn
    name = "oto_vide_" + uuid.uuid4().hex[:8]
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
        db.upsert_user(SUB, email=f"{SUB}@vide.invalid", name=SUB)
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


@pytest.fixture(scope="module")
def data_rows(live):
    from fastmcp import FastMCP

    from oto_mcp.tools import datastore as tools_ds
    mcp = FastMCP("test")
    tools_ds.register(mcp)
    return asyncio.run(mcp.get_tool("data_rows")).fn


@pytest.fixture
def acteur(monkeypatch):
    from oto_mcp import access
    monkeypatch.setattr(access, "current_user_sub_from_token", lambda: SUB)


def _poser_a_la_main(ns_id: int, row_id: str, data: dict) -> None:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("UPDATE datastore_rows SET data = %s::jsonb "
                     "WHERE ns_id = %s AND row_id = %s", (json.dumps(data), ns_id, row_id))


def _stockee(ns_id: int, row_id: str) -> dict:
    from oto_mcp import db
    return db.datastore_get_row(ns_id, row_id)["data"]


@pytest.fixture
def ligne_marquee(live):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = "t204-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", SUB, ns)
    st = make_store(SUB)
    st.set_schema(ns, SCHEMA)
    rid = st.append_row(ns, {"raison": "ACME", "fonction": "DG",
                             "contacts": [{"nom": "X", "fonction": "CEO"}]})["_id"]
    _poser_a_la_main(ns_id, rid, {"raison": "ACME", "fonction": MARQUEE,
                                  "contacts": [{"nom": "X", "fonction": MARQUEE}]})
    return st, ns, ns_id, rid


# ── (b) et (c) : lue, validée, servie sans fuite ─────────────────────────────

@pytest.mark.parametrize("layers", ["flat", "nested"])
def test_le_store_sert_le_vide_sans_fuite(ligne_marquee, layers):
    st, ns, _, rid = ligne_marquee
    ligne = st.get_row(ns, rid, layers=layers)
    assert ligne["fonction"] == "" and ligne["contacts"][0]["fonction"] == ""
    _aucune_trace(ligne)
    _aucune_trace(st.list_rows(ns))


@pytest.mark.parametrize("layers", ["flat", "nested"])
def test_REST_fiche_et_page_servent_le_vide_sans_fuite(client, ligne_marquee, layers):
    _, ns, _, rid = ligne_marquee
    fiche = client.get(f"/api/datastores/{ns}/rows/{rid}", headers=_h(),
                       params={"layers": layers})
    page = client.get(f"/api/datastores/{ns}/rows", headers=_h(), params={"layers": layers})
    assert fiche.status_code == 200 and page.status_code == 200, (fiche.text, page.text)
    assert fiche.json()["fonction"] == ""
    _aucune_trace(fiche.json()), _aucune_trace(page.json())


@pytest.mark.parametrize("layers", ["flat", "nested"])
def test_MCP_data_rows_sert_le_vide_sans_fuite(data_rows, ligne_marquee, acteur, layers):
    _, ns, _, rid = ligne_marquee
    fiche = data_rows(datastore=ns, id=rid, layers=layers)
    assert fiche["fonction"] == ""
    _aucune_trace(fiche)
    _aucune_trace(data_rows(datastore=ns, layers=layers))


def test_la_reservation_sert_le_vide_sans_fuite(ligne_marquee):
    st, ns, _, rid = ligne_marquee
    reservee = st.claim_next(ns, worker="w-204")
    assert reservee is not None and reservee["_id"] == rid
    assert reservee["fonction"] == ""
    _aucune_trace(reservee)


def test_une_ligne_marquee_n_est_pas_comptee_bloquee(ligne_marquee):
    from oto_mcp import db
    st, ns, ns_id, _ = ligne_marquee
    assert db.datastore_rows_missing_required(ns_id, ["fonction"]) == []
    autre = st.append_row(ns, {"raison": "BETA", "fonction": "DG",
                               "contacts": [{"nom": "Y", "fonction": "CEO"}]})["_id"]
    _poser_a_la_main(ns_id, autre, {"raison": "BETA", "fonction": "",
                                    "contacts": [{"nom": "Y", "fonction": "CEO"}]})
    assert db.datastore_rows_missing_required(ns_id, ["fonction"]) == [
        {"field": "fonction", "rows": 1}], "un vide ordinaire doit rester compté"


# ── l'écriture autour d'une ligne marquée ────────────────────────────────────

def test_ecrire_une_autre_colonne_passe_et_garde_le_marqueur(ligne_marquee):
    st, ns, ns_id, rid = ligne_marquee
    st.update_row(ns, rid, {"raison": "ACME SAS"})
    assert _stockee(ns_id, rid)["fonction"] == MARQUEE


def test_renvoyer_le_vide_servi_d_une_colonne_garde_le_marqueur(ligne_marquee):
    st, ns, ns_id, rid = ligne_marquee
    servie = st.get_row(ns, rid)
    st.update_row(ns, rid, {"raison": servie["raison"], "fonction": servie["fonction"]})
    assert _stockee(ns_id, rid)["fonction"] == MARQUEE


def test_renvoyer_une_liste_SANS_cle_d_element_est_refuse_sans_perte(ligne_marquee):
    """⚠️ LIMITE ASSUMÉE. Une liste sans `of.key` n'a pas d'identité d'élément : écrite,
    elle REMPLACE la liste en bloc (#120). Le `""` servi de son sous-champ redevient donc un
    vide ordinaire, qui ne satisfait pas `required` : l'écriture est refusée, bruyamment,
    et rien ne change en base. Sur une liste À clé, la fusion se fait élément par élément
    et le marqueur est gardé (banc sans base). Pour réécrire un vide assumé dans une liste
    sans clé, le geste est `@empty` (étape 2)."""
    from oto_mcp.datastore.errors import RowValidationError
    st, ns, ns_id, rid = ligne_marquee
    avant = _stockee(ns_id, rid)
    servie = st.get_row(ns, rid)
    with pytest.raises(RowValidationError) as e:
        st.update_row(ns, rid, {k: v for k, v in servie.items() if not k.startswith("_")})
    assert "contacts[0].fonction" in str(e.value) and "requis" in str(e.value)
    assert _stockee(ns_id, rid) == avant, "un refus a laissé une trace en base"


def test_ecrire_une_vraie_valeur_fait_tomber_le_marqueur(ligne_marquee):
    st, ns, ns_id, rid = ligne_marquee
    st.update_row(ns, rid, {"fonction": "Directrice"})
    assert _stockee(ns_id, rid)["fonction"] == "Directrice"


@pytest.mark.parametrize("chemin", ["update_row", "append_row", "write_rows"])
def test_un_client_qui_ecrit_la_cle_interne_est_refuse_sur_chaque_chemin(ligne_marquee, chemin):
    from oto_mcp.datastore.errors import RowValidationError
    st, ns, ns_id, rid = ligne_marquee
    avant = _stockee(ns_id, rid)
    corps = {"raison": "PIRATE", "fonction": MARQUEE, "contacts": [{"nom": "Z", "fonction": "CEO"}]}
    with pytest.raises((RowValidationError, ValueError)) as e:
        if chemin == "update_row":
            st.update_row(ns, rid, corps)
        elif chemin == "append_row":
            st.append_row(ns, corps)
        else:
            st.write_rows(ns, [corps])
    assert "@empty" in str(e.value)
    assert _stockee(ns_id, rid) == avant


def test_sans_marqueur_un_vide_ordinaire_reste_refuse_comme_avant(ligne_marquee):
    from oto_mcp.datastore.errors import RowValidationError
    st, ns, _, _ = ligne_marquee
    with pytest.raises(RowValidationError) as e:
        st.append_row(ns, {"raison": "GAMMA", "fonction": "",
                           "contacts": [{"nom": "Z", "fonction": "CEO"}]})
    assert "requis" in str(e.value)
