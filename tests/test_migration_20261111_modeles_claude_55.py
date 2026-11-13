"""La révision `20261111_1400_modeles_claude_55` passe les agents posés sur un modèle
Claude d'avant à la génération 5.5.

Jouée sur une vraie base : chaque id d'avant devient son successeur sur
`runner_triggers.model`, `runner_triggers.forfait_prete_model` (le prêt reste vivant) et
`runner_fleets.model` ; les autres modèles et les travaux (`runner_jobs`) ne bougent pas ;
le retour arrière rend les ids d'avant.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261111_0920_jetons_echeance"
_REVISION = "20261111_1400_modeles_claude_55"
_ORG = 8391


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _agent(c, model, prete=None):
    return c.execute(
        "INSERT INTO runner_triggers (org_id, sub, procedure, tools, tz, model, enabled, "
        "forfait_prete_model) VALUES (%s, 'proprio', 'veille', '[]'::jsonb, 'UTC', %s, "
        "false, %s) RETURNING id", (_ORG, model, prete)).fetchone()[0]


def _flotte(c, fid, model):
    c.execute("INSERT INTO runner_fleets (id, org_id, sub, label, procedure, tools, model) "
              "VALUES (%s, %s, 'proprio', 'f', 'p', '[]'::jsonb, %s)", (fid, _ORG, model))
    return fid


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    import psycopg
    from alembic import command
    command.stamp(_alembic(), _PRECEDENTE)
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        ids = {
            "sonnet": _agent(c, "claude-sonnet-5"),
            "opus_prete": _agent(c, "claude-opus-5", prete="claude-opus-5"),
            "haiku": _agent(c, "claude-haiku-4-5"),
            "mistral": _agent(c, "mistral-small-2603"),
            "abonnement": _agent(c, "sub:sonnet", prete="sub:sonnet"),
            "sans": _agent(c, None),
            "deja": _agent(c, "claude-sonnet-5-5"),
        }
        flottes = {"sonnet": _flotte(c, 91001, "claude-sonnet-5"),
                   "haiku": _flotte(c, 91002, "claude-haiku-4-5"),
                   "mistral": _flotte(c, 91003, "mistral-large-2512")}
        travail = c.execute(
            "INSERT INTO runner_jobs (org_id, kind, status, payload) VALUES "
            "(%s, 'start', 'pending', %s::jsonb) RETURNING id",
            (_ORG, json.dumps({"procedure": "p", "model": "claude-haiku-4-5",
                               "model_family": "anthropic", "effort": "none"}))
        ).fetchone()[0]
    return pg_module_dsn, ids, flottes, travail


def _etat(dsn, ids, flottes, travail):
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        agents = {k: c.execute("SELECT model, forfait_prete_model FROM runner_triggers "
                               "WHERE id = %s", (v,)).fetchone() for k, v in ids.items()}
        fl = {k: c.execute("SELECT model FROM runner_fleets WHERE id = %s",
                           (v,)).fetchone()["model"] for k, v in flottes.items()}
        charge = c.execute("SELECT payload FROM runner_jobs WHERE id = %s",
                           (travail,)).fetchone()["payload"]
    return agents, fl, charge


def test_la_revision_remplace_les_ids_d_avant_et_rien_d_autre(base_d_avant):
    from alembic import command
    dsn, ids, flottes, travail = base_d_avant
    command.upgrade(_alembic(), _REVISION)
    agents, fl, charge = _etat(dsn, ids, flottes, travail)
    assert agents["sonnet"] == {"model": "claude-sonnet-5-5", "forfait_prete_model": None}
    assert agents["opus_prete"] == {"model": "claude-opus-5-5",
                                    "forfait_prete_model": "claude-opus-5-5"}, (
        "les deux colonnes ensemble : le prêt reste vivant")
    assert agents["haiku"]["model"] == "claude-haiku-5-5"
    assert agents["mistral"]["model"] == "mistral-small-2603"
    assert agents["abonnement"] == {"model": "sub:sonnet", "forfait_prete_model": "sub:sonnet"}
    assert agents["sans"]["model"] is None
    assert agents["deja"]["model"] == "claude-sonnet-5-5"
    assert fl == {"sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5",
                  "mistral": "mistral-large-2512"}
    assert charge["model"] == "claude-haiku-4-5" and charge["effort"] == "none", (
        "un travail enfilé garde la charge avec laquelle il est parti")


def test_les_ids_des_deux_cotes_existent_au_catalogue(base_d_avant):
    """Ce que la révision écrit, le catalogue le sert — et ce qu'elle remplace aussi,
    puisque la base le nomme jusqu'au geste (révision jouée après le tag)."""
    from alembic.script import ScriptDirectory
    from oto_mcp import runner_models
    module = ScriptDirectory.from_config(_alembic()).get_revision(_REVISION).module
    for ancien, neuf in module._REMPLACES:
        assert runner_models.famille(ancien) == "anthropic", ancien
        assert runner_models.famille(neuf) == "anthropic", neuf


def test_le_retour_arriere_rend_les_ids_d_avant(base_d_avant):
    from alembic import command
    dsn, ids, flottes, travail = base_d_avant
    command.upgrade(_alembic(), _REVISION)   # chaque test se suffit (xdist)
    command.downgrade(_alembic(), _PRECEDENTE)
    agents, fl, _ = _etat(dsn, ids, flottes, travail)
    assert agents["sonnet"]["model"] == "claude-sonnet-5"
    assert agents["opus_prete"] == {"model": "claude-opus-5",
                                    "forfait_prete_model": "claude-opus-5"}
    assert agents["haiku"]["model"] == "claude-haiku-4-5"
    assert agents["deja"]["model"] == "claude-sonnet-5", (
        "posé sur 5.5 après coup, il redescend aussi")
    assert fl["sonnet"] == "claude-sonnet-5" and fl["mistral"] == "mistral-large-2512"
    command.upgrade(_alembic(), _REVISION)
    agents, _, _ = _etat(dsn, ids, flottes, travail)
    assert agents["sonnet"]["model"] == "claude-sonnet-5-5"
