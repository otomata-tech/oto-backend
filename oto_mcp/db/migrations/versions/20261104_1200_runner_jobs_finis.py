"""runner_jobs : l'index partiel des travaux CONCLUS d'une org — les jetons des travaux.

La lentille `org.usage.tokens` (`runner_jobs.jetons_des_travaux`) lit les travaux
conclus d'une org sur une fenêtre de `finished_at`. Sans index dédié, la seule voie est
un parcours complet de la file.

`idx_runner_jobs_conclus (org_id, finished_at) WHERE finished_at IS NOT NULL` ne garde
que les travaux conclus, rangés par (org, conclusion). Sa forme vit dans
`oto_mcp/db/runner_jobs.py` (`INDEX_CONCLUS`), partagée avec le démarrage d'une base
neuve ; le prédicat est celui de la requête, mot pour mot.

**Posé CONCURRENTLY**, sous le verdict commun (`db/index_concurrent.py`) : la révision
construit tant que `runner_jobs` reste sous le seuil de taille ; au-delà, elle lève
`ConstructionManuelleRequise`, et le geste est manuel (docs/migrations-versionnees.md
§5.1) : `oto-mcp maintenance index-concurrents 20261104_1200_runner_jobs_finis`, puis
`oto-mcp migrer upgrade head`. Rejouée après le geste, elle constate l'index valide et ne
fait rien. Un index INVALIDE lève `IndexInvalide`, jamais pris pour fait.

**Verrous.** `ShareUpdateExclusiveLock` sur `runner_jobs` seulement : ni lectures ni
écritures bloquées. `lock_timeout` 5 min.

**Ordre avec le code : indifférent.** La lecture répond juste sans l'index, plus
lentement (et sous sa borne de durée).

Retour arrière : `DROP INDEX CONCURRENTLY IF EXISTS idx_runner_jobs_conclus`.

Révision : 20261104_1200_runner_jobs_finis
Précédente : 20261103_1400_guide_revisions
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db import index_concurrent, runner_jobs

revision = "20261104_1200_runner_jobs_finis"
down_revision = "20261103_1400_guide_revisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    index_concurrent.poser_par_revision(op, runner_jobs.INDEX_CONCLUS)


def downgrade() -> None:
    index_concurrent.retirer_par_revision(op, runner_jobs.INDEX_CONCLUS)
