"""Plomberie de connexion PG : pool psycopg, row factory, bornes serveur.

Extrait de l'ex-monolithe `db.py` (barreau 2). Aucune logique métier ici —
juste le pool, le `_connect()` context manager et les helpers de normalisation
de row. Importé par tous les modules de domaine du package `db`.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .. import connectors

def _normalize_value(v: Any) -> Any:
    # Match the string shape SQLite returned ("YYYY-MM-DD HH:MM:SS") so downstream
    # JSONResponse + frontends keep working unchanged.
    if isinstance(v, datetime):
        return v.replace(tzinfo=None, microsecond=0).isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    return v


def _str_dict_row(cursor):
    inner = dict_row(cursor)

    def make_row(values):
        d = inner(values)
        if d:
            for k, v in d.items():
                if isinstance(v, (datetime, date)):
                    d[k] = _normalize_value(v)
        return d

    return make_row


# Providers supportés pour les user keys. DÉRIVÉ du registre source unique
# (`connectors.py`) — ne plus éditer ici, déclarer le connecteur dans le registre.
KEY_PROVIDERS = connectors.KEY_PROVIDERS
# Ensemble plus large des providers pouvant détenir un credential (keyed + sessions
# cookie + byo multi-champs) — garde-fou d'écriture `keys._check_provider`.
CREDENTIAL_PROVIDERS = connectors.CREDENTIAL_PROVIDERS


_pool: Optional[ConnectionPool] = None


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set (managed PG connection string)")
    return url


def _connect_options() -> str:
    """Bornes serveur posées par connexion via l'option libpq `options` (issue #70).

    `idle_in_transaction_session_timeout` (défaut 60 s) tue une transaction laissée
    IDLE → empêche qu'un process hangé laisse une connexion zombie tenant un lock
    qui bloquerait le boot suivant (`init_db`, incident 2026-06-25). Sans effet sur
    une requête EN COURS (seules les txns inactives sont coupées).

    `statement_timeout` est **opt-in** (défaut 0 = off) : on ne l'active pas par
    défaut car un `CREATE INDEX` de migration sur une grosse table (tool_calls,
    datastore_rows) pourrait dépasser le seuil au boot. Le borné cold-S3 du scan
    SIRENE est déjà porté par le service FOD (watchdog 90 s), pas par ce pool.
    """
    idle = os.environ.get("OTO_MCP_DB_IDLE_TX_TIMEOUT_MS", "60000")
    stmt = os.environ.get("OTO_MCP_DB_STATEMENT_TIMEOUT_MS", "0")
    parts = [f"-c idle_in_transaction_session_timeout={idle}"]
    if stmt and stmt != "0":
        parts.append(f"-c statement_timeout={stmt}")
    return " ".join(parts)


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=_database_url(),
            min_size=1,
            max_size=int(os.environ.get("OTO_MCP_DB_POOL_MAX", "8")),
            kwargs={"row_factory": _str_dict_row, "options": _connect_options()},
            open=True,
            # Attente MAX d'une connexion (défaut psycopg_pool : 30s !). Pendant un
            # blip DB (SSL eof, saturation), le pool se vide et `getconn` ATTEND —
            # depuis un chemin sync dans l'event loop (ex. _authenticate), c'est le
            # serveur ENTIER qui gèle. 5s ⇒ PoolTimeout → 500 propre, pas un down.
            # Vécu 2026-07-02 (2 gels, py-spy : getconn wait sous _authenticate).
            timeout=float(os.environ.get("OTO_MCP_DB_POOL_TIMEOUT", "5") or "5"),
        )
    return _pool


@contextmanager
def _connect() -> Iterator[psycopg.Connection]:
    pool = _get_pool()
    with pool.connection() as conn:
        yield conn
