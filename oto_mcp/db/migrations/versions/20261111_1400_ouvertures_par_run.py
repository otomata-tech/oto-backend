"""tool_calls : l'index partiel des ouvertures de runs PAR `run_id` — l'inertie d'un projet.

`usage.project_run_stats` (l'inertie de l'audit de liens, que `oto_project op=get` lit à
chaque ouverture d'un projet) cherche les ouvertures `run_start` des ≤ 500 derniers runs
d'un projet. Aucun index ne partait du `run_id` d'une ouverture : PostgreSQL lisait toutes
les ouvertures de la plateforme par `idx_tool_calls_run_start`, une à une dans le tas,
puis les hachait contre les runs du projet. Mesuré en production (lecture seule,
11/11/2026), projet de 2 218 runs : 60 ms juste après un échauffement, 3,3 s dix minutes plus tard, 33 s à froid ; 19 877 buffers. Les
réécritures sans index faisaient pire (LATERAL par run : 4 s ; jointure qui écarte
l'index d'outil : plus de 15 s).

`idx_tool_calls_run_start_run (run_id) WHERE tool = 'run_start' AND run_id IS NOT NULL`
ne garde que les ouvertures (~21 000 lignes) ; une sonde par run. Sa forme vit dans
`oto_mcp/db/index_releve.py` (`OUVERTURE_PAR_RUN`), partagée avec le démarrage d'une base
neuve ; son prédicat est celui des ouvertures (`PREDICAT_OUVERTURE`), que la requête
implique.

**Posé CONCURRENTLY**, sous le verdict commun (`db/index_concurrent.py`) : la révision
construit tant que `tool_calls` reste sous le seuil de taille ; au-delà (la base servie),
elle lève `ConstructionManuelleRequise` — la construction lit deux fois toute la table —,
et le geste est manuel (docs/migrations-versionnees.md §5.1) :
`oto-mcp maintenance index-concurrents 20261111_1400_ouvertures_par_run`, puis
`oto-mcp migrer upgrade head`. Rejouée après le geste, elle constate l'index valide et ne
fait rien. Un index INVALIDE lève `IndexInvalide`, jamais pris pour fait.

**Verrous.** `ShareUpdateExclusiveLock` sur `tool_calls` seulement : ni lectures ni
écritures bloquées, seulement un autre DDL ou un VACUUM sur la table. `lock_timeout` 5 min.

**Ordre avec le code : indifférent.** L'index seul ne suffit pas : sur la semi-jointure
d'avant, le planificateur, qui estime les ouvertures à quelques centaines, parcourrait ce
nouvel index EN ENTIER au lieu de l'autre. La lecture passe donc à une sonde par run
(`LATERAL … OFFSET 0`) — mais seulement quand l'index est là et valide
(`usage._ouverture_par_run_prete`, relu au plus toutes les 60 s) : sans lui, cette
sonde ferait pire (4 s mesurées), et le code garde la forme d'avant. Prod et préprod
partagent la base : posé une fois, il sert les deux.

Retour arrière : `DROP INDEX CONCURRENTLY IF EXISTS idx_tool_calls_run_start_run`.

Révision : 20261111_1400_ouvertures_par_run
Précédente : 20261111_0920_jetons_echeance
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db import index_concurrent, index_releve

revision = "20261111_1400_ouvertures_par_run"
down_revision = "20261111_0920_jetons_echeance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    index_concurrent.poser_par_revision(op, index_releve.OUVERTURE_PAR_RUN)


def downgrade() -> None:
    index_concurrent.retirer_par_revision(op, index_releve.OUVERTURE_PAR_RUN)
