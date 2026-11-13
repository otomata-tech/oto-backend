"""La révision `20261111_2000_office_en_lecture` rabat en lecture les partages d'org
posés d'office sur les agents, et `oto-mcp maintenance partages-office --dry-run` les
compte sans rien changer.

Jouée sur une vraie base : seuls les partages d'org d'agent `editor` sous la marque de
la révision 0035 deviennent `viewer`/`read` ; un partage posé par une personne ne bouge
pas ; le journal porte le compte par org ; le compte à blanc rend le même compte ; le
retour arrière remet exactement ce que la révision a rabattu.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261111_1930_dataforseo_decrit"
_REVISION = "20261111_2000_office_en_lecture"
_M0035 = "migration:0035_agents_partages_a_l_org"
_MARQUE = f"migration:{_REVISION}"

#: (agent, type de principal, principal, rôle, posé par) — l'état d'avant la révision.
_AVANT = [
    ("1", "org", "10", "editor", _M0035),          # d'office : rabattu
    ("2", "org", "10", "editor", _M0035),          # d'office : rabattu
    ("3", "org", "20", "editor", _M0035),          # d'office : rabattu
    ("4", "org", "20", "editor", "u-proprio"),     # posé par une personne : intact
    ("5", "group", "7", "editor", "u-proprio"),    # partage d'équipe : intact
    ("6", "org", "30", "viewer", "u-proprio"),     # déjà en lecture : intact
]
_RABATTUS = {"1", "2", "3"}


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _partages(dsn) -> dict:
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        return {r["resource_id"]: (r["principal_type"], r["principal_id"], r["role"],
                                   r["permission"], r["granted_by"])
                for r in c.execute(
                    "SELECT * FROM resource_grants "
                    "WHERE resource_type = 'runner_trigger'").fetchall()}


@pytest.fixture
def base_d_avant(live, pg_module_dsn):
    """Chaque test repart de l'état d'avant (xdist : un test se suffit)."""
    import psycopg
    from alembic import command
    command.stamp(_alembic(), _PRECEDENTE)
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("DELETE FROM resource_grants WHERE resource_type = 'runner_trigger'")
        for rid, pt, pid, role, par in _AVANT:
            c.execute(
                "INSERT INTO resource_grants (resource_type, resource_id, principal_type, "
                "principal_id, permission, role, granted_by) "
                "VALUES ('runner_trigger', %s, %s, %s, %s, %s, %s)",
                (rid, pt, pid, "read" if role == "viewer" else "write", role, par))
    return pg_module_dsn


def test_la_revision_est_chainee_sous_son_nom():
    from alembic.script import ScriptDirectory
    from oto_mcp.db import partages_office
    rev = ScriptDirectory.from_config(_alembic()).get_revision(_REVISION)
    assert rev.down_revision == _PRECEDENTE
    assert partages_office.REVISION == _REVISION
    assert partages_office.MARQUE_RABATTU_EN_LECTURE == _MARQUE


def test_un_partage_d_office_est_rabattu_les_autres_ne_bougent_pas(base_d_avant):
    from alembic import command
    avant = _partages(base_d_avant)
    command.upgrade(_alembic(), _REVISION)
    apres = _partages(base_d_avant)
    for rid in _RABATTUS:
        pt, pid, _, _, _ = avant[rid]
        assert apres[rid] == (pt, pid, "viewer", "read", _MARQUE), rid
    for rid in set(avant) - _RABATTUS:
        assert apres[rid] == avant[rid], rid


def test_le_journal_porte_le_compte_par_org(base_d_avant, caplog):
    from alembic import command
    caplog.set_level(logging.INFO, logger="alembic.runtime.migration")
    command.upgrade(_alembic(), _REVISION)
    texte = "\n".join(r.getMessage() for r in caplog.records
                      if r.name == "alembic.runtime.migration")
    assert "org_id=10 n=2" in texte
    assert "org_id=20 n=1" in texte
    assert "total=3 orgs=2" in texte
    assert "org_id=30" not in texte, "un partage d'une personne n'est pas compté"


def test_le_compte_a_blanc_rend_le_meme_compte_et_ne_change_rien(base_d_avant, capsys):
    from alembic import command
    from oto_mcp import maintenance
    from oto_mcp.db import partages_office
    avant = _partages(base_d_avant)
    assert partages_office.compter_a_blanc() == {10: 2, 20: 1}
    assert maintenance.main(["partages-office", "--dry-run"]) == (
        maintenance.PARTAGES_OFFICE_COMPTES)
    sortie = capsys.readouterr().out.splitlines()
    assert sortie == ["org_id=10 n=2", "org_id=20 n=1", "total=3 orgs=2"]
    assert _partages(base_d_avant) == avant, "le compte à blanc n'écrit rien"
    command.upgrade(_alembic(), _REVISION)
    assert partages_office.compter_a_blanc() == {}, (
        "après la révision, il ne reste rien à rabattre")


def test_le_compte_a_blanc_tourne_en_transaction_en_lecture_seule(live, monkeypatch):
    import psycopg
    from oto_mcp.db import partages_office
    monkeypatch.setattr(
        partages_office, "COMPTE_PAR_ORG",
        "UPDATE resource_grants SET role = role WHERE false RETURNING 1 AS org_id, 1 AS n")
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        partages_office.compter_a_blanc()


def test_le_compte_a_blanc_exige_dry_run():
    from oto_mcp import maintenance
    with pytest.raises(SystemExit) as e:
        maintenance.main(["partages-office"])
    assert e.value.code == 2


def test_le_retour_arriere_remet_exactement_ce_qui_a_ete_rabattu(base_d_avant):
    import psycopg
    from alembic import command
    avant = _partages(base_d_avant)
    command.upgrade(_alembic(), _REVISION)
    # Après la révision, une personne reprend le partage de l'agent 2 (en lecture, à son
    # nom) et retire celui de l'agent 3 : le retour arrière n'y touche plus.
    with psycopg.connect(base_d_avant, autocommit=True) as c:
        c.execute("UPDATE resource_grants SET granted_by = 'u-proprio' "
                  "WHERE resource_type = 'runner_trigger' AND resource_id = '2'")
        c.execute("DELETE FROM resource_grants "
                  "WHERE resource_type = 'runner_trigger' AND resource_id = '3'")
    command.downgrade(_alembic(), _PRECEDENTE)
    apres = _partages(base_d_avant)
    assert apres["1"] == avant["1"], "rabattu par la révision : remis en écriture"
    assert apres["2"] == ("org", "10", "viewer", "read", "u-proprio"), (
        "repris par une personne : laissé tel qu'elle l'a posé")
    assert "3" not in apres, "retiré par une personne : pas recréé"
    for rid in ("4", "5", "6"):
        assert apres[rid] == avant[rid], rid
