"""Un agent est à son ORG par défaut (23/10/2026) — en base.

1. `visibilites_d_agents` dit, en UNE requête, qui voit chaque agent au-delà de son
   propriétaire : le rôle du partage VIVANT à l'org, et le nombre de personnes nommées.
2. La révision `20261023_1000_agents_org_default` ouvre à leur org les agents nés privés
   APRÈS la révision 0035, et pas un agent plus ancien dont le partage d'org a été retiré
   (quelqu'un l'a fermé). Joué sur une vraie base.
"""
from __future__ import annotations

from pathlib import Path

import psycopg

RACINE = Path(__file__).resolve().parent.parent
ORG = 8310
_PRECEDENTE = "20261022_2112_forfait_fenetre"
_REVISION = "20261023_1000_agents_org_default"
_MARQUE_0035 = "migration:0035_agents_partages_a_l_org"


def _agent(db, org=ORG):
    return db.create_trigger(org, "proprio", procedure="veille", tz="UTC", tools=["a"],
                             cron="0 8 * * *")


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def test_qui_voit_chaque_agent(live, pg_module_dsn):
    from oto_mcp import db
    a, b, c, d = _agent(db), _agent(db), _agent(db), _agent(db)
    db.grant_resource("runner_trigger", str(a["id"]), "org", str(ORG), role="editor")
    db.grant_resource("runner_trigger", str(a["id"]), "user", "x", role="viewer")
    db.grant_resource("runner_trigger", str(b["id"]), "org", str(ORG), role="viewer")
    db.grant_resource("runner_trigger", str(c["id"]), "org", str(ORG), role="editor")
    with psycopg.connect(pg_module_dsn, autocommit=True) as conn:
        conn.execute("UPDATE resource_grants SET expires_at = NOW() - interval '1 day' "
                     "WHERE resource_id = %s", (str(c["id"]),))
    assert db.visibilites_d_agents([a["id"], b["id"], c["id"], d["id"]], ORG) == {
        a["id"]: {"org": "editor", "people": 1},
        b["id"]: {"org": "viewer", "people": 0},
        c["id"]: {"org": None, "people": 0},   # échu : ne donne plus rien
        d["id"]: {"org": None, "people": 0},
    }
    # Un partage à une AUTRE org ne rend pas l'agent visible dans la sienne.
    assert db.visibilites_d_agents([b["id"]], ORG + 1)[b["id"]]["org"] is None
    assert db.visibilites_d_agents([], ORG) == {}


def _org_de(dsn, agent_id):
    with psycopg.connect(dsn) as c:
        r = c.execute("SELECT role, granted_by FROM resource_grants "
                      "WHERE resource_type = 'runner_trigger' AND resource_id = %s "
                      "AND principal_type = 'org'", (str(agent_id),)).fetchone()
    return r


def test_la_revision_ouvre_les_agents_nes_prives_depuis_0035(live, pg_module_dsn):
    from alembic import command

    from oto_mcp import db
    ferme = _agent(db)        # né avant 0035, son partage d'org retiré depuis
    marque = _agent(db)       # né avant 0035, toujours ouvert
    prive = _agent(db)        # né après 0035, privé
    arme = db.create_trigger(ORG, "proprio", procedure="veille", tz="UTC",
                             tools=["a", "oto_admin_org"], cron="0 8 * * *")
    db.update_trigger(arme["id"], ORG, {"outils_plateforme": ["oto_admin_org"]})
    deja = _agent(db)         # né après 0035, déjà ouvert en lecture
    db.grant_resource("runner_trigger", str(marque["id"]), "org", str(ORG),
                      role="editor", granted_by=_MARQUE_0035)
    db.grant_resource("runner_trigger", str(deja["id"]), "org", str(ORG), role="viewer")
    command.stamp(_alembic(), _PRECEDENTE)

    command.upgrade(_alembic(), _REVISION)
    assert _org_de(pg_module_dsn, ferme["id"]) is None, (
        "on ne rouvre pas en silence ce que quelqu'un a fermé")
    assert _org_de(pg_module_dsn, prive["id"])[0] == "editor"
    assert _org_de(pg_module_dsn, arme["id"]) is None, (
        "un agent armé d'oto_admin_* reste privé : le partager le désarmerait")
    assert _org_de(pg_module_dsn, deja["id"])[0] == "viewer", "un partage posé reste"

    command.downgrade(_alembic(), _PRECEDENTE)
    assert _org_de(pg_module_dsn, prive["id"]) is None
    assert _org_de(pg_module_dsn, deja["id"])[0] == "viewer"
    assert _org_de(pg_module_dsn, marque["id"])[1] == _MARQUE_0035
