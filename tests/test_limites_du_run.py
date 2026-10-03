"""Les limites d'UN run déclarées sur l'agent : `max_tokens`, `max_run_seconds`.

Ce qui se prouve : la fourchette refusée à la pose (et nommée), `0` qui RETIRE une limite
(écrit NULL, jamais 0), rien qui voyage quand rien n'est déclaré — l'exécuteur garde
alors ses défauts —, et les colonnes réellement écrites et relues en base.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from oto_mcp import runner_tick
from oto_mcp.capabilities import _limites_du_run as L
from oto_mcp.capabilities import runner_fleets as RF
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx



@pytest.fixture(autouse=True)
def _l_appelant_possede_l_agent(monkeypatch):
    """Ce fichier ne parle pas du PARTAGE d'agents — il a son banc
    (`test_partage_agents.py`). L'appelant y est lu propriétaire de tout agent
    qu'on lui présente ; sans cette doublure, la lecture de rôle irait chercher la
    vraie base."""
    from oto_mcp.capabilities import _acces_agent
    monkeypatch.setattr(_acces_agent, "niveaux",
                        lambda sub, org_id, agents: {int(t["id"]): "owner" for t in agents})

@pytest.fixture(autouse=True)
def _cle_de_modele_non_exigee(monkeypatch):
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda *a, **k: None)


def _ctx():
    return ResolvedCtx(sub="alexis", org_id=2)


# ── la fourchette ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tokens, secondes", [
    (-1, None), (None, 30), (None, 59), (None, 3601), (None, -60)])
def test_une_limite_hors_fourchette_est_refusee_et_nommee(tokens, secondes):
    with pytest.raises(AuthzDenied) as e:
        L.valider(tokens, secondes)
    assert (e.value.status, e.value.code) == (400, "invalid_bound")
    nom = "max_tokens" if tokens is not None else "max_run_seconds"
    assert f"`{nom}`" in str(e.value)


@pytest.mark.parametrize("tokens, secondes", [
    (None, None), (0, 0), (1, 60), (200_000, 3600), (50_000, 900)])
def test_les_limites_valides_passent(tokens, secondes):
    L.valider(tokens, secondes)


def test_zero_retire_la_limite():
    assert L.a_ecrire(0) is None
    assert L.a_ecrire(None) is None
    assert L.a_ecrire(1200) == 1200


def test_rien_ne_voyage_quand_rien_n_est_declare():
    assert L.charge(None, None) == {}
    assert L.charge(50_000, 1200) == {"max_tokens": 50_000, "max_seconds": 1200}


# ── le déclencheur : retouche ────────────────────────────────────────────────

def _retouche(monkeypatch, **kw):
    vu = {}
    monkeypatch.setattr(RT.db, "get_trigger", lambda i, o: {"id": i, "cron": "5 6 * * *",
                                                            "tz": "UTC", "kind": "schedule"})
    monkeypatch.setattr(RT.db, "update_trigger",
                        lambda i, o, champs, **k: vu.update(champs) or {"id": i, **champs})
    asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(op="update", trigger_id=3, **kw)))
    return vu


def test_la_retouche_ecrit_les_limites(monkeypatch):
    vu = _retouche(monkeypatch, max_tokens=80_000, max_run_seconds=1800)
    assert vu["max_tokens"] == 80_000 and vu["max_run_seconds"] == 1800


def test_la_retouche_a_zero_ecrit_null(monkeypatch):
    vu = _retouche(monkeypatch, max_tokens=0, max_run_seconds=0)
    assert "max_tokens" in vu and vu["max_tokens"] is None
    assert "max_run_seconds" in vu and vu["max_run_seconds"] is None


def test_une_retouche_sans_limite_n_y_touche_pas(monkeypatch):
    vu = _retouche(monkeypatch, label="veille")
    assert "max_tokens" not in vu and "max_run_seconds" not in vu


def test_la_retouche_hors_fourchette_est_refusee_avant_toute_ecriture(monkeypatch):
    with pytest.raises(AuthzDenied) as e:
        _retouche(monkeypatch, max_run_seconds=7200)
    assert e.value.code == "invalid_bound"


# ── le tick : ce qui part avec le travail ────────────────────────────────────

def _tick(monkeypatch, **declencheur):
    enfile = {}
    base = {"id": 5, "org_id": 2, "cron": "5 6 * * *", "tz": "Europe/Paris",
            "next_due": "2026-08-14 04:05:00", "procedure": "veille-linkedin",
            "project_id": None, "tools": ["data_write"], "input": None,
            "label": None, "max_steps": None, **declencheur}
    monkeypatch.setattr(runner_tick.db, "due_triggers", lambda limit=50: [base])
    monkeypatch.setattr(runner_tick.db, "consume_due", lambda i, vu, prochaine: True)
    monkeypatch.setattr(runner_tick.db, "perimer_travaux_du_declencheur", lambda t, o: 0)
    monkeypatch.setattr(runner_tick.db, "enqueue_job",
                        lambda org, kind, payload=None, **_: enfile.update(payload) or {"id": 9})
    assert runner_tick._tick() == 1
    return enfile


def test_le_tick_emporte_les_limites_declarees(monkeypatch):
    charge = _tick(monkeypatch, max_tokens=60_000, max_run_seconds=1200)
    assert charge["max_tokens"] == 60_000 and charge["max_seconds"] == 1200


def test_le_tick_n_emporte_rien_sans_limite(monkeypatch):
    charge = _tick(monkeypatch, max_tokens=None, max_run_seconds=None)
    assert "max_tokens" not in charge and "max_seconds" not in charge


# ── la flotte ────────────────────────────────────────────────────────────────

def test_la_flotte_refuse_une_duree_hors_fourchette():
    with pytest.raises(AuthzDenied) as e:
        RF._bornes_valides(RF.FleetInput(op="update", fleet_id=1, max_run_seconds=10))
    assert e.value.code == "invalid_bound"
    RF._bornes_valides(RF.FleetInput(op="update", fleet_id=1, max_run_seconds=0))


# ── en base : écrit, relu, retiré ────────────────────────────────────────────

@pytest.fixture(scope="module")
def live(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_limites_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    avant_url, avant_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    avant_key = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["DATABASE_URL"] = dsn
    os.environ["OTO_MCP_MASTER_KEY"] = "4" * 64
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = avant_pool
        for cle, valeur in (("DATABASE_URL", avant_url),
                            ("OTO_MCP_MASTER_KEY", avant_key)):
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


def test_le_declencheur_ecrit_et_relit_ses_limites(live):
    from oto_mcp import db
    t = db.create_trigger(2, "alexis", procedure="p", tz="UTC", tools=["data_rows"],
                          cron="5 6 * * *", max_tokens=40_000, max_run_seconds=600)
    assert (t["max_tokens"], t["max_run_seconds"]) == (40_000, 600)
    lu = db.get_trigger(t["id"], 2)
    assert (lu["max_tokens"], lu["max_run_seconds"]) == (40_000, 600)
    db.update_trigger(t["id"], 2, {"max_tokens": None, "max_run_seconds": 1800})
    lu = db.get_trigger(t["id"], 2)
    assert (lu["max_tokens"], lu["max_run_seconds"]) == (None, 1800)


def test_un_declencheur_sans_limite_les_relit_null(live):
    from oto_mcp import db
    t = db.create_trigger(2, "alexis", procedure="q", tz="UTC", tools=["data_rows"],
                          cron="5 6 * * *")
    assert (t["max_tokens"], t["max_run_seconds"]) == (None, None)


def test_la_flotte_ecrit_et_relit_sa_duree(live):
    from oto_mcp import db
    f = db.create_fleet(2, "alexis", label="banc", procedure="p", tools=["data_rows"],
                        max_tokens_per_row=30_000, max_run_seconds=900)
    assert (f["max_tokens_per_row"], f["max_run_seconds"]) == (30_000, 900)
    f = db.update_fleet(f["id"], 2, {"max_run_seconds": None})
    assert f["max_run_seconds"] is None
