"""tool_calls : la CLÉ d'un tour de modèle journalisé à son coût — un tour, une ligne.

`tours_au_cout.journaliser_tour` écrit une ligne de relevé (`kind='settlement'`) par appel
au modèle d'une famille au coût, payé par une clé partagée, et débite son coût au quota du
jour de cette clé. Un worker REJOUE un rapport resté sans réponse : lire « déjà écrit ? »
puis écrire laissait deux rapports simultanés passer tous les deux — deux lignes facturées,
deux débits. La clé `(kind, tool, call_uid)` sur ces seules lignes rend l'écriture
atomique : `INSERT … ON CONFLICT DO NOTHING` (`usage.inserer_tour_au_cout`), le second
rapport attend le premier puis n'écrit rien.

`idx_tool_calls_tour_au_cout`, UNIQUE, partiel sur `index_releve.PREDICAT_TOUR_AU_COUT` :
il ne porte que les relevés des familles au coût (aucune ligne avant cette révision). Sa
forme vit dans `oto_mcp/db/index_releve.py` (`TOUR_AU_COUT`), partagée avec le démarrage
d'une base neuve.

**Posé CONCURRENTLY**, sous le verdict commun (`db/index_concurrent.py`) : la révision
construit tant que `tool_calls` reste sous le seuil de taille ; au-delà (la base servie),
elle lève `ConstructionManuelleRequise` et le geste est manuel
(docs/migrations-versionnees.md §5.1) :
`oto-mcp maintenance index-concurrents 20261113_1200_tours_au_cout_cle`, puis
`oto-mcp migrer upgrade head`. Rejouée après le geste, elle constate l'index valide et ne
fait rien. Un index INVALIDE lève `IndexInvalide`, jamais pris pour fait.

**Verrous.** `ShareUpdateExclusiveLock` sur `tool_calls` seulement : ni lectures ni
écritures bloquées. `lock_timeout` 5 min.

**Ordre avec le code : l'index AVANT le premier tour au coût.** Sans lui, l'`ON CONFLICT`
ne trouve aucune contrainte et l'écriture du tour échoue : le worker s'arrête (503), rien
n'est servi sans dédoublonnage. Aucun agent en place n'en dépend : la famille est neuve.

Retour arrière : `DROP INDEX CONCURRENTLY IF EXISTS idx_tool_calls_tour_au_cout`.

Révision : 20261113_1200_tours_au_cout_cle
Précédente : 20261112_1900_allow_cn_models
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db import index_concurrent, index_releve

revision = "20261113_1200_tours_au_cout_cle"
down_revision = "20261112_1900_allow_cn_models"
branch_labels = None
depends_on = None


def upgrade() -> None:
    index_concurrent.poser_par_revision(op, index_releve.TOUR_AU_COUT)


def downgrade() -> None:
    index_concurrent.retirer_par_revision(op, index_releve.TOUR_AU_COUT)
