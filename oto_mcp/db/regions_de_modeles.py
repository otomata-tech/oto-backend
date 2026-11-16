"""Where an org lets its agents' models run — column `orgs.allow_cn_models`.

**Models that run in China** (`oto_org_settings domain=model_regions`): a model whose
hosts include one in China (`runner_models.hebergement`, region `cn`) — the open models
served through OpenRouter. Allowed by default: the state of every org before the switch
existed, so nothing changes until an org admin turns it off. Off, those models are
marked `allowed: false` in the catalogue (the picker hides them), an agent cannot be
set on one (`_modele.exige_region_permise`), and a job already queued on one is stopped
at the claim with the reason written (`runner_jobs._avec_cle`).

The column is born in `CREATE TABLE orgs` (`db/schema/orgs.py`); an existing base gets
it from the revision `20261112_1900_allow_cn_models` or from the boot, same DDL
(`DDL_COLONNE`), in either order.

Not flattened into `db.*` — callers write `db.regions_de_modeles.<fn>`.
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect

COLONNE = "allow_cn_models"
DDL_COLONNE = (f"ALTER TABLE orgs ADD COLUMN IF NOT EXISTS {COLONNE} "
               "BOOLEAN NOT NULL DEFAULT TRUE")


def chine_permise(org_id: int) -> Optional[bool]:
    """May the org's agents run on a model that runs in China? None when the org does
    not exist."""
    with _connect() as conn:
        row = conn.execute(f"SELECT {COLONNE} AS v FROM orgs WHERE id = %s",
                           (int(org_id),)).fetchone()
    return None if row is None else bool(row["v"])


def poser_chine_permise(org_id: int, valeur: bool) -> bool:
    """Sets it. False if the org does not exist."""
    with _connect() as conn:
        row = conn.execute(f"UPDATE orgs SET {COLONNE} = %s WHERE id = %s RETURNING id",
                           (bool(valeur), int(org_id))).fetchone()
    return row is not None
