"""Store des acceptations légales (`legal_acceptances`) — ré-exporté par `db/__init__`.

Une ligne par (sub, doc_slug) = la dernière version acceptée. Trace de consentement
UNIQUEMENT ; les métadonnées des docs vivent dans `legal_docs.py`.
"""
from __future__ import annotations

from ._conn import _connect


def get_legal_acceptances(sub: str) -> dict[str, dict]:
    """slug → {version, accepted_at} des docs acceptés par `sub` (dernière version)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT doc_slug, version, accepted_at FROM legal_acceptances WHERE sub = %s",
            (sub,),
        ).fetchall()
        return {r["doc_slug"]: {"version": r["version"], "accepted_at": r["accepted_at"]}
                for r in rows}


def record_legal_acceptances(sub: str, items: list[tuple[str, str]]) -> None:
    """Upsert (slug, version) pour `sub` — restampe `accepted_at` à maintenant."""
    if not items:
        return
    with _connect() as conn:
        for slug, version in items:
            conn.execute(
                "INSERT INTO legal_acceptances (sub, doc_slug, version, accepted_at) "
                "VALUES (%s, %s, %s, NOW()) "
                "ON CONFLICT (sub, doc_slug) DO UPDATE SET "
                "version = EXCLUDED.version, accepted_at = EXCLUDED.accepted_at",
                (sub, slug, version),
            )
