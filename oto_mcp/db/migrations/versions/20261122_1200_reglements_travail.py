"""credit_movements : l'index partiel des règlements d'un TRAVAIL — les unités réglées par
agent.

La lentille `org.usage.tokens` (`runner_jobs.jetons_des_travaux`) rend, à côté des jetons
déclarés, les unités de budget que le grand livre a RÉGLÉES pour les tours de modèle de
ces travaux : les mouvements `settle` dont la `meta` porte `runner_job_id`
(`credits_tours.appariement`). Aucun index existant ne mène d'un travail à ses
règlements (ceux de la table servent l'idempotence d'un versement et le curseur du
commerce) : sans celui-ci, la seule voie est un parcours complet du grand livre.

`idx_credit_movements_travail ((meta->>'runner_job_id')) WHERE kind = 'settle' AND
meta->>'runner_job_id' IS NOT NULL` ne garde que les règlements de tours. Sa forme vit
dans `oto_mcp/db/credits.py` (`INDEX_REGLEMENTS_PAR_TRAVAIL`), partagée avec le démarrage
d'une base neuve ; le prédicat est celui de la requête, mot pour mot.

**Posé CONCURRENTLY**, sous le verdict commun (`db/index_concurrent.py`) : la révision
construit tant que `credit_movements` reste sous le seuil de taille ; au-delà, elle lève
`ConstructionManuelleRequise`, et le geste est manuel (docs/migrations-versionnees.md
§5.1) : `oto-mcp maintenance index-concurrents 20261122_1200_reglements_travail`, puis
`oto-mcp migrer upgrade head`. Rejouée après le geste, elle constate l'index valide et ne
fait rien. Un index INVALIDE lève `IndexInvalide`, jamais pris pour fait.

**Verrous.** `ShareUpdateExclusiveLock` sur `credit_movements` seulement : ni lectures
ni écritures bloquées (un règlement continue de s'écrire pendant la construction).
`lock_timeout` 5 min.

**Ordre avec le code : l'index AVANT la lentille, de préférence.** Sans lui, la lecture
reste juste mais parcourt le grand livre ligne à ligne (mesuré : ~9 s pour 1,6 M de
règlements, contre ~25 ms avec) : sur un gros grand livre, elle dépasse sa borne de durée
et `org.usage.tokens` répond `503 aggregate_timeout` jusqu'à ce que l'index soit posé.
Rien d'autre n'en dépend : aucune écriture, aucune réservation ne lit cet index.

Retour arrière : `DROP INDEX CONCURRENTLY IF EXISTS idx_credit_movements_travail`.

Révision : 20261122_1200_reglements_travail
Précédente : 20261119_1600_repar_consentie
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db import credits, index_concurrent

revision = "20261122_1200_reglements_travail"
down_revision = "20261119_1600_repar_consentie"
branch_labels = None
depends_on = None


def upgrade() -> None:
    index_concurrent.poser_par_revision(op, credits.INDEX_REGLEMENTS_PAR_TRAVAIL)


def downgrade() -> None:
    index_concurrent.retirer_par_revision(op, credits.INDEX_REGLEMENTS_PAR_TRAVAIL)
