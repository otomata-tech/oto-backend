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
import logging
from typing import Optional

from . import connectors, crypto
from .db import _connect

logger = logging.getLogger(__name__)

USER = "user"
ORG = "org"


def _secret_kind(connector: str) -> str:
    c = connectors.REGISTRY.get(connector)
    return c.secret_kind if c else "api_key"


def _aad(entity_type: str, entity_id: str, connector: str) -> str:
    """AAD liant le ciphertext à SA ligne (anti-transplant)."""
    return f"connector_credentials:{entity_type}:{entity_id}:{connector}"


def get_credential(entity_type: str, entity_id: str, connector: str) -> Optional[str]:
    """Secret en CLAIR du connecteur pour cette entité, ou None. Déchiffrement
    JIT si la ligne est chiffrée (secret_enc) ; fallback plaintext (secret) pour
    les lignes non-migrées / chiffrement désactivé. Lève si connecteur non-keyed.

    Primitive de déchiffrement : appelée par resolve_api_key (résolution, injecte
    au connecteur) ET api_key_get (lecture de SA clé par le propriétaire).
    status_for utilise `has_credential` (présence, sans déchiffrer)."""
    connectors.require_keyed(connector)
    with _connect() as conn:
        row = conn.execute(
            "SELECT secret, secret_enc FROM connector_credentials "
            "WHERE entity_type = %s AND entity_id = %s AND connector = %s",
            (entity_type, entity_id, connector),
        ).fetchone()
    if not row:
        return None
    if row["secret_enc"]:
        try:
            return crypto.decrypt(row["secret_enc"], _aad(entity_type, entity_id, connector))
        except Exception:
            # Incident clé pendant le soak : si le plaintext est encore là
            # (pas encore nullé), fallback gracieux + warning ; sinon on relève
            # (échec bruyant — pas de secret silencieusement faux).
            if row["secret"] is not None:
                logger.warning(
                    "decrypt KO %s/%s/%s — fallback plaintext (soak) ; vérifier la master key",
                    entity_type, entity_id, connector,
                )
                return row["secret"]
            raise
    return row["secret"]


def has_credential(entity_type: str, entity_id: str, connector: str) -> bool:
    """Présence d'un secret SANS déchiffrer (pour status_for / surface d'attaque
    réduite : /api/me n'a besoin que du booléen, jamais de la valeur)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM connector_credentials WHERE entity_type = %s AND entity_id = %s "
            "AND connector = %s AND (secret IS NOT NULL OR secret_enc IS NOT NULL)",
            (entity_type, entity_id, connector),
        ).fetchone()
        return row is not None


def _upsert(conn, entity_type, entity_id, connector, secret, set_by, meta) -> None:
    # Chiffré → secret_enc porte le ciphertext, secret=NULL (jamais de plaintext
    # en clair pour un nouveau write). Désactivé → secret en clair, secret_enc=NULL.
    if crypto.encryption_enabled():
        plain, enc = None, crypto.encrypt(secret, _aad(entity_type, entity_id, connector))
    else:
        plain, enc = secret, None
    conn.execute(
        """
        INSERT INTO connector_credentials
            (entity_type, entity_id, connector, secret, secret_enc, secret_kind, meta, set_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (entity_type, entity_id, connector) DO UPDATE SET
            secret = EXCLUDED.secret,
            secret_enc = EXCLUDED.secret_enc,
            secret_kind = EXCLUDED.secret_kind,
            meta = EXCLUDED.meta,
            set_by = EXCLUDED.set_by,
            set_at = NOW()
        """,
        (entity_type, entity_id, connector, plain, enc, _secret_kind(connector),
         json.dumps(meta or {}), set_by),
    )


def encrypt_existing_rows(conn) -> int:
    """Migration (Phase 7) : chiffre en place les lignes encore en clair.

    Idempotent (WHERE secret_enc IS NULL). No-op si chiffrement désactivé.
    GARDE le plaintext (`secret`) pendant le soak — fallback/rollback ; nullé
    dans une étape ultérieure délibérée une fois le déchiffrement éprouvé.
    Tourne dans la transaction de l'appelant (init_db)."""
    if not crypto.encryption_enabled():
        return 0
    rows = conn.execute(
        "SELECT entity_type, entity_id, connector, secret FROM connector_credentials "
        "WHERE secret IS NOT NULL AND secret_enc IS NULL"
    ).fetchall()
    for r in rows:
        enc = crypto.encrypt(r["secret"], _aad(r["entity_type"], r["entity_id"], r["connector"]))
        conn.execute(
            "UPDATE connector_credentials SET secret_enc = %s "
            "WHERE entity_type = %s AND entity_id = %s AND connector = %s",
            (enc, r["entity_type"], r["entity_id"], r["connector"]),
        )
    return len(rows)


def verify_and_null_plaintext(conn) -> int:
    """Étape FINALE du soak (runbook prod, délibérée) : nulle le plaintext
    résiduel de connector_credentials. SELF-CHECK : déchiffre chaque secret_enc
    AVANT — si UNE ligne ne déchiffre pas, on LÈVE (abort, rollback) plutôt que
    de perdre le plaintext. À orchestrer avec le nulling des 9 colonnes
    users.<provider>_api_key + org_secrets côté db (cf. _drop_plaintext)."""
    rows = conn.execute(
        "SELECT entity_type, entity_id, connector, secret_enc FROM connector_credentials "
        "WHERE secret IS NOT NULL AND secret_enc IS NOT NULL"
    ).fetchall()
    for r in rows:
        crypto.decrypt(r["secret_enc"], _aad(r["entity_type"], r["entity_id"], r["connector"]))
    conn.execute("UPDATE connector_credentials SET secret = NULL WHERE secret_enc IS NOT NULL")
    return len(rows)


def _delete(conn, entity_type, entity_id, connector) -> bool:
    cur = conn.execute(
        "DELETE FROM connector_credentials "
        "WHERE entity_type = %s AND entity_id = %s AND connector = %s",
        (entity_type, entity_id, connector),
    )
    return (cur.rowcount or 0) > 0


def set_credential(
    entity_type: str,
    entity_id: str,
    connector: str,
    secret: str,
    set_by: Optional[str] = None,
    meta: Optional[dict] = None,
    conn=None,
) -> None:
    """Pose/rote le secret (UPSERT). secret_kind dérivé du registre.

    `conn` : si fourni, participe à la transaction de l'appelant (dual-write
    ATOMIQUE — le write legacy et le write canonique commitent ou rollback
    ensemble). Sinon ouvre sa propre transaction.
    """
    connectors.require_keyed(connector)
    if not secret:
        raise ValueError("secret requis")
    if conn is not None:
        _upsert(conn, entity_type, entity_id, connector, secret, set_by, meta)
    else:
        with _connect() as c:
            _upsert(c, entity_type, entity_id, connector, secret, set_by, meta)


def clear_credential(entity_type: str, entity_id: str, connector: str, conn=None) -> bool:
    """Supprime le credential. `conn` fourni → transaction de l'appelant."""
    if conn is not None:
        return _delete(conn, entity_type, entity_id, connector)
    with _connect() as c:
        return _delete(c, entity_type, entity_id, connector)


def list_credentials(entity_type: str, entity_id: str) -> list[dict]:
    """Connecteurs configurés pour l'entité — SANS le secret (jamais exposé)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT connector, secret_kind, set_by, set_at FROM connector_credentials "
            "WHERE entity_type = %s AND entity_id = %s ORDER BY connector",
            (entity_type, entity_id),
        ).fetchall()
        return [dict(r) for r in rows]
