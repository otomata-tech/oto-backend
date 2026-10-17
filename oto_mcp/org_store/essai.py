"""La fin d'essai d'une org (`orgs.trial_ends_at`), poussée par le commerce.

L'essai n'est PAS un droit déclaré (docs/droits-declares.md) : il appartient au service
commerce, qui suspend l'org à sa fin (`service.org.suspension`) et libère lui-même les
comptes Unipile. Il pousse ici la date (`service.org.trial`), pour qu'elle se LISE
(la fiche d'org) — le cœur ne décide rien sur elle. Lecture ciblée plutôt qu'un
élargissement de `get_org` (même choix qu'`org_front`) : la colonne vient d'une révision
jouée à la main, et une base en retard ne casse alors que ce qui la sert.

Feuille du package : n'importe aucun frère.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..db import _connect


def set_trial_ends_at(org_id: int, ends_at: Optional[datetime]) -> bool:
    """Pose la fin d'essai de l'org (`None` l'efface : l'org a payé). Idempotent : True
    si la valeur a changé, False si elle était déjà celle-là ou si l'org n'existe pas.
    Une date sans fuseau est lue en UTC."""
    if ends_at is not None and ends_at.tzinfo is None:
        ends_at = ends_at.replace(tzinfo=timezone.utc)
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE orgs SET trial_ends_at = %(t)s "
            "WHERE id = %(id)s AND trial_ends_at IS DISTINCT FROM %(t)s",
            {"t": ends_at, "id": org_id})
        return cur.rowcount > 0


def get_trial_ends_at(org_id: int) -> Optional[str]:
    """La fin d'essai de l'org, servie en ISO 8601 au décalage UTC explicite ; `None` si
    aucun essai n'est en cours ou si l'org n'existe pas."""
    with _connect() as conn:
        row = conn.execute("SELECT trial_ends_at FROM orgs WHERE id = %s",
                           (org_id,)).fetchone()
    return trial_iso(row["trial_ends_at"]) if row else None


def trial_iso(v) -> Optional[str]:
    """La fin d'essai telle qu'on la SERT : ISO 8601 au décalage UTC explicite. Le store
    la rend en texte sans fuseau (`db._conn._str_dict_row`), en UTC puisque la session
    l'est — même règle que les dates servies au commerce (#1073)."""
    if v is None:
        return None
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).isoformat()
