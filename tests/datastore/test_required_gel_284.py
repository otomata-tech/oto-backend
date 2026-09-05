"""Déclarer une colonne obligatoire rend les lignes existantes inécrivables — le dire
AU MOMENT où on la déclare (oto-backend#284).

⚠️ Ce que « gelée » veut dire, mesuré le 05/09/2026 : un tableau a 500 lignes, on
ajoute une colonne `required`, et les lignes qui ne la portent pas refusent DÈS LORS
toute écriture — sur n'importe quelle colonne. Le refus nomme le champ manquant, que
l'appelant n'essayait pas d'écrire : il cherche donc au mauvais endroit.

⚠️ Le contraste qui a décidé de la forme : `max_length` et `pattern` posés après coup
ne gèlent RIEN (ils ne jugent que les clés qu'un geste écrit), et pourtant ils
avertissent déjà. Le seul cran qui bloque vraiment était le seul muet.

Ce que la mesure a AUSSI établi, et qui corrige l'issue : le contrôle de TYPE ne gèle
pas (text→number, text→date passent), ni `max_length` réduit. Le cas réel est le champ
requis, et lui seul.
"""
from __future__ import annotations

import os
import uuid

import pytest


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


def _table(lignes=3):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = "t-" + uuid.uuid4().hex[:6]
    db.create_datastore_namespace("user", "sub-req", ns)
    st = make_store("sub-req")
    st.set_schema(ns, {"key": "ref", "fields": [{"key": "ref", "type": "text"},
                                                {"key": "email", "type": "text"}]})
    st.write_rows(ns, [{"ref": f"r{i}", "email": f"r{i}@x.invalid"}
                       for i in range(1, lignes + 1)])
    return st, ns


def test_la_pose_AVERTIT_et_compte_les_lignes_devenues_inecrivables(live):
    st, ns = _table(lignes=3)
    out = st.patch_schema(ns, fields=[{"key": "secteur", "type": "text",
                                       "required": True}])
    w = out.get("warning") or ""
    assert "`secteur` : 3 ligne(s)" in w, w
    assert "PLUS AUCUNE écriture" in w
    assert "required: null" in w, "l'avertissement doit dire comment revenir en arrière"


def test_l_avertissement_dit_le_PIEGE_pas_seulement_le_compte(live):
    """⚠️ Ce qui fait perdre du temps n'est pas le blocage, c'est que le refus nomme un
    champ que l'appelant n'écrivait pas. Le dire ici, c'est lui éviter de chercher du
    côté de la colonne qu'il visait."""
    st, ns = _table(lignes=1)
    w = st.patch_schema(ns, fields=[{"key": "secteur", "required": True}])["warning"]
    assert "sur aucune colonne" in w
    assert "pas celui qu'on essayait d'écrire" in w


def test_le_gel_ANNONCÉ_est_bien_celui_qui_se_produit(live):
    """L'avertissement ne vaut que s'il décrit ce qui arrive vraiment. On le vérifie
    dans la foulée : une écriture sur une AUTRE colonne est bien refusée, en nommant
    le champ requis."""
    st, ns = _table(lignes=1)
    st.patch_schema(ns, fields=[{"key": "secteur", "required": True}])
    rid = st.list_rows(ns)[0]["_id"]
    with pytest.raises(Exception) as e:
        st.update_row(ns, rid, {"email": "neuf@x.invalid"})
    assert "secteur" in str(e.value)


def test_pas_d_avertissement_quand_les_lignes_portent_deja_le_champ(live):
    """Un avertissement qu'on reçoit toujours cesse d'être lu — celui-ci ne se dit que
    sur des lignes réellement bloquées."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = "t-" + uuid.uuid4().hex[:6]
    db.create_datastore_namespace("user", "sub-req", ns)
    st = make_store("sub-req")
    st.set_schema(ns, {"key": "ref", "fields": [{"key": "ref", "type": "text"},
                                                {"key": "secteur", "type": "text"}]})
    st.write_rows(ns, [{"ref": "r1", "secteur": "industrie"}])
    out = st.patch_schema(ns, fields=[{"key": "secteur", "required": True}])
    assert "obligatoire" not in (out.get("warning") or "")


def test_une_valeur_VIDE_compte_comme_absente(live):
    """Sinon on annoncerait moins de lignes bloquées qu'il n'y en a : le contrôle de
    `required` refuse aussi la chaîne vide."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = "t-" + uuid.uuid4().hex[:6]
    db.create_datastore_namespace("user", "sub-req", ns)
    st = make_store("sub-req")
    st.set_schema(ns, {"key": "ref", "fields": [{"key": "ref", "type": "text"},
                                                {"key": "secteur", "type": "text"}]})
    st.write_rows(ns, [{"ref": "r1", "secteur": ""}])
    out = st.patch_schema(ns, fields=[{"key": "secteur", "required": True}])
    assert "`secteur` : 1 ligne(s)" in (out.get("warning") or "")
