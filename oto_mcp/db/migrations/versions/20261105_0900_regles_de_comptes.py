"""Les règles d'accès des comptes de connecteur, leur journal, et la reprise des anciens partages.

oto-backend#1499, étape 3, lot L1 (docs/partage-de-compte.md § Les règles de comptes) :

- deux tables NEUVES, nées entières — le fragment `db/schema/connectors.ACCOUNT_RULES`,
  exécuté tel quel : `connector_account_rules` (une règle par compte et sujet) et
  `connector_account_share_events` (le journal) ;
- les fonctions et déclencheurs du journal et de la garde `owner_subject`
  (`regles_de_comptes.poser_journal_des_partages`, que le démarrage pose aussi) ;
- une sous-instance par compte Unipile (`sous_instances.poser_toutes`).

La REPRISE des anciens partages en règles ne vit plus ici : elle est figée dans la
révision de la bascule (`20261105_1000_bascule_comptes`, lot L3), qui la joue dans la
transaction qui retire les anciennes tables — la seule lecture qui leur reste. Une base
qui monte à la tête passe par les deux ; rien ne lisait les règles entre les deux.

**Verrous.** `AccessExclusiveLock` sur les deux tables neuves (vides) ; la clé
étrangère vers `connector_instances`, `orgs`, `users` et `org_groups` y prend un
`ShareRowExclusiveLock` bref ; `RowExclusiveLock` sur `connector_instances` pour les
sous-instances (quelques lignes). `lock_timeout` 5 s.

**Ordre avec le code : APRÈS le tag, de préférence.** Le démarrage pose déjà les tables,
les fonctions et les déclencheurs ; le code de ce lot pose la sous-instance d'un compte
Unipile connecté. Jouée avant, un compte connecté par l'ancien code entre-temps n'aurait
pas de sous-instance jusqu'au prochain passage de la reprise (L3). Prod et préprod
partagent la base.

Essai à blanc (`--sql`) : il imprime les tables, pas les sous-instances, qui lisent la base.

Retour arrière : retire les deux tables (règles et journal perdus), les fonctions, et
archive les sous-instances Unipile (motif `regles_de_comptes_retirees`).

Révision : 20261105_0900_regles_de_comptes
Précédente : 20261104_1200_runner_jobs_finis
"""
from __future__ import annotations

import logging

from alembic import context, op

from oto_mcp.db.schema.connectors import ACCOUNT_RULES

revision = "20261105_0900_regles_de_comptes"
down_revision = "20261104_1200_runner_jobs_finis"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"
logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(ACCOUNT_RULES)
    if context.is_offline_mode():
        return
    from oto_mcp.db import regles_de_comptes, sous_instances

    conn = op.get_bind().connection.driver_connection
    regles_de_comptes.poser_journal_des_partages(conn)
    logger.info("%s : sous-instances %s", revision, sous_instances.poser_toutes(conn))


def downgrade() -> None:
    from oto_mcp.db import regles_de_comptes, sous_instances

    op.execute(_ATTENTE_MAX)
    op.execute("DROP TABLE IF EXISTS connector_account_share_events")
    op.execute("DROP TABLE IF EXISTS connector_account_rules")
    for _, _, fonction in regles_de_comptes._DECLENCHEURS:
        op.execute(f"DROP FUNCTION IF EXISTS {fonction}()")
    op.execute(sous_instances.ARCHIVER_TOUTES)
