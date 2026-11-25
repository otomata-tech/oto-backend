"""La communauté (`community_moderation`, `community_installs`) — le stockage seul.

Commun à tout ce qui se publie dans la communauté : `kind` nomme le genre d'entrée
(`procedure` = une entrée de la bibliothèque publique de guides). Qui peut retirer,
restaurer, voir une entrée retirée se juge dans les capacités ; ce module ne filtre
rien et ne connaît aucun rôle.

Deux usages :
- les gestes : retirer / restaurer (`retirer`, `restaurer`), noter une installation
  (`noter_installation`), purger ce qui suit une entrée dépubliée (`purger`) ;
- les FRAGMENTS SQL qu'une requête de catalogue joint à sa table d'entrées
  (`jointures`, `ORDRES`), pour que le compte des installations et l'état de
  modération se lisent dans LA requête qui liste — jamais une requête par entrée.
"""
from __future__ import annotations

from typing import Any, Optional

from ._conn import _connect

KINDS = ("procedure", "app")
LISTED, REMOVED = "listed", "removed"

#: Fenêtre des installations « récentes » (`installs_30d`, tri `trending`).
FENETRE_RECENTE = "30 days"

#: Les tris du catalogue. `None` = le tri historique (dernière publication d'abord),
#: celui de tout appelant qui ne demande rien.
ORDRES = {
    None: "{e}.updated_at DESC, {e}.id DESC",
    "trending": "installs_30d DESC, installs_total DESC, {e}.updated_at DESC, {e}.id DESC",
    "most_added": "installs_total DESC, installs_30d DESC, {e}.updated_at DESC, {e}.id DESC",
    "new": "{e}.created_at DESC, {e}.id DESC",
}

_COLS_MOD = "state, reason, removed_by, removed_at, restored_by, restored_at"


def jointures(kind: str, alias: str) -> tuple[str, str]:
    """`(colonnes, jointures)` à poser dans un `SELECT … FROM <entrées> <alias>` :

    - colonnes : `moderation_state` ('listed' | 'removed'), `moderation_reason`,
      `removed_by`, `removed_at`, `installs_total`, `installs_30d` ;
    - jointures : l'état de modération (LEFT JOIN) et les comptes d'installations
      (LATERAL, servi par `idx_community_installs_entry`).

    `kind` est une constante du code (vérifiée), jamais une valeur d'appelant."""
    if kind not in KINDS:
        raise ValueError(f"kind de communauté inconnu : {kind!r}")
    cols = ("COALESCE(cm.state, 'listed') AS moderation_state, "
            "COALESCE(cm.reason, '') AS moderation_reason, "
            "cm.removed_by AS removed_by, cm.removed_at AS removed_at, "
            "COALESCE(ci.installs_total, 0) AS installs_total, "
            "COALESCE(ci.installs_30d, 0) AS installs_30d")
    joins = (
        f" LEFT JOIN community_moderation cm ON cm.kind = '{kind}' "
        f"AND cm.entry_id = {alias}.id"
        " LEFT JOIN LATERAL (SELECT COUNT(DISTINCT i.org_id) AS installs_total, "
        "COUNT(DISTINCT i.org_id) FILTER (WHERE i.created_at > NOW() - INTERVAL "
        f"'{FENETRE_RECENTE}') AS installs_30d FROM community_installs i "
        f"WHERE i.kind = '{kind}' AND i.entry_id = {alias}.id) ci ON TRUE")
    return cols, joins


def etat(kind: str, entry_id: int) -> dict[str, Any]:
    """L'état de modération d'une entrée : `{"state": "listed"}` sans ligne."""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_COLS_MOD} FROM community_moderation WHERE kind = %s AND entry_id = %s",
            (kind, int(entry_id))).fetchone()
    return dict(row) if row else {"state": LISTED, "reason": "", "removed_by": None,
                                  "removed_at": None, "restored_by": None,
                                  "restored_at": None}


def retirer(kind: str, entry_id: int, *, by: str, reason: str) -> dict[str, Any]:
    """Retire une entrée (idempotent : un nouveau retrait remplace motif, auteur, date)."""
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO community_moderation (kind, entry_id, state, reason, removed_by, "
            "removed_at) VALUES (%s, %s, 'removed', %s, %s, NOW()) "
            "ON CONFLICT (kind, entry_id) DO UPDATE SET state = 'removed', "
            "reason = EXCLUDED.reason, removed_by = EXCLUDED.removed_by, "
            f"removed_at = EXCLUDED.removed_at RETURNING {_COLS_MOD}",
            (kind, int(entry_id), reason, by)).fetchone()
    return dict(row)


def restaurer(kind: str, entry_id: int, *, by: str, reason: str) -> dict[str, Any]:
    """Reliste une entrée retirée. Garde le dernier retrait (`removed_*`) pour la
    trace ; le motif devient celui de la restauration. Sans ligne : déjà listée."""
    with _connect() as conn:
        row = conn.execute(
            "UPDATE community_moderation SET state = 'listed', reason = %s, "
            "restored_by = %s, restored_at = NOW() WHERE kind = %s AND entry_id = %s "
            f"RETURNING {_COLS_MOD}",
            (reason, by, kind, int(entry_id))).fetchone()
    return dict(row) if row else etat(kind, entry_id)


def noter_installation(kind: str, entry_id: int, *, org_id: int,
                       sub: Optional[str]) -> None:
    """Une installation réussie, dans `org_id`, par `sub`."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO community_installs (kind, entry_id, org_id, installed_by) "
            "VALUES (%s, %s, %s, %s)", (kind, int(entry_id), int(org_id), sub))


def purger(conn, kind: str, entry_id: int) -> None:
    """Ce qui suit une entrée DÉPUBLIÉE part avec elle, dans la transaction de
    l'appelant : son état de modération et ses installations."""
    conn.execute("DELETE FROM community_moderation WHERE kind = %s AND entry_id = %s",
                 (kind, int(entry_id)))
    conn.execute("DELETE FROM community_installs WHERE kind = %s AND entry_id = %s",
                 (kind, int(entry_id)))
