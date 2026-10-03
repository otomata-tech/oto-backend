"""Le partage d'agents EN BASE — ce qu'une doublure ne peut pas prouver.

1. `partages_d_agents` lit en UNE requête le meilleur partage VIVANT parmi les
   principaux de l'appelant (`write` l'emporte, un partage échu ne compte pas).
2. Supprimer un agent retire ses partages.
3. La révision 0032 ouvre chaque agent EXISTANT à son org, sans écraser un partage
   déjà posé, et se défait.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

RACINE = Path(__file__).resolve().parents[1]
AVANT, APRES = "0031_selection_org_reelle", "0032_agents_partages_a_l_org"
ORG = 8200


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _agent(db, sub="proprio", org=ORG):
    return db.create_trigger(org, sub, procedure="veille", tz="UTC", tools=["a"],
                             cron="0 8 * * *")


def test_le_meilleur_partage_vivant_parmi_les_principaux(live, pg_module_dsn):
    from oto_mcp import db
    a, b, c = _agent(db), _agent(db), _agent(db)
    db.grant_resource("runner_trigger", str(a["id"]), "user", "lecteur", role="viewer")
    db.grant_resource("runner_trigger", str(a["id"]), "org", str(ORG), role="editor")
    db.grant_resource("runner_trigger", str(b["id"]), "user", "lecteur", role="viewer")
    db.grant_resource("runner_trigger", str(c["id"]), "user", "lecteur", role="editor")
    with psycopg.connect(pg_module_dsn, autocommit=True) as conn:
        conn.execute("UPDATE resource_grants SET expires_at = NOW() - interval '1 day' "
                     "WHERE resource_id = %s", (str(c["id"]),))
    principaux = [("user", "lecteur"), ("org", str(ORG))]
    assert db.partages_d_agents([a["id"], b["id"], c["id"]], principaux) == {
        a["id"]: "write", b["id"]: "read"}, (
        "l'org en écriture l'emporte sur la personne en lecture ; un partage échu ne "
        "donne rien")
    assert db.partages_d_agents([a["id"]], [("user", "autre")]) == {}
    assert db.partages_d_agents([], principaux) == {}


def test_supprimer_un_agent_retire_ses_partages(live):
    from oto_mcp import db
    t = _agent(db)
    db.grant_resource("runner_trigger", str(t["id"]), "user", "editeur", role="editor")
    assert db.delete_trigger(t["id"], ORG)
    assert db.list_resource_grants("runner_trigger", str(t["id"])) == []


def test_la_revision_0032_ouvre_les_agents_existants_a_leur_org(live, pg_module_dsn):
    from alembic import command
    from oto_mcp import db
    ouvert, ferme = _agent(db), _agent(db, org=ORG + 1)
    # Un partage d'org DÉJÀ posé en lecture n'est pas réécrit en écriture.
    db.grant_resource("runner_trigger", str(ferme["id"]), "org", str(ORG + 1),
                      role="viewer")
    cfg = _alembic()
    command.stamp(cfg, AVANT)
    try:
        command.upgrade(cfg, APRES)
        assert db.get_resource_grant("runner_trigger", str(ouvert["id"]), "org",
                                     str(ORG))["role"] == "editor"
        assert db.get_resource_grant("runner_trigger", str(ferme["id"]), "org",
                                     str(ORG + 1))["role"] == "viewer"
        command.downgrade(cfg, AVANT)
        assert db.get_resource_grant("runner_trigger", str(ouvert["id"]), "org",
                                     str(ORG)) is None
        assert db.get_resource_grant("runner_trigger", str(ferme["id"]), "org",
                                     str(ORG + 1)) is not None, (
            "le retour arrière ne retire que ce que la révision a posé")
    finally:
        command.stamp(cfg, "head")
