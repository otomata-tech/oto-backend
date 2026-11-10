"""L'issue qu'un agent a conclue de son travail, et sa note (`db/issues_des_travaux.py`).

Deux colonnes NEUVES, nullables, sans défaut, sur `runner_jobs` — posées aussi par le
démarrage, sous garde de catalogue, même DDL : `run_outcome` (l'issue la plus grave
déclarée par `run_finish` sous le jeton du travail) et `run_note` (sa note, bornée). Lues
et écrites seulement quand l'instance compte les runs bloqués (`OTO_RUNS_BLOQUES_COMPTES`).

**Verrous.** `AccessExclusiveLock` bref sur `runner_jobs` (colonnes nullables sans défaut :
catalogue seul, aucune réécriture). `lock_timeout` 5 s : le tick et la prise lisent cette
table en continu ; un échec net, qu'on rejoue, plutôt qu'une attente qui les bloquerait.

**Ordre avec le code : indifférent.** Le démarrage pose les mêmes colonnes ; l'ancien code
les ignore.

Retour arrière : retire les deux colonnes (les issues déclarées se perdent ; les travaux
restent).

Révision : 20261109_2105_issue_des_travaux
Précédente : 20261109_2100_slack_mention_only
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db import issues_des_travaux

revision = "20261109_2105_issue_des_travaux"
down_revision = "20261109_2100_slack_mention_only"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(issues_des_travaux.DDL_COLONNE_ISSUE)
    op.execute(issues_des_travaux.DDL_COLONNE_NOTE)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE runner_jobs DROP COLUMN IF EXISTS {issues_des_travaux.COLONNE_NOTE}")
    op.execute(f"ALTER TABLE runner_jobs DROP COLUMN IF EXISTS {issues_des_travaux.COLONNE_ISSUE}")
