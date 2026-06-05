"""Accès DB des credentials génériques (`connector_credentials`).

Source canonique des secrets de connecteurs keyed, per-entité (user OU org).
Remplace les 9 colonnes `users.<provider>_api_key` + la table `org_secrets`
(migrés en Phase 2 : dual-write puis cutover des lectures).

`secret` est en clair pour l'instant ; le chiffrement par enveloppe (Phase 7)
s'insère dans `get_credential`/`set_credential` sans changer les appelants — le
déchiffrement JIT vit dans `resolve_api_key`. Réutilise `db._connect` (comme
`org_store`) ; ne PAS importer depuis `db` les helpers haut-niveau (cycle).
"""
from __future__ import annotations

import json
from typing import Optional

from . import connectors
from .db import _connect

USER = "user"
ORG = "org"


def _secret_kind(connector: str) -> str:
    c = connectors.REGISTRY.get(connector)
    return c.secret_kind if c else "api_key"


def get_credential(entity_type: str, entity_id: str, connector: str) -> Optional[str]:
    """Secret du connecteur pour cette entité, ou None. Lève si connecteur
    inconnu/non-keyed (même verrou que l'ancien _check_provider)."""
    connectors.require_keyed(connector)
    with _connect() as conn:
        row = conn.execute(
            "SELECT secret FROM connector_credentials "
            "WHERE entity_type = %s AND entity_id = %s AND connector = %s",
            (entity_type, entity_id, connector),
        ).fetchone()
        return row["secret"] if row else None


def set_credential(
    entity_type: str,
    entity_id: str,
    connector: str,
    secret: str,
    set_by: Optional[str] = None,
    meta: Optional[dict] = None,
) -> None:
    """Pose/rote le secret (UPSERT). secret_kind dérivé du registre."""
    connectors.require_keyed(connector)
    if not secret:
        raise ValueError("secret requis")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO connector_credentials
                (entity_type, entity_id, connector, secret, secret_kind, meta, set_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (entity_type, entity_id, connector) DO UPDATE SET
                secret = EXCLUDED.secret,
                secret_kind = EXCLUDED.secret_kind,
                meta = EXCLUDED.meta,
                set_by = EXCLUDED.set_by,
                set_at = NOW()
            """,
            (entity_type, entity_id, connector, secret, _secret_kind(connector),
             json.dumps(meta or {}), set_by),
        )


def clear_credential(entity_type: str, entity_id: str, connector: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM connector_credentials "
            "WHERE entity_type = %s AND entity_id = %s AND connector = %s",
            (entity_type, entity_id, connector),
        )
        return (cur.rowcount or 0) > 0


def list_credentials(entity_type: str, entity_id: str) -> list[dict]:
    """Connecteurs configurés pour l'entité — SANS le secret (jamais exposé)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT connector, secret_kind, set_by, set_at FROM connector_credentials "
            "WHERE entity_type = %s AND entity_id = %s ORDER BY connector",
            (entity_type, entity_id),
        ).fetchall()
        return [dict(r) for r in rows]
