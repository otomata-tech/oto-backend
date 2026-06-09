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


def _aad(entity_type: str, entity_id: str, connector: str, account: str = "") -> str:
    """AAD liant le ciphertext à SA ligne (anti-transplant). Le segment `account`
    n'est ajouté que s'il est non vide → AAD INCHANGÉE pour le mono-compte
    (compat ascendante : un ciphertext mono-compte reste déchiffrable)."""
    base = f"connector_credentials:{entity_type}:{entity_id}:{connector}"
    return f"{base}:{account}" if account else base


def _reveal(row, entity_type: str, entity_id: str, connector: str, account: str) -> Optional[str]:
    """Secret en clair depuis une ligne : déchiffre `secret_enc` (fallback plaintext
    pendant le soak si la clé déraille), sinon `secret`. Primitive partagée par
    get_credential / get_credential_with_meta."""
    if row["secret_enc"]:
        try:
            return crypto.decrypt(row["secret_enc"], _aad(entity_type, entity_id, connector, account))
        except Exception:
            # Incident clé pendant le soak : si le plaintext est encore là
            # (pas encore nullé), fallback gracieux + warning ; sinon on relève
            # (échec bruyant — pas de secret silencieusement faux).
            if row["secret"] is not None:
                logger.warning(
                    "decrypt KO %s/%s/%s/%s — fallback plaintext (soak) ; vérifier la master key",
                    entity_type, entity_id, connector, account,
                )
                return row["secret"]
            raise
    return row["secret"]


def get_credential(entity_type: str, entity_id: str, connector: str, account: str = "") -> Optional[str]:
    """Secret en CLAIR du connecteur pour cette entité (et ce `account` pour le
    multi-compte ; '' = mono-compte), ou None. Déchiffrement JIT si la ligne est
    chiffrée (secret_enc) ; fallback plaintext (secret) pour les lignes
    non-migrées / chiffrement désactivé. Lève si le connecteur ne peut pas porter
    un credential à ce niveau d'entité (user→byo_user, org→org-partageable).

    Primitive de déchiffrement : appelée par resolve_api_key (résolution, injecte
    au connecteur) ET api_key_get (lecture de SA clé par le propriétaire).
    status_for utilise `has_credential` (présence, sans déchiffrer)."""
    connectors.require_credential(entity_type, connector)
    with _connect() as conn:
        row = conn.execute(
            "SELECT secret, secret_enc FROM connector_credentials "
            "WHERE entity_type = %s AND entity_id = %s AND connector = %s AND account = %s",
            (entity_type, entity_id, connector, account),
        ).fetchone()
    return _reveal(row, entity_type, entity_id, connector, account) if row else None


def get_credential_with_meta(entity_type: str, entity_id: str, connector: str,
                             account: str = "") -> Optional[dict]:
    """`{secret (déchiffré), meta, set_at}` ou None. Pour les connecteurs dont des
    satellites vivent dans `meta` : user_agent (linkedin/crunchbase),
    scopes/is_default (google). Même déchiffrement JIT que get_credential."""
    connectors.require_credential(entity_type, connector)
    with _connect() as conn:
        row = conn.execute(
            "SELECT secret, secret_enc, meta, set_at FROM connector_credentials "
            "WHERE entity_type = %s AND entity_id = %s AND connector = %s AND account = %s",
            (entity_type, entity_id, connector, account),
        ).fetchone()
    if not row:
        return None
    return {"secret": _reveal(row, entity_type, entity_id, connector, account),
            "meta": row["meta"] or {}, "set_at": row["set_at"]}


def credential_status(entity_type: str, entity_id: str, connector: str,
                      account: str = "") -> Optional[dict]:
    """Présence + satellites NON-secrets (`meta`, `set_at`) SANS déchiffrer — pour
    /api/me et autres surfaces de statut (mêmes garanties que has_credential :
    jamais la valeur du secret). None si aucun credential."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT meta, set_at, (secret IS NOT NULL OR secret_enc IS NOT NULL) AS configured "
            "FROM connector_credentials "
            "WHERE entity_type = %s AND entity_id = %s AND connector = %s AND account = %s",
            (entity_type, entity_id, connector, account),
        ).fetchone()
    if not row or not row["configured"]:
        return None
    return {"set_at": row["set_at"], "meta": row["meta"] or {}}


def has_credential(entity_type: str, entity_id: str, connector: str, account: Optional[str] = None) -> bool:
    """Présence d'un secret SANS déchiffrer (pour status_for / surface d'attaque
    réduite : /api/me n'a besoin que du booléen, jamais de la valeur).

    `account` None = n'importe quel compte (présence du connecteur, multi-compte
    inclus) ; '' = strictement le mono-compte ; une valeur = ce compte précis."""
    sql = ("SELECT 1 FROM connector_credentials WHERE entity_type = %s AND entity_id = %s "
           "AND connector = %s AND (secret IS NOT NULL OR secret_enc IS NOT NULL)")
    params: tuple = (entity_type, entity_id, connector)
    if account is not None:
        sql += " AND account = %s"
        params += (account,)
    with _connect() as conn:
        return conn.execute(sql + " LIMIT 1", params).fetchone() is not None


def _upsert(conn, entity_type, entity_id, connector, account, secret, set_by, meta) -> None:
    # Chiffré → secret_enc porte le ciphertext, secret=NULL (jamais de plaintext
    # en clair pour un nouveau write). Désactivé → secret en clair, secret_enc=NULL.
    if crypto.encryption_enabled():
        plain, enc = None, crypto.encrypt(secret, _aad(entity_type, entity_id, connector, account))
    else:
        plain, enc = secret, None
    conn.execute(
        """
        INSERT INTO connector_credentials
            (entity_type, entity_id, connector, account, secret, secret_enc, secret_kind, meta, set_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (entity_type, entity_id, connector, account) DO UPDATE SET
            secret = EXCLUDED.secret,
            secret_enc = EXCLUDED.secret_enc,
            secret_kind = EXCLUDED.secret_kind,
            meta = EXCLUDED.meta,
            set_by = EXCLUDED.set_by,
            set_at = NOW()
        """,
        (entity_type, entity_id, connector, account, plain, enc, _secret_kind(connector),
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
        "SELECT entity_type, entity_id, connector, account, secret FROM connector_credentials "
        "WHERE secret IS NOT NULL AND secret_enc IS NULL"
    ).fetchall()
    for r in rows:
        enc = crypto.encrypt(
            r["secret"], _aad(r["entity_type"], r["entity_id"], r["connector"], r["account"]))
        conn.execute(
            "UPDATE connector_credentials SET secret_enc = %s "
            "WHERE entity_type = %s AND entity_id = %s AND connector = %s AND account = %s",
            (enc, r["entity_type"], r["entity_id"], r["connector"], r["account"]),
        )
    return len(rows)


def verify_and_null_plaintext(conn) -> int:
    """Étape FINALE du soak (runbook prod, délibérée) : nulle le plaintext
    résiduel de connector_credentials. SELF-CHECK : déchiffre chaque secret_enc
    AVANT — si UNE ligne ne déchiffre pas, on LÈVE (abort, rollback) plutôt que
    de perdre le plaintext. À orchestrer avec le nulling des 9 colonnes
    users.<provider>_api_key + org_secrets côté db (cf. _drop_plaintext)."""
    rows = conn.execute(
        "SELECT entity_type, entity_id, connector, account, secret_enc FROM connector_credentials "
        "WHERE secret IS NOT NULL AND secret_enc IS NOT NULL"
    ).fetchall()
    for r in rows:
        crypto.decrypt(
            r["secret_enc"], _aad(r["entity_type"], r["entity_id"], r["connector"], r["account"]))
    conn.execute("UPDATE connector_credentials SET secret = NULL WHERE secret_enc IS NOT NULL")
    return len(rows)


def _delete(conn, entity_type, entity_id, connector, account) -> bool:
    cur = conn.execute(
        "DELETE FROM connector_credentials "
        "WHERE entity_type = %s AND entity_id = %s AND connector = %s AND account = %s",
        (entity_type, entity_id, connector, account),
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
    account: str = "",
) -> None:
    """Pose/rote le secret (UPSERT). secret_kind dérivé du registre. `account`
    discrimine le multi-compte ('' = mono-compte ; ex. email Google).

    `conn` : si fourni, participe à la transaction de l'appelant (dual-write
    ATOMIQUE — le write legacy et le write canonique commitent ou rollback
    ensemble). Sinon ouvre sa propre transaction.
    """
    connectors.require_credential(entity_type, connector)
    if not secret:
        raise ValueError("secret requis")
    if conn is not None:
        _upsert(conn, entity_type, entity_id, connector, account, secret, set_by, meta)
    else:
        with _connect() as c:
            _upsert(c, entity_type, entity_id, connector, account, secret, set_by, meta)


def clear_credential(entity_type: str, entity_id: str, connector: str, conn=None,
                     account: str = "") -> bool:
    """Supprime le credential (ce `account` ; '' = mono-compte). `conn` fourni →
    transaction de l'appelant."""
    if conn is not None:
        return _delete(conn, entity_type, entity_id, connector, account)
    with _connect() as c:
        return _delete(c, entity_type, entity_id, connector, account)


def list_credentials(entity_type: str, entity_id: str) -> list[dict]:
    """Connecteurs configurés pour l'entité — SANS le secret (jamais exposé).
    Une ligne par (connector, account) : le multi-compte apparaît en N lignes."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT connector, account, secret_kind, set_by, set_at FROM connector_credentials "
            "WHERE entity_type = %s AND entity_id = %s ORDER BY connector, account",
            (entity_type, entity_id),
        ).fetchall()
        return [dict(r) for r in rows]
