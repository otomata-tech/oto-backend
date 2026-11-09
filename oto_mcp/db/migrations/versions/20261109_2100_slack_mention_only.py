"""Hors d'un DM, l'app Slack ne répond qu'à qui la tague — le réglage d'org
(`orgs.slack_mention_only`, docs/runner-et-automatisations.md § Une réponse au message
d'un agent).

Une colonne NEUVE, `BOOLEAN NOT NULL DEFAULT FALSE` — le DDL est
`db/slack_reglage.py::DDL_COLONNE`, exécuté tel quel. FALSE = le comportement du
29/10/2026, celui de toutes les orgs d'avant : une réponse dans le fil d'un message
d'agent le relance sans mention. Seul un admin de l'org la pose (`oto_org_settings
domain=slack`).

**Verrous.** `AccessExclusiveLock` bref sur `orgs` (défaut constant non volatil :
catalogue seul, aucune réécriture, PostgreSQL ≥ 11). `lock_timeout` 5 s : `orgs` est
lue à chaque requête ; un échec net, qu'on rejoue.

**Ordre avec le code : indifférent.** Le démarrage pose la colonne avant de servir
(même DDL, sous garde de catalogue) ; l'ancien code l'ignore.

Retour arrière : retire la colonne (les orgs qui l'avaient posée reviennent au
comportement par défaut).

Révision : 20261109_2100_slack_mention_only
Précédente : 20261108_1200_procedure_comments
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.slack_reglage import COLONNE, DDL_COLONNE

revision = "20261109_2100_slack_mention_only"
down_revision = "20261108_1200_procedure_comments"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_COLONNE)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE orgs DROP COLUMN IF EXISTS {COLONNE}")
