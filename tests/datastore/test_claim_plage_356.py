"""Une plage bornée devient exprimable dans une réservation (oto-backend#356).

`filter` accepte **un seul opérateur par colonne** — c'est un refus explicite de
`ds_filter_specs`. Une file bornée des deux côtés (`score >= 10 ET score <= 20`) y était
donc inexprimable, et le contournement documenté — ne poser qu'une borne — sert des
lignes hors plage à un worker qui les traite quand même.

⚠️ Rien de neuf en dessous : `db.datastore_claim_next` prend `filters` depuis toujours,
et `_filter_clauses` réunit déjà les deux formes. **Ce qui manquait était le chemin** —
exactement comme `layers` ce matin : la réservation ne portait pas ce que les lectures
servaient depuis longtemps. Deux fois le même jour, sur le même geste : le chemin
qu'un agent emprunte pour travailler est celui qu'on outille en dernier.
"""
from __future__ import annotations

import inspect
import os
import uuid

import pytest


def test_les_deux_faces_portent_filters():
    """Le store, la face agent et la face REST — les trois, sinon l'une d'elles
    resterait le chemin pauvre qu'on vient de corriger."""
    from oto_mcp.capabilities.datastore.claim import ClaimNextInput
    from oto_mcp.datastore.core import DatastorePg
    from oto_mcp.tools import datastore as tools_ds

    assert "filters" in inspect.signature(DatastorePg.claim_next).parameters
    assert "filters" in ClaimNextInput.model_fields
    assert "filters: Optional[list] = None" in inspect.getsource(tools_ds)


def test_filter_refuse_toujours_deux_operateurs_sur_une_colonne():
    """La raison d'être du lot, gravée : si ce refus disparaissait, `filters` perdrait
    son objet — et ce banc dirait qu'il faut rouvrir la question, pas le supprimer."""
    from oto_mcp.db.query import ds_filter_specs

    with pytest.raises(ValueError) as e:
        ds_filter_specs({"score": {"gte": 10, "lte": 20}})
    assert "un seul opérateur par colonne" in str(e.value)


@pytest.fixture(scope="module")
def live(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    nom = "oto_org_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{nom}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + nom
    url_avant, pool_avant = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = pool_avant
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant
        root.execute(f'DROP DATABASE IF EXISTS "{nom}" WITH (FORCE)')
        root.close()


def _table():
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store

    ns = "t-" + uuid.uuid4().hex[:6]
    db.create_datastore_namespace("user", "sub-356", ns)
    st = make_store("sub-356")
    st.set_schema(ns, {"key": "ref", "fields": [{"key": "ref", "type": "text"},
                                                {"key": "score", "type": "number"}]})
    st.write_rows(ns, [{"ref": f"r{i}", "score": i * 10} for i in range(1, 6)])
    return st, ns


def test_une_PLAGE_ne_sert_que_les_lignes_dedans(live):
    """Le chemin réel : deux bornes, et le worker ne reçoit que ce qu'il a demandé."""
    st, ns = _table()
    plage = [{"field": "score", "op": "gte", "value": 20},
             {"field": "score", "op": "lte", "value": 40}]

    servis = []
    for i in range(4):
        row = st.claim_next(ns, worker=f"w{i}", filters=plage)
        if row is None:
            break
        servis.append(row["score"])
    assert sorted(servis) == [20, 30, 40], servis


def test_le_contournement_d_UNE_borne_sert_bien_des_lignes_HORS_plage(live):
    """⚠️ Ce que le lot remplace, mesuré plutôt qu'affirmé : avec une seule borne, le
    worker reçoit des lignes qu'il n'a pas demandées — et il les traite, puisque rien
    ne lui dit qu'elles sont hors plage."""
    st, ns = _table()
    row = st.claim_next(ns, worker="w0", filter={"score": {"gte": 20}})
    assert row is not None
    # 50 est hors de la plage voulue [20, 40] et reste servi par une borne seule.
    servis = [row["score"]]
    for i in range(1, 4):
        suivant = st.claim_next(ns, worker=f"w{i}", filter={"score": {"gte": 20}})
        if suivant is None:
            break
        servis.append(suivant["score"])
    assert 50 in servis, servis


def test_filters_et_filter_se_CUMULENT(live):
    """Les deux formes en ET, comme sur les lectures — pas l'une ou l'autre."""
    st, ns = _table()
    row = st.claim_next(ns, worker="w0", filter={"ref": "r3"},
                        filters=[{"field": "score", "op": "gte", "value": 20}])
    assert row is not None and row["ref"] == "r3"


def test_le_perimetre_du_TABLEAU_passe_toujours_devant(live):
    """⚠️ L'invariant à ne pas casser : le périmètre déclaré au tableau resserre, et
    un filtre d'appelant ne l'élargit jamais. Ajouter `filters` ne devait pas ouvrir
    une porte à côté."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store

    ns = "t-" + uuid.uuid4().hex[:6]
    db.create_datastore_namespace("user", "sub-356", ns)
    st = make_store("sub-356")
    st.set_schema(ns, {"key": "ref", "fields": [
        {"key": "ref", "type": "text"},
        {"key": "score", "type": "number"},
        {"key": "statut", "type": "text", "role": "status",
         "values": ["a_faire", "fait"],
         "lifecycle": {"states": ["a_faire", "fait"],
                       "transitions": {"a_faire": ["fait"]},
                       "claimable": {"statut": "a_faire"}}}]})
    st.write_rows(ns, [{"ref": "r1", "score": 10, "statut": "fait"},
                       {"ref": "r2", "score": 20, "statut": "a_faire"}])

    # `filters` vise r1 (score 10) — mais le périmètre du tableau l'exclut.
    row = st.claim_next(ns, worker="w0",
                        filters=[{"field": "score", "op": "lte", "value": 10}])
    assert row is None, row
