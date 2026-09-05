"""Façade du store PostgreSQL (package `db`).

Le store est découpé en modules : `_conn` (pool/connexion), `_schema` (DDL),
`_init` (init/migrations) et un module par domaine métier (`users`, `unipile`,
`keys`, `usage`, `platform_instructions`, `visibility`, `emails`, `google`,
`datastore`, `projects`, `tokens`, `opendata`).

Ce `__init__` ré-exporte l'intégralité du namespace de ces sous-modules pour que
la surface `db.<symbole>` (publics + privés consommés à l'extérieur comme
`db._connect` / `db._ds_filter_clauses`) reste plate et stable. Les modules
n'ont pas de dépendance circulaire : tout pointe vers `_conn` puis `users`.

⚠️ **Aucun module de ce package n'est importé par son nom ailleurs** — c'est
précisément l'effet recherché : les appelants écrivent `db.<fn>`. Un inventaire
qui cherche des IMPORTS STATIQUES les déclare donc tous orphelins, et c'est un
faux positif systématique : 8 modules (`connector_grants`, `datastore_embed`,
`emails`, `google`, `keys`, `tenants`, `tokens`, `visibility`) n'ont aucun
importeur hors d'ici alors que chacune de leurs fonctions est appelée en prod.
Pour juger qu'un module d'ici est mort, chercher ses SYMBOLES (`db.<fn>`, y
compris posés par `monkeypatch.setattr`), jamais son nom de module — puis croiser
avec `tests/test_db_surface_frozen.py`, qui fige la surface plate : un nom qui y
figure ne se retire qu'en retirant sa ligne, délibérément.
"""
from __future__ import annotations

from . import (
    _conn,
    _schema,
    _init,
    users,
    tenants,
    unipile,
    connector_grants,
    connector_instances,
    grants,
    access_shadow,
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
    billing,
    billing_invoices,
    guides,
    legal,
    search,
    aux_embed,
    datastore_embed,
    run_thread,
    runner_jobs,
    runner_triggers,
    runner_fleets,
    journal_calls,
    # NON aplati ci-dessous (comme `access_shadow`) : ses noms de domaine
    # (`audience`, `journal`…) sont trop communs pour la surface plate `db.*`.
    # Les appelants écrivent `from ..db import outreach as db_outreach`.
    outreach,
    portee,
    alertes_credential,
    origine_ecritures,
)

# Ré-export plat (publics + privés à un underscore). Les noms dunder restent au
# package. L'ordre place les bases (_conn, users) d'abord — sans incidence, les
# noms sont disjoints entre modules.
_MODULES = (
    _conn, _schema, _init, users, tenants, unipile, connector_grants,
    connector_instances, grants, keys, usage,
    platform_instructions, visibility, emails, google, datastore, projects,
    tokens, upload_tokens, billing, billing_invoices, guides, legal, search, aux_embed,
    datastore_embed, run_thread, runner_jobs, runner_triggers, runner_fleets,
    journal_calls,
)
_g = globals()
for _mod in _MODULES:
    for _name in dir(_mod):
        if not _name.startswith("__"):
            _g[_name] = getattr(_mod, _name)
del _g, _mod, _name, _MODULES
