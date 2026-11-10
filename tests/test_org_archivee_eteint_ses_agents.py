"""Une org archivée éteint ses agents hébergés, et le tick ignore ceux qu'une archive
plus ancienne aurait laissés allumés.

Une org archivée n'est plus joignable par `_org=` : chaque occurrence d'un de ses agents
mourrait au `run_start` (« tu n'es membre d'aucune org »), et le médecin des agents
l'escaladait comme une panne de plateforme. Banc : un PostgreSQL réel — le défaut vit
dans une transaction et un prédicat SQL.
"""
from __future__ import annotations

import datetime

import pytest

pytest.importorskip("psycopg")


def _org(nom: str) -> int:
    from oto_mcp.db._conn import _connect

    with _connect() as conn:
        return conn.execute("INSERT INTO orgs (name) VALUES (%s) RETURNING id",
                            (nom,)).fetchone()["id"]


def _agent(org: int, procedure: str) -> dict:
    from oto_mcp import db

    hier = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    return db.create_trigger(org, "porteur", procedure=procedure, cron="5 6 * * *",
                             tz="UTC", tools=["a"], next_due=hier)


def _allume(trigger_id: int) -> bool:
    from oto_mcp.db._conn import _connect

    with _connect() as conn:
        return conn.execute("SELECT enabled FROM runner_triggers WHERE id = %s",
                            (trigger_id,)).fetchone()["enabled"]


def test_archiver_une_org_eteint_ses_agents_et_pas_ceux_des_autres(live):
    from oto_mcp import db, org_store

    archivee, voisine = _org("archivee"), _org("voisine")
    a, b = _agent(archivee, "veille-a"), _agent(voisine, "veille-b")
    assert _allume(a["id"]) and _allume(b["id"])

    assert org_store.archive_org(archivee) is True
    assert not _allume(a["id"]), "l'agent d'une org archivée doit être éteint"
    assert _allume(b["id"]), "l'archive d'une org ne touche pas les agents d'une autre"
    dus = [d["id"] for d in db.due_triggers(limit=500)]
    assert a["id"] not in dus and b["id"] in dus


def test_le_tick_ignore_un_agent_allume_d_une_org_deja_archivee(live):
    """Une org archivée AVANT que l'archive n'éteigne ses agents en garde d'allumés."""
    from oto_mcp import db
    from oto_mcp.db._conn import _connect

    org = _org("archivee-avant")
    t = _agent(org, "veille-ancienne")
    with _connect() as conn:
        conn.execute("UPDATE orgs SET archived_at = now() WHERE id = %s", (org,))
    assert _allume(t["id"])
    assert t["id"] not in [d["id"] for d in db.due_triggers(limit=500)]
    with _connect() as conn:  # désarchivée : l'agent repart à l'horloge
        conn.execute("UPDATE orgs SET archived_at = NULL WHERE id = %s", (org,))
    assert t["id"] in [d["id"] for d in db.due_triggers(limit=500)]
