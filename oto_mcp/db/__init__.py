"""Façade du store PostgreSQL (package `db`).

Le store est découpé en modules : `_conn` (pool/connexion), `_schema` (DDL),
`_init` (init/migrations) et un module par domaine métier (`users`, `unipile`,
`keys`, `usage`, `platform_instructions`, `visibility`, `emails`, `google`,
`datastore`, `projects`, `tokens`, `opendata`).

Ce `__init__` ré-exporte l'intégralité du namespace de ces sous-modules pour que
la surface `db.<symbole>` (publics + privés consommés à l'extérieur comme
`db._connect` / `db._ds_filter_clauses`) reste plate et stable. Les modules
n'ont pas de dépendance circulaire : tout pointe vers `_conn` puis `users`.
"""
from __future__ import annotations

from . import (
    _conn,
    _schema,
    _init,
    users,
    unipile,
    connector_grants,
    keys,
    usage,
    platform_instructions,
    visibility,
    emails,
    google,
    datastore,
    projects,
    tokens,
    upload_tokens,
    opendata,
    billing,
)

# Ré-export plat (publics + privés à un underscore). Les noms dunder restent au
# package. L'ordre place les bases (_conn, users) d'abord — sans incidence, les
# noms sont disjoints entre modules.
_MODULES = (
    _conn, _schema, _init, users, unipile, connector_grants, keys, usage,
    platform_instructions, visibility, emails, google, datastore, projects,
    tokens, upload_tokens, opendata, billing,
)
_g = globals()
for _mod in _MODULES:
    for _name in dir(_mod):
        if not _name.startswith("__"):
            _g[_name] = getattr(_mod, _name)
del _g, _mod, _name, _MODULES
