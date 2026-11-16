"""La révision `20261111_0920_jetons_echeance` borne à 90 jours les jetons sans échéance.

Jouée sur une vraie base : seuls les jetons NON révoqués à `expires_at` NULL reçoivent
`NOW() + 90 jours`, toutes natures comprises ; une échéance déjà posée et une ligne
révoquée ne bougent pas ; le retour arrière lève, nommément.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "20261109_2120_alertes_slack"
_REVISION = "20261111_0920_jetons_echeance"


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _jeton(c, sub, h, kind="user", expires=None, revoque=False):
    return c.execute(
        "INSERT INTO user_api_tokens (sub, token_hash, kind, expires_at, revoked_at) "
        f"VALUES (%s, %s, %s, {expires or 'NULL'}, {'NOW()' if revoque else 'NULL'}) "
        "RETURNING id", (sub, h, kind)).fetchone()["id"]


@pytest.fixture(scope="module")
def base_d_avant(live, pg_module_dsn):
    import psycopg
    from psycopg.rows import dict_row
    from alembic import command
    command.stamp(_alembic(), _PRECEDENTE)
    with psycopg.connect(pg_module_dsn, autocommit=True, row_factory=dict_row) as c:
        c.execute("INSERT INTO users (sub) VALUES ('u-migr'), ('org-migr') "
                  "ON CONFLICT DO NOTHING")
        ids = {
            "user_null": _jeton(c, "u-migr", "h1"),
            "org_null": _jeton(c, "org-migr", "h2", kind="org"),
            "deja_borne": _jeton(c, "u-migr", "h3",
                                 expires="NOW() + INTERVAL '10 days'"),
            "revoque_null": _jeton(c, "u-migr", "h4", revoque=True),
            "delegation": _jeton(c, "u-migr", "h5", kind="delegation",
                                 expires="NOW() + INTERVAL '12 minutes'"),
        }
        avant = {r["id"]: r["expires_at"] for r in c.execute(
            "SELECT id, expires_at FROM user_api_tokens").fetchall()}
    return pg_module_dsn, ids, avant


def _echeances(dsn) -> dict:
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        return {r["id"]: r for r in c.execute(
            "SELECT id, expires_at, expires_at - NOW() AS reste "
            "FROM user_api_tokens").fetchall()}


def test_la_revision_borne_les_seuls_jetons_vivants_sans_echeance(base_d_avant):
    from datetime import timedelta
    from alembic import command
    dsn, ids, avant = base_d_avant
    command.upgrade(_alembic(), _REVISION)
    apres = _echeances(dsn)
    for cle in ("user_null", "org_null"):
        reste = apres[ids[cle]]["reste"]
        assert timedelta(days=89, hours=23) < reste <= timedelta(days=90), (cle, reste)
    for cle in ("deja_borne", "delegation"):
        assert apres[ids[cle]]["expires_at"] == avant[ids[cle]], cle
    assert apres[ids["revoque_null"]]["expires_at"] is None, (
        "une ligne révoquée garde sa trace telle quelle")


def _module():
    from alembic.script import ScriptDirectory
    return ScriptDirectory.from_config(_alembic()).get_revision(_REVISION).module


def test_l_ordre_rejoue_ne_change_rien(base_d_avant):
    """Une seconde passe ne trouve plus de NULL vivant : aucune échéance ne glisse."""
    import psycopg
    from alembic import command
    dsn, _, _ = base_d_avant
    command.upgrade(_alembic(), _REVISION)   # chaque test se suffit (xdist)
    une = {i: r["expires_at"] for i, r in _echeances(dsn).items()}
    with psycopg.connect(dsn, autocommit=True) as c:
        assert c.execute(_module().BORNER).rowcount == 0
    assert {i: r["expires_at"] for i, r in _echeances(dsn).items()} == une


def test_le_retour_arriere_leve_nommement(base_d_avant):
    from alembic import command
    command.upgrade(_alembic(), _REVISION)
    # Alembic recharge le module à chaque `ScriptDirectory` : la classe se reconnaît
    # par son NOM, pas par son identité.
    with pytest.raises(RuntimeError, match="irréversible") as e:
        command.downgrade(_alembic(), _PRECEDENTE)
    assert type(e.value).__name__ == "RetourImpossible"
