"""Consommation à usage unique des jetons d'upload signés (issue oto-backend#105).

Le jeton lui-même est STATELESS (HMAC signé, cf. `oto_mcp.upload_tokens`) : on ne
persiste que le `jti` déjà consommé, pour interdire le rejeu. TTL court côté jeton →
purge opportuniste des lignes anciennes à chaque consommation.
"""
from __future__ import annotations

from ._conn import _connect

# Bien au-delà du TTL du jeton (15 min) — simple filet pour que la table ne croisse pas.
_PRUNE_AFTER = "1 day"


def consume_upload_token(jti: str) -> bool:
    """Marque `jti` consommé. Renvoie True si c'était la 1re fois (upload autorisé),
    False si déjà utilisé (rejeu → refus). Purge au passage les jtis expirés."""
    if not jti:
        return False
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO upload_tokens_used (jti) VALUES (%s) "
            "ON CONFLICT (jti) DO NOTHING RETURNING jti",
            (jti,),
        ).fetchone()
        conn.execute(
            f"DELETE FROM upload_tokens_used WHERE used_at < NOW() - INTERVAL '{_PRUNE_AFTER}'"
        )
        return row is not None
