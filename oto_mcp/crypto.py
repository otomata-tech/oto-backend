"""Chiffrement par enveloppe des secrets au repos.

AES-256-GCM. Master key **hors DB** : env `OTO_MCP_MASTER_KEY` (32 octets,
base64 ou hex) — `_load_master_key` est le SEUL point à swapper pour passer à
un KMS-unwrap-au-boot (Scaleway Key Manager) sans toucher le reste.

Chiffrement **obligatoire** : il n'existe plus aucune colonne plaintext (purge
2026-06-11). `encryption_enabled()` reste exposé, mais `encrypt`/`decrypt`
LÈVENT si la master key est absente → un serveur sans `OTO_MCP_MASTER_KEY` boote
mais tout write de credential échoue fort (pas de stockage en clair silencieux).

Enveloppe (base64) = key_ref(1o) ‖ nonce(12o) ‖ ciphertext+tag. L'AAD = identité
de la ligne (table:entity_type:entity_id:connector) lie le ciphertext à SA
ligne : un blob ne peut pas être transplanté vers un autre connecteur/entité.

« Dump Postgres = ciphertext only » : vrai. Tous les secrets vivent dans
`secret_enc` / `platform_keys.api_key_enc` ; aucune colonne plaintext résiduelle.

`_KEY_REF` est réservé à la ROTATION future (key-ring sélectionné sur blob[0]) —
non implémentée : une seule clé. Une clé erronée → InvalidTag (échec bruyant),
pas de mauvais déchiffrement silencieux. Perte de la master key = perte totale
des secrets chiffrés → la sauvegarder hors-DB (Secret Manager versionné +
escrow), sur un cycle de backup distinct de la DB.
"""
from __future__ import annotations

import base64
import os
import string
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_KEY_REF = b"\x01"  # version de master key (réservé rotation future)


def _load_master_key() -> Optional[bytes]:
    """Charge la master key depuis l'env (32 octets). None si absente.

    Accepte hex (64 chars) OU base64 — détection EXPLICITE du format (sinon une
    clé hex décode aussi en base64 → 48 octets → refus de boot). SEUL point à
    remplacer pour un KMS-unwrap-au-boot.
    """
    raw = os.environ.get("OTO_MCP_MASTER_KEY")
    if not raw:
        return None
    raw = raw.strip()
    if len(raw) == 64 and all(c in string.hexdigits for c in raw):
        key = bytes.fromhex(raw)
    else:
        key = base64.b64decode(raw, validate=True)
    if len(key) != 32:
        raise ValueError("OTO_MCP_MASTER_KEY doit décoder en 32 octets (AES-256 ; hex 64 chars ou base64)")
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
