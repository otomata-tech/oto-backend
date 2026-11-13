"""Where an org lets its agents' models run — column `orgs.allow_cn_models`.

**Models that run in China** (`oto_org_settings domain=model_regions`): a model whose
hosts include one in China (`runner_models.hebergement`, region `cn`) — the open models
served through OpenRouter. NOT allowed by default (13/11/2026, revision
`20261113_2300_cn_models_opt_in`): agent turns carry customer data, so an org admin opts
in. The default catalogue runs in the US (DeepSeek V4.1 Flash, MiniMax M3); only DeepSeek
V4 Pro still has hosts in China. Off, those models are
marked `allowed: false` in the catalogue (the picker hides them), an agent cannot be
set on one (`_modele.exige_region_permise`), and a job already queued on one is stopped
at the claim with the reason written (`runner_jobs._avec_cle`).

The column is born in `CREATE TABLE orgs` (`db/schema/orgs.py`); an existing base gets
it from the revision `20261112_1900_allow_cn_models` or from the boot, same DDL
(`DDL_COLONNE`), in either order. The default moved from TRUE to FALSE in
`20261113_2300_cn_models_opt_in` (`DDL_DEFAUT`), which also turned every org off: no org
could run these models yet (no worker, no key).

Not flattened into `db.*` — callers write `db.regions_de_modeles.<fn>`.
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect

COLONNE = "allow_cn_models"
DDL_COLONNE = (f"ALTER TABLE orgs ADD COLUMN IF NOT EXISTS {COLONNE} "
               "BOOLEAN NOT NULL DEFAULT FALSE")
#: The default of a base where the column was born TRUE (`20261112_1900_allow_cn_models`).
DDL_DEFAUT = f"ALTER TABLE orgs ALTER COLUMN {COLONNE} SET DEFAULT FALSE"


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
