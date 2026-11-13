"""`resume_des_declencheurs` (ce que la page Agents trace) sur un VRAI PostgreSQL.

La clé `trigger_id` vit dans `payload`, un JSON libre : elle est lue en TEXTE et
convertie côté Python, jamais castée en SQL (`test_runs_surface`). Ce banc prouve que
la requête tient quand l'org porte une ligne dont la clé n'est pas un nombre — le cas
qui ferait tomber la lecture ENTIÈRE si la clé était castée — et que l'appelant reçoit
bien des entiers.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def base(pg_module_dsn):
    psycopg = pytest.importorskip("psycopg")
    url_prec = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = pg_module_dsn
    try:
        from oto_mcp.db import init_db
        init_db()
        with psycopg.connect(pg_module_dsn, autocommit=True) as c:
            c.execute("DELETE FROM runner_jobs")
            for payload, statut, erreur in (
                ('{"trigger_id": "5"}', "done", None),
                ('{"trigger_id": "5"}', "failed", "boom"),
                ('{"trigger_id": "7"}', "done", None),
                ('{"trigger_id": "pas-un-nombre"}', "failed", "x"),
                ("{}", "done", None),
            ):
                c.execute(
                    "INSERT INTO runner_jobs (org_id, kind, status, due_at, attempts, "
                    "max_attempts, payload, last_error) "
                    "VALUES (1, 'start', %s, NOW() - interval '1 hour', 1, 3, %s::jsonb, %s)",
                    (statut, payload, erreur))
        yield
    finally:
        if url_prec is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_prec


def test_le_resume_lit_la_cle_en_texte_et_rend_des_entiers(base):
    from oto_mcp.db import runner_jobs as RJ
    cases, pannes = RJ.resume_des_declencheurs(1, 7)
    assert {(c["trigger_id"], c["status"], c["n"]) for c in cases} == {
        (5, "done", 1), (5, "failed", 1), (7, "done", 1)}
    assert all(isinstance(c["trigger_id"], int) for c in cases)
    assert [(p["trigger_id"], p["last_error"]) for p in pannes] == [(5, "boom")]


# ── L'index des travaux d'un déclencheur (révision 0067) ─────────────────────

def test_les_trois_lectures_portent_le_predicat_de_leur_index():
    """Un index partiel ne sert que la requête qui IMPLIQUE son prédicat : le résumé
    (cases, pannes) et le temps gagné filtrent par `PREDICAT_DECLENCHEUR`, celui de
    l'index, et le code n'en écrit aucune autre copie."""
    import inspect

    from oto_mcp.db import runner_jobs as RJ

    assert RJ.INDEX_DECLENCHEURS.forme == f"(org_id, due_at) WHERE {RJ.PREDICAT_DECLENCHEUR}"
    assert inspect.getsource(RJ.resume_des_declencheurs).count("{PREDICAT_DECLENCHEUR}") == 2
    # Le temps gagné lit ses travaux par UN seul fragment, qui porte le prédicat, dans
    # chacune de ses trois requêtes.
    assert inspect.getsource(RJ).count("AND {PREDICAT_DECLENCHEUR}\n           AND {_AGENT_DU_TRAVAIL}") == 1
    assert inspect.getsource(RJ.formes_des_runs_conclus).count("{_TRAVAUX_CONCLUS}") == 3
    assert inspect.getsource(RJ).count("~ '^[0-9]+$'\"") == 1


def test_la_revision_0067_pose_l_index_des_declencheurs():
    import importlib.util
    from pathlib import Path

    from oto_mcp.db import index_concurrent as ic
    from oto_mcp.db.runner_jobs import INDEX_DECLENCHEURS, REVISION_DECLENCHEURS

    (chemin,) = Path("oto_mcp/db/migrations/versions").glob("*_0067_*.py")
    spec = importlib.util.spec_from_file_location("rev_0067", chemin)
    rev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rev)
    assert rev.revision == REVISION_DECLENCHEURS and len(rev.revision) <= 32
    assert rev.down_revision == "0066_orgs_time_saved_minutes"
    assert ic.index_de_revision("0067") == (INDEX_DECLENCHEURS,)
    assert "index-concurrents 0067_runner_jobs_declencheurs" in INDEX_DECLENCHEURS.procedure


def test_les_lectures_d_une_org_chargee_passent_par_l_index(base, monkeypatch):
    """Contre une vraie base : 20 000 travaux de flotte (sans clé) dans l'org, quelques
    travaux d'agent. Le démarrage a posé l'index, valide, et les cinq lectures du code
    — capturées telles quelles, puis expliquées — le prennent au lieu de balayer la file."""
    from contextlib import contextmanager

    from oto_mcp.db import index_concurrent as ic
    from oto_mcp.db import runner_jobs as RJ
    from oto_mcp.db._conn import _connect

    lues: list = []

    class _Capture:
        def execute(self, sql, params):
            lues.append((sql, params))
            # Un couple d'outils au journal, pour que la lecture du temps gagné passe
            # aussi par sa requête de compte.
            rows = [{"tool": "serper_search", "op": None}] if "SELECT DISTINCT tc.tool" in sql else []
            return type("C", (), {"fetchall": lambda _s: rows})()

    @contextmanager
    def _capture():
        yield _Capture()

    monkeypatch.setattr(RJ, "_connect", _capture)
    RJ.resume_des_declencheurs(1, 28)
    RJ.formes_des_runs_conclus(1, 28, [5], lambda outil, op: ("research", outil))
    monkeypatch.undo()
    assert len(lues) == 5

    with _connect() as c:
        assert ic.scalaire_de(c)(RJ.INDEX_DECLENCHEURS.sql_validite) is True
        c.execute(
            "INSERT INTO runner_jobs (org_id, kind, status, due_at, attempts, max_attempts, payload) "
            "SELECT 1, 'start', 'done', NOW() - g * interval '1 minute', 1, 3, '{\"fleet\": 1}'::jsonb "
            "FROM generate_series(1, 20000) g")
        c.execute("ANALYZE runner_jobs")
        for sql, params in lues:
            plan = "\n".join(r["QUERY PLAN"] for r in c.execute("EXPLAIN " + sql, params).fetchall())
            assert RJ.INDEX_DECLENCHEURS.nom in plan and "Seq Scan" not in plan, plan
        c.rollback()


def test_le_resume_rend_les_dates_en_texte(base):
    """Le pool normalise les dates en texte (`_str_dict_row`) : la capacité les sert
    telles quelles. Un `.isoformat()` dessus a rendu `op=summary` en 500 partout."""
    from oto_mcp.db import runner_jobs as RJ
    cases, pannes = RJ.resume_des_declencheurs(1, 7)
    assert cases and all(isinstance(c["heure"], str) for c in cases)
    assert pannes and all(isinstance(p["due_at"], str) for p in pannes)
