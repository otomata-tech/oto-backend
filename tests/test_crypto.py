"""Chiffrement par enveloppe des secrets (`crypto.py`) — le socle du coffre.

Module pur (pas de DB, master key lue de l'env à chaque appel) → testable sans
stub. On exerce le VRAI chemin AES-256-GCM : round-trip, liaison AAD↔ligne
(anti-transplant), rejet d'une clé périmée / d'un blob corrompu, échec bruyant
quand la master key manque, et le parsing hex/base64 de la clé.

Ce fichier comblait un trou critique : `crypto.py` n'avait AUCUN test alors qu'il
garde tout le coffre (une régression ici = perte ou fuite de credentials).
"""
from __future__ import annotations

import base64

import pytest

from oto_mcp import crypto

# 32 octets → AES-256. Deux encodages acceptés par `_load_master_key`.
_KEY_HEX = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
_KEY_B64 = base64.b64encode(bytes.fromhex(_KEY_HEX)).decode()
_OTHER_KEY_B64 = base64.b64encode(b"\x11" * 32).decode()


@pytest.fixture
def key(monkeypatch):
    """Master key présente (base64) pour la durée du test."""
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", _KEY_B64)


# --- round-trip + format d'enveloppe ---------------------------------------

def test_round_trip(key):
    aad = "connector_credentials:user:u1:pennylane"
    env = crypto.encrypt("s3cr3t-value", aad)
    assert crypto.decrypt(env, aad) == "s3cr3t-value"


def test_envelope_starts_with_key_ref_byte(key):
    """blob = key_ref(1o) ‖ nonce(12o) ‖ ct+tag → le 1er octet = _KEY_REF."""
    blob = base64.b64decode(crypto.encrypt("x", "aad"))
    assert blob[:1] == crypto._KEY_REF
    assert len(blob) >= 1 + 12 + 16  # key_ref + nonce + tag GCM au minimum


def test_nonce_is_random_per_encrypt(key):
    """Deux chiffrements du même plaintext/AAD diffèrent (nonce aléatoire) —
    sinon réutilisation de nonce = faille GCM."""
    a = crypto.encrypt("same", "aad")
    b = crypto.encrypt("same", "aad")
    assert a != b
    assert crypto.decrypt(a, "aad") == crypto.decrypt(b, "aad") == "same"


def test_unicode_plaintext_round_trips(key):
    secret = "clé-privée-éàü-🔑"
    env = crypto.encrypt(secret, "aad")
    assert crypto.decrypt(env, "aad") == secret


# --- liaison AAD (anti-transplant de credential) ---------------------------

def test_wrong_aad_is_rejected(key):
    """L'AAD lie le ciphertext à SA ligne : déchiffrer avec une autre identité
    de ligne échoue (un blob ne se transplante pas vers un autre connecteur)."""
    env = crypto.encrypt("secret", "connector_credentials:user:u1:pennylane")
    with pytest.raises(RuntimeError, match="indéchiffrable"):
        crypto.decrypt(env, "connector_credentials:user:u2:pennylane")


# --- clé périmée / blob corrompu → échec bruyant, jamais silencieux --------

def test_wrong_key_raises_readable_error(monkeypatch, key):
    env = crypto.encrypt("secret", "aad")
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", _OTHER_KEY_B64)
    # InvalidTag (str() vide) doit être traduit en message actionnable, pas remonter nu.
    with pytest.raises(RuntimeError, match="reconnecte ce connecteur"):
        crypto.decrypt(env, "aad")


def test_tampered_ciphertext_is_rejected(key):
    blob = bytearray(base64.b64decode(crypto.encrypt("secret", "aad")))
    blob[-1] ^= 0xFF  # flip le dernier octet du tag
    with pytest.raises(RuntimeError, match="indéchiffrable"):
        crypto.decrypt(base64.b64encode(bytes(blob)).decode(), "aad")


# --- master key absente : boot OK mais tout crypto échoue fort -------------

def test_encryption_enabled_reflects_env(monkeypatch, key):
    assert crypto.encryption_enabled() is True
    monkeypatch.delenv("OTO_MCP_MASTER_KEY", raising=False)
    assert crypto.encryption_enabled() is False


def test_encrypt_without_key_raises(monkeypatch):
    monkeypatch.delenv("OTO_MCP_MASTER_KEY", raising=False)
    with pytest.raises(RuntimeError, match="chiffrement indisponible"):
        crypto.encrypt("secret", "aad")


def test_decrypt_without_key_raises(monkeypatch):
    """FAUX négatif classique (CLAUDE.md) : un env sans master key lève
    RuntimeError « indisponible » — ≠ InvalidTag (clé périmée)."""
    monkeypatch.delenv("OTO_MCP_MASTER_KEY", raising=False)
    with pytest.raises(RuntimeError, match="déchiffrement impossible"):
        crypto.decrypt("Zm9v", "aad")


# --- parsing de la master key (hex vs base64, longueur) --------------------

def test_key_accepts_hex_and_base64_equivalently(monkeypatch):
    """hex 64 chars et son base64 encodent la MÊME clé → un blob chiffré sous
    l'un se déchiffre sous l'autre."""
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", _KEY_HEX)
    env = crypto.encrypt("secret", "aad")
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", _KEY_B64)
    assert crypto.decrypt(env, "aad") == "secret"


def test_key_wrong_length_is_refused(monkeypatch):
    """Une clé qui ne décode pas en 32 octets = refus de boot (pas d'AES-256
    silencieusement affaibli)."""
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", base64.b64encode(b"tooshort").decode())
    with pytest.raises(ValueError, match="32 octets"):
        crypto.encryption_enabled()


def test_key_whitespace_is_stripped(monkeypatch):
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", f"  {_KEY_B64}\n")
    env = crypto.encrypt("secret", "aad")
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", _KEY_HEX)
    assert crypto.decrypt(env, "aad") == "secret"
