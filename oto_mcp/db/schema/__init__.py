"""Le DDL du store, un module par domaine.

Chaque module expose des fragments SQL nommés ; `db/_schema.py` les assemble
dans l'ordre figé de son `ASSEMBLAGE`. Rien ici n'est exécuté à l'import :
importer ce paquet ne crée aucune table.
"""
from __future__ import annotations

from . import (
    billing,
    connectors,
    datastore,
    emails,
    embeddings,
    grants,
    guides,
    legal,
    nodes,
    orgs,
    outreach,
    alertes,
    portee,
    procedures,
    projects,
    runs,
    tenants,
    tokens,
    unipile,
    usage,
    users,
    visibility,
)

__all__ = [
    "billing",
    "connectors",
    "datastore",
    "emails",
    "embeddings",
    "grants",
    "guides",
    "legal",
    "nodes",
    "orgs",
    "outreach",
    "alertes",
    "portee",
    "procedures",
    "projects",
    "runs",
    "tenants",
    "tokens",
    "unipile",
    "usage",
    "users",
    "visibility",
]
