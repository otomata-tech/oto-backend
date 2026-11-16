"""The models an org has turned off for its agents — column `orgs.disabled_models`.

**One switch per model** (`oto_org_settings domain=models`, REST `/api/orgs/{id}/models`):
an org admin turns a model of the catalogue (`runner_models.MODELES`) off or back on.
Empty by default: the state of every org before the switch existed, so nothing changes
until an admin turns one off. Off, the model is marked `allowed: false, disabled: true`
in the catalogue (the picker hides it, it is never the default), an agent cannot be set
on it (`_modele.exige_modele_permis` → `model_disabled`), an agent already on it no
longer runs — no fallback to another model: its occurrences are refused and named
(`refused_model_disabled`), a manual run is refused (`model_disabled`), a campaign stops
(`stop_reason = model_disabled`) — and a job already queued on it is stopped at the
claim with the reason written (`runner_jobs._avec_cle`).

The column is born in `CREATE TABLE orgs` (`db/schema/orgs.py`); an existing base gets
it from the revision `20261115_2100_disabled_models` or from the boot, same DDL
(`DDL_COLONNE`), in either order.

Not flattened into `db.*` — callers write `db.modeles_eteints.<fn>`.
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect

COLONNE = "disabled_models"
DDL_COLONNE = (f"ALTER TABLE orgs ADD COLUMN IF NOT EXISTS {COLONNE} "
               "TEXT[] NOT NULL DEFAULT '{}'")


def eteints(org_id: int) -> Optional[list[str]]:
    """The ids of the models the org turned off, sorted. None when the org does not
    exist."""
    with _connect() as conn:
        row = conn.execute(f"SELECT {COLONNE} AS v FROM orgs WHERE id = %s",
                           (int(org_id),)).fetchone()
    return None if row is None else sorted(row["v"] or [])


def est_eteint(org_id: Optional[int], model: Optional[str]) -> bool:
    """Has the org turned this model off? An unknown org or no model: False."""
    if not org_id or not model:
        return False
    return model in (eteints(int(org_id)) or [])


def poser(org_id: int, model: str, allume: bool) -> bool:
    """Turns one model on (removed from the list) or off (added, once). False if the org
    does not exist. Idempotent both ways."""
    sql = (f"UPDATE orgs SET {COLONNE} = array_remove({COLONNE}, %s) WHERE id = %s "
           "RETURNING id" if allume else
           f"UPDATE orgs SET {COLONNE} = CASE WHEN %s = ANY({COLONNE}) THEN {COLONNE} "
           f"ELSE array_append({COLONNE}, %s) END WHERE id = %s RETURNING id")
    params = (model, int(org_id)) if allume else (model, model, int(org_id))
    with _connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return row is not None


def agents_sur(org_id: int, model: str) -> list[int]:
    """The ids of the org's agents (`runner_triggers`) set on this model, paused ones
    included: switched back on, they would hit the same stop. Sorted."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id FROM runner_triggers WHERE org_id = %s AND model = %s ORDER BY id",
            (int(org_id), model)).fetchall()
    return [int(r["id"]) for r in rows]
