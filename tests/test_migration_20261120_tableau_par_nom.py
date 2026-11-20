"""La révision `20261120_2210_tableau_par_nom` pose `idx_tool_calls_ref_nom`, et la mesure
de l'usage d'un nom de tableau (`index_releve.MESURE_TABLEAU_PAR_NOM`) passe par lui.

Un index CONCURRENTLY que le démarrage pose aussi (même déclaration, `TABLEAU_PAR_NOM`) :
la révision doit le poser VALIDE sur une base d'avant, ne rien faire sur une base que le
démarrage a déjà servie, et le retirer au retour arrière. La mesure compte les seules
désignations par nom, par face, org et principal. Joué sur une vraie base.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261119_1600_repar_consentie"
_REVISION = "20261120_2210_tableau_par_nom"


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _validite(dsn):
    """`None` si l'index est absent, sinon sa validité."""
    import psycopg

    from oto_mcp.db.index_releve import TABLEAU_PAR_NOM
    with psycopg.connect(dsn) as c:
        return c.execute(TABLEAU_PAR_NOM.sql_validite).fetchone()


def test_la_revision_est_declaree_au_registre_des_index_concurrents():
    from oto_mcp.db import index_concurrent as ic
    from oto_mcp.db.index_releve import REVISION_TABLEAU_PAR_NOM, TABLEAU_PAR_NOM

    assert REVISION_TABLEAU_PAR_NOM == _REVISION and len(_REVISION) <= 32
    assert ic.index_de_revision(_REVISION) == (TABLEAU_PAR_NOM,)
    assert f"index-concurrents {_REVISION}" in TABLEAU_PAR_NOM.procedure


def test_la_mesure_porte_le_predicat_de_l_index_mot_pour_mot():
    """Un index partiel ne sert que la requête qui IMPLIQUE son prédicat."""
    from oto_mcp.db.index_releve import (MESURE_TABLEAU_PAR_NOM,
                                         PREDICAT_TABLEAU_PAR_NOM, TABLEAU_PAR_NOM)

    assert TABLEAU_PAR_NOM.forme == f"(created_at DESC) WHERE {PREDICAT_TABLEAU_PAR_NOM}"
    assert f"WHERE {PREDICAT_TABLEAU_PAR_NOM} " in MESURE_TABLEAU_PAR_NOM
    # La clé lue est celle que le journal écrit, la valeur celle de la forme « nom ».
    from oto_mcp import session_org
    from oto_mcp.server import _TRACED_ARGS
    assert "ref_kind" in _TRACED_ARGS
    assert f"'{session_org.REF_NAME}'" in PREDICAT_TABLEAU_PAR_NOM


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    """Une base bootée, ramenée à l'état d'avant : index retiré, estampillée à la
    révision précédente."""
    import psycopg
    from alembic import command

    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("DROP INDEX idx_tool_calls_ref_nom")
    command.stamp(_alembic(), _PRECEDENTE)
    return pg_module_dsn


def test_la_revision_pose_l_index_valide_et_le_retour_arriere_le_retire(base_d_avant):
    from alembic import command

    assert _validite(base_d_avant) is None
    command.upgrade(_alembic(), _REVISION)
    assert _validite(base_d_avant) == (True,)
    command.downgrade(_alembic(), _PRECEDENTE)
    assert _validite(base_d_avant) is None
    command.upgrade(_alembic(), _REVISION)
    assert _validite(base_d_avant) == (True,)


def test_rejouee_sur_une_base_que_le_demarrage_a_servie_elle_ne_fait_rien(base_d_avant):
    """Ordre indifférent avec le code : le démarrage a pu poser l'index avant."""
    from alembic import command

    command.upgrade(_alembic(), _REVISION)
    command.stamp(_alembic(), _PRECEDENTE)
    command.upgrade(_alembic(), _REVISION)    # l'index est là, valide : rien à faire
    assert _validite(base_d_avant) == (True,)


def _appel(conn, *, kind, sub, org_id, forme, il_y_a_j=1, token_kind=None):
    args = {"ns_id": 204, "ref_kind": forme} if forme else {"ns_id": 204}
    conn.execute(
        "INSERT INTO tool_calls (server, sub, tool, args, ok, duration_ms, kind, org_id, "
        "token_kind, created_at) VALUES ('oto', %s, 'data_rows', %s, true, 1, %s, %s, %s, "
        "now() - make_interval(days => %s))",
        (sub, json.dumps(args), kind, org_id, token_kind, il_y_a_j))


def test_la_mesure_compte_les_noms_par_face_org_et_principal_par_l_index(live):
    from oto_mcp.db._conn import _connect
    from oto_mcp.db.index_releve import MESURE_TABLEAU_PAR_NOM, TABLEAU_PAR_NOM

    with _connect() as c:
        _appel(c, kind="mcp", sub="agent-a", org_id=41, forme="name")
        _appel(c, kind="mcp", sub="agent-a", org_id=41, forme="name")
        _appel(c, kind="rest", sub="integ-b", org_id=42, forme="name", token_kind="api")
        _appel(c, kind="mcp", sub="agent-a", org_id=41, forme="number")
        _appel(c, kind="mcp", sub="agent-c", org_id=41, forme="slot")
        _appel(c, kind="mcp", sub="agent-d", org_id=41, forme=None)        # d'avant la clé
        _appel(c, kind="mcp", sub="agent-e", org_id=43, forme="name", il_y_a_j=20)  # hors fenêtre
        c.commit()
        lignes = c.execute(MESURE_TABLEAU_PAR_NOM, {"jours": 7}).fetchall()
        assert [(r["face"], r["org_id"], r["sub"], r["token_kind"], r["appels"])
                for r in lignes] == [("mcp", 41, "agent-a", None, 2),
                                     ("rest", 42, "integ-b", "api", 1)]
        # Table minuscule : sans ce réglage, un parcours séquentiel l'emporte. Ce qu'on
        # vérifie, c'est que la mesure PEUT prendre l'index (son prédicat est impliqué).
        c.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(r["QUERY PLAN"] for r in c.execute(
            "EXPLAIN " + MESURE_TABLEAU_PAR_NOM, {"jours": 7}).fetchall())
        assert TABLEAU_PAR_NOM.nom in plan, plan
        c.rollback()
