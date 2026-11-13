"""La révision `20261113_1900_app_use_role`, jouée sur une vraie base : elle élargit le
CHECK de `resource_grants.role` à `contributor` et pose `app_consents.file_ids` ; le
retour arrière ramène les partages `contributor` à `viewer`, remet le CHECK d'avant et
retire la colonne — puis la révision se rejoue."""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261113_1800_usage_org"
_REVISION = "20261113_1900_app_use_role"


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _connexion(dsn):
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)


def _check(c) -> str:
    return c.execute(
        "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
        "WHERE conname = 'resource_grants_role_check'").fetchone()["d"]


def _a_la_colonne(c) -> bool:
    return c.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = 'app_consents' "
        "AND column_name = 'file_ids'").fetchone() is not None


def _partage(c, role: str) -> None:
    c.execute("INSERT INTO resource_grants (resource_type, resource_id, principal_type, "
              "principal_id, permission, role) VALUES ('app', '1', 'user', 'u-migr', "
              "'read', %s) ON CONFLICT DO NOTHING", (role,))


@pytest.fixture(scope="module")
def base(live, pg_module_dsn):
    from alembic import command
    command.stamp(_alembic(), _REVISION)
    return pg_module_dsn


def test_aller_retour_puis_rejeu(base):
    import psycopg
    from alembic import command
    with _connexion(base) as c:
        c.execute("DELETE FROM resource_grants WHERE principal_id = 'u-migr'")
        _partage(c, "contributor")
        assert "contributor" in _check(c) and _a_la_colonne(c)

    command.downgrade(_alembic(), _PRECEDENTE)
    with _connexion(base) as c:
        assert "contributor" not in _check(c)
        assert not _a_la_colonne(c)
        (g,) = c.execute("SELECT role, permission FROM resource_grants "
                         "WHERE principal_id = 'u-migr'").fetchall()
        assert (g["role"], g["permission"]) == ("viewer", "read")
        c.execute("DELETE FROM resource_grants WHERE principal_id = 'u-migr'")
        with pytest.raises(psycopg.errors.CheckViolation):
            _partage(c, "contributor")

    command.upgrade(_alembic(), _REVISION)
    with _connexion(base) as c:
        assert "contributor" in _check(c) and _a_la_colonne(c)
        _partage(c, "contributor")
        c.execute("DELETE FROM resource_grants WHERE principal_id = 'u-migr'")
