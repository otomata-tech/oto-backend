"""Les blocages du jour poussés par le commerce (`spend_blocks`, `docs/limites-de-depense.md`).

Le commerce tient les limites de dépense qu'un admin d'org a fixées et compte la dépense
du jour ; le cœur ne connaît que la LISTE de ce qui est bloqué jusqu'à quand. Deux
gestes :

- `remplacer(org_id, blocs)` : la liste d'une org, posée d'un bloc dans une transaction
  (une liste vide lève tout). Idempotent : poser deux fois la même liste ne change rien.
- `actifs()` : tous les blocages dont la remise à zéro n'est pas passée, toutes orgs —
  la liste que `access/limite_du_jour` garde en mémoire, relue toutes les 30 s par
  processus. Petite table (une ligne par personne bloquée aujourd'hui).
"""
from __future__ import annotations

from datetime import datetime, timezone

from ._conn import _connect

#: La valeur de `sub` d'un blocage de TOUTE l'org (la clé primaire n'accepte pas NULL).
TOUTE_L_ORG = ""


def _date(v) -> datetime:
    """La fabrique de lignes rend les dates en texte UTC sans fuseau (`_conn._str_dict_row`,
    session en `TimeZone=UTC`) : on rend une date RÉELLE, comparable à maintenant."""
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _ligne(r) -> dict:
    d = dict(r)
    for k in ("until", "set_at"):
        if d.get(k) is not None:
            d[k] = _date(d[k])
    return d


def remplacer(org_id: int, blocs: list[dict]) -> list[dict]:
    """Remplace les blocages de `org_id` par `blocs` (`{sub, scope, group_id, until}`,
    `sub` = `TOUTE_L_ORG` pour l'org). Rend la liste posée, relue."""
    with _connect() as conn, conn.transaction():
        conn.execute("DELETE FROM spend_blocks WHERE org_id = %s", (org_id,))
        for b in blocs:
            conn.execute(
                "INSERT INTO spend_blocks (org_id, sub, scope, group_id, until) "
                "VALUES (%s, %s, %s, %s, %s)",
                (org_id, b["sub"], b["scope"], b.get("group_id"), b["until"]))
        return _de_l_org(conn, org_id)


def de_l_org(org_id: int) -> list[dict]:
    """Les blocages posés pour `org_id`, échus compris (ce que le commerce a poussé)."""
    with _connect() as conn:
        return _de_l_org(conn, org_id)


def _de_l_org(conn, org_id: int) -> list[dict]:
    return [_ligne(r) for r in conn.execute(
        "SELECT org_id, sub, scope, group_id, until, set_at FROM spend_blocks "
        "WHERE org_id = %s ORDER BY sub", (org_id,)).fetchall()]


def actifs() -> list[dict]:
    """Les blocages encore en cours, toutes orgs : `{org_id, sub, scope, group_id,
    until}`."""
    with _connect() as conn:
        return [_ligne(r) for r in conn.execute(
            "SELECT org_id, sub, scope, group_id, until FROM spend_blocks "
            "WHERE until > NOW()").fetchall()]

