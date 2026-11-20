"""tool_calls : l'index partiel des appels qui ont désigné un tableau par son NOM.

Un nom de tableau est refusé (`datastore_by_name`) ; il faut
prouver qu'aucun appelant ne s'en sert plus, sur la face MCP comme sur la face REST, par
org et par principal. Le journal citait le tableau RÉSOLU (`ns_id`, nom canonique) : `957`,
`slot:vivier` et `mes_leads` y laissaient la même trace. Il porte désormais la FORME de la
désignation, `args.ref_kind` ∈ `number`|`slot`|`name` (`session_org.noter_forme_tableau`),
jamais le nom.

`idx_tool_calls_ref_nom (created_at DESC) WHERE args->>'ref_kind' = 'name'` ne garde que
les désignations par nom : la mesure (`index_releve.MESURE_TABLEAU_PAR_NOM`) lit ses N
derniers jours sans ouvrir le reste du journal. Sa forme vit dans
`oto_mcp/db/index_releve.py` (`TABLEAU_PAR_NOM`), partagée avec le démarrage d'une base
neuve.

**Posé CONCURRENTLY**, sous le verdict commun (`db/index_concurrent.py`) : la révision
construit tant que `tool_calls` reste sous le seuil de taille ; au-delà (la base servie),
elle lève `ConstructionManuelleRequise` et le geste est manuel
(docs/migrations-versionnees.md §5.1) :
`oto-mcp maintenance index-concurrents 20261120_2210_tableau_par_nom`, puis
`oto-mcp migrer upgrade head`. Rejouée après le geste, elle constate l'index valide et ne
fait rien. Un index INVALIDE lève `IndexInvalide`.

**Verrous.** `ShareUpdateExclusiveLock` sur `tool_calls` seulement : ni lectures ni
écritures bloquées. `lock_timeout` 5 min.

**Ordre avec le code : indifférent.** Sans le code, aucune ligne ne porte `ref_kind` et
l'index reste vide ; sans l'index, la clé s'écrit et la mesure reste juste, seulement plus
lente. Prod et préprod partagent la base : posé une fois, il sert les deux.

Retour arrière : `DROP INDEX CONCURRENTLY IF EXISTS idx_tool_calls_ref_nom`.

Révision : 20261120_2210_tableau_par_nom
Précédente : 20261119_1600_repar_consentie
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db import index_concurrent, index_releve

revision = "20261120_2210_tableau_par_nom"
down_revision = "20261119_1600_repar_consentie"
branch_labels = None
depends_on = None


def upgrade() -> None:
    index_concurrent.poser_par_revision(op, index_releve.TABLEAU_PAR_NOM)


def downgrade() -> None:
    index_concurrent.retirer_par_revision(op, index_releve.TABLEAU_PAR_NOM)
