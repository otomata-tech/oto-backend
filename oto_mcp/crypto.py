"""Chiffrement par enveloppe des secrets au repos (Phase 7).

AES-256-GCM. Master key **hors DB** : env `OTO_MCP_MASTER_KEY` (32 octets,
base64 ou hex) — `_load_master_key` est le SEUL point à swapper pour passer à
un KMS-unwrap-au-boot (Scaleway Key Manager) sans toucher le reste.

GARDE-FOU déploiement : si la master key n'est pas posée, le chiffrement est
DÉSACTIVÉ (`encryption_enabled()` False) — les secrets restent en clair. Permet
de déployer ce code en NO-OP, puis d'activer le chiffrement en provisionnant la
clé (la migration chiffre alors les lignes existantes au boot suivant).

Enveloppe (base64) = key_ref(1o) ‖ nonce(12o) ‖ ciphertext+tag. L'AAD = identité
de la ligne (table:entity_type:entity_id:connector) lie le ciphertext à SA
ligne : un blob ne peut pas être transplanté vers un autre connecteur/entité.
Un dump Postgres seul ne livre que du ciphertext.
"""
from __future__ import annotations

import base64
import os
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_KEY_REF = b"\x01"  # version de master key (pour la rotation future)


def _load_master_key() -> Optional[bytes]:
    """Charge la master key depuis l'env (32 octets). None si absente.

    SEUL point à remplacer pour un KMS-unwrap-au-boot : renvoyer ici la clé
    déchiffrée une fois par le KMS au démarrage, au lieu de la lire en env.
    """
    raw = os.environ.get("OTO_MCP_MASTER_KEY")
    if not raw:
        return None
    raw = raw.strip()
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception:
        key = bytes.fromhex(raw)
    if len(key) != 32:
        raise ValueError("OTO_MCP_MASTER_KEY doit décoder en 32 octets (AES-256)")
    return key


def encryption_enabled() -> bool:
    return _load_master_key() is not None


def encrypt(plaintext: str, aad: str) -> str:
    key = _load_master_key()
    if key is None:
        raise RuntimeError("OTO_MCP_MASTER_KEY absente — chiffrement indisponible")
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), aad.encode())
    return base64.b64encode(_KEY_REF + nonce + ct).decode()


def decrypt(envelope: str, aad: str) -> str:
    key = _load_master_key()
    if key is None:
        raise RuntimeError("OTO_MCP_MASTER_KEY absente — déchiffrement impossible")
    blob = base64.b64decode(envelope)
    # blob[:1] = key_ref (réservé au versioning/rotation) ; [1:13] = nonce.
    nonce, ct = blob[1:13], blob[13:]
    return AESGCM(key).decrypt(nonce, ct, aad.encode()).decode()
