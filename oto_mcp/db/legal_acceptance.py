"""Store du journal d'acceptation des documents légaux (CGU/CGV/DPA).

APPEND-ONLY (table `legal_acceptances`) : chaque acceptation = une ligne figée
(slug + version + contexte + horodatage). Sert la preuve juridique « quel texte a
été accepté, par qui, quand » et la re-sollicitation au changement de version.

Le CATALOGUE des documents + versions courantes + jeux requis par contexte vit
dans `oto_mcp.legal_docs` (aligné sur les slugs/versions d'oto-websites) ; ce
module ne fait QUE persister et relire.
"""
from __future__ import annotations

from typing import Any, Optional

from ._conn import _connect


def record_legal_acceptance(
    sub: str,
    doc_slug: str,
    doc_version: str,
    context: str,
    *,
    org_id: Optional[int] = None,
    lang: Optional[str] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    """Insère une acceptation (append-only, jamais d'upsert)."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO legal_acceptances "
            "(sub, org_id, doc_slug, doc_version, context, lang, ip, user_agent) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (sub, org_id, doc_slug, doc_version, context, lang, ip, user_agent),
        )


def latest_acceptances(sub: str, org_id: Optional[int] = None) -> dict[str, dict[str, Any]]:
    """Dernière acceptation PAR slug pour ce sub (optionnellement scopée org).

    Portée : les lignes user-level (org_id IS NULL) ∪ les lignes de l'org donnée
    — une acceptation d'accès (user) vaut pour toutes les orgs ; une acceptation
    d'achat est rattachée à l'org qui souscrit. Renvoie `{slug: {version, accepted_at,
    context, org_id}}` (la plus récente gagne)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ON (doc_slug) doc_slug, doc_version, context, org_id, accepted_at "
            "FROM legal_acceptances "
            "WHERE sub = %s AND (org_id IS NULL OR org_id = %s) "
            "ORDER BY doc_slug, accepted_at DESC",
            (sub, org_id),
        ).fetchall()
    return {
        r["doc_slug"]: {
            "version": r["doc_version"],
            "context": r["context"],
            "org_id": r["org_id"],
            "accepted_at": r["accepted_at"],
        }
        for r in rows
    }


def has_accepted(sub: str, doc_slug: str, doc_version: str, org_id: Optional[int] = None) -> bool:
    """La version EXACTE d'un doc a-t-elle été acceptée par ce sub (user-level ∪ org) ?"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM legal_acceptances "
            "WHERE sub = %s AND doc_slug = %s AND doc_version = %s "
            "AND (org_id IS NULL OR org_id = %s) LIMIT 1",
            (sub, doc_slug, doc_version, org_id),
        ).fetchone()
    return row is not None
