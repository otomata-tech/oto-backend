"""Les partages d'org posés d'office sur les agents passent en lecture par un GESTE
explicite, `oto-mcp maintenance partages-office --appliquer`, après l'aperçu
`--dry-run` qui compte sans rien écrire. Aucune révision ni le démarrage n'y touchent.

Joué sur une vraie base : seuls les partages d'org d'agent `editor` sous la marque de
la révision 0035 deviennent `viewer`/`read` ; un partage posé par une personne ne bouge
pas ; l'aperçu rend le compte par org que le geste écrit ; le retour arrière remet
exactement ce que le geste a rabattu.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_M0035 = "migration:0035_agents_partages_a_l_org"
_MARQUE = "maintenance:partages-office"

#: (agent, type de principal, principal, rôle, posé par) — l'état d'avant le geste.
_AVANT = [
    ("1", "org", "10", "editor", _M0035),          # d'office : rabattu
    ("2", "org", "10", "editor", _M0035),          # d'office : rabattu
    ("3", "org", "20", "editor", _M0035),          # d'office : rabattu
    ("4", "org", "20", "editor", "u-proprio"),     # posé par une personne : intact
    ("5", "group", "7", "editor", "u-proprio"),    # partage d'équipe : intact
    ("6", "org", "30", "viewer", "u-proprio"),     # déjà en lecture : intact
]
_RABATTUS = {"1", "2", "3"}


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
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("DELETE FROM resource_grants WHERE resource_type = 'runner_trigger'")
        for rid, pt, pid, role, par in _AVANT:
            c.execute(
                "INSERT INTO resource_grants (resource_type, resource_id, principal_type, "
                "principal_id, permission, role, granted_by) "
                "VALUES ('runner_trigger', %s, %s, %s, %s, %s, %s)",
                (rid, pt, pid, "read" if role == "viewer" else "write", role, par))
    return pg_module_dsn


def test_aucune_revision_ni_le_demarrage_n_ecrivent_ces_partages():
    """Le passage en lecture est un geste sur décision : ni le registre Alembic (joué
    après chaque tag), ni le démarrage, ni le travail quotidien ne le portent."""
    from oto_mcp.db import partages_office
    versions = RACINE / "oto_mcp" / "db" / "migrations" / "versions"
    for f in versions.glob("*.py"):
        assert "partages_office" not in f.read_text(encoding="utf-8"), f.name
    for module in ("server.py", "db/_init.py"):
        assert "partages_office" not in (RACINE / "oto_mcp" / module).read_text(
            encoding="utf-8"), module
    from oto_mcp import maintenance
    assert "partages-office" not in maintenance._TRAVAUX, "pas un travail du timer"
    assert "partages-office" not in maintenance._ALL
    assert partages_office.MARQUE_RABATTU_EN_LECTURE == _MARQUE


def test_l_apercu_compte_par_org_et_n_ecrit_rien(base_d_avant, capsys):
    from oto_mcp import maintenance
    from oto_mcp.db import partages_office
    avant = _partages(base_d_avant)
    assert partages_office.compter_a_blanc() == {10: 2, 20: 1}
    assert maintenance.main(["partages-office", "--dry-run"]) == (
        maintenance.PARTAGES_OFFICE_FAIT)
    sortie = capsys.readouterr().out.splitlines()
    assert sortie[1:] == ["org_id=10 n=2", "org_id=20 n=1", "total=3 orgs=2"]
    assert _partages(base_d_avant) == avant, "l'aperçu n'écrit rien"


def test_le_geste_rabat_les_partages_d_office_les_autres_ne_bougent_pas(base_d_avant,
                                                                         capsys):
    from oto_mcp import maintenance
    from oto_mcp.db import partages_office
    avant = _partages(base_d_avant)
    assert maintenance.main(["partages-office", "--appliquer"]) == (
        maintenance.PARTAGES_OFFICE_FAIT)
    assert capsys.readouterr().out.splitlines()[1:] == [
        "org_id=10 n=2", "org_id=20 n=1", "total=3 orgs=2"]
    apres = _partages(base_d_avant)
    for rid in _RABATTUS:
        pt, pid, _, _, _ = avant[rid]
        assert apres[rid] == (pt, pid, "viewer", "read", _MARQUE), rid
    for rid in set(avant) - _RABATTUS:
        assert apres[rid] == avant[rid], rid
    assert partages_office.compter_a_blanc() == {}, "après le geste, rien à rabattre"


def test_l_apercu_tourne_en_transaction_en_lecture_seule(live, monkeypatch):
    import psycopg
    from oto_mcp.db import partages_office
    monkeypatch.setattr(
        partages_office, "COMPTE_PAR_ORG",
        "UPDATE resource_grants SET role = role WHERE false RETURNING 1 AS org_id, 1 AS n")
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        partages_office.compter_a_blanc()


@pytest.mark.parametrize("args", [[], ["--dry-run", "--appliquer"],
                                  ["--appliquer", "--retablir"]])
def test_un_seul_mode_est_exige(args):
    from oto_mcp import maintenance
    with pytest.raises(SystemExit) as e:
        maintenance.main(["partages-office", *args])
    assert e.value.code == 2


def test_le_retour_arriere_remet_exactement_ce_qui_a_ete_rabattu(base_d_avant):
    import psycopg
    from oto_mcp import maintenance
    avant = _partages(base_d_avant)
    assert maintenance.main(["partages-office", "--appliquer"]) == 0
    # Après le geste, une personne reprend le partage de l'agent 2 (en lecture, à son
    # nom) et retire celui de l'agent 3 : le retour arrière n'y touche plus.
    with psycopg.connect(base_d_avant, autocommit=True) as c:
        c.execute("UPDATE resource_grants SET granted_by = 'u-proprio' "
                  "WHERE resource_type = 'runner_trigger' AND resource_id = '2'")
        c.execute("DELETE FROM resource_grants "
                  "WHERE resource_type = 'runner_trigger' AND resource_id = '3'")
    assert maintenance.main(["partages-office", "--retablir"]) == 0
    apres = _partages(base_d_avant)
    assert apres["1"] == avant["1"], "rabattu par le geste : remis en écriture"
    assert apres["2"] == ("org", "10", "viewer", "read", "u-proprio"), (
        "repris par une personne : laissé tel qu'elle l'a posé")
    assert "3" not in apres, "retiré par une personne : pas recréé"
    for rid in ("4", "5", "6"):
        assert apres[rid] == avant[rid], rid
