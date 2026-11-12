"""docs : les index partiels qui traitent les sources brutes à part des pages de travail.

`docs.kind = 'source'` (transcriptions, imports) se verse en continu et en nombre : un
projet peut en porter des dizaines de milliers pour une centaine de pages de travail
(`doc`, `note`). La recherche par défaut et la liste par défaut ne parcourent plus les
sources ; ces quatre index partiels leur évitent de les LIRE :

- `idx_docs_travail_fts`, `idx_docs_travail_trgm` : les deux GIN de la recherche des
  pages (`db/search.py::INDEX_PAGES_DE_TRAVAIL`), sur le prédicat `kind <> 'source'`
  que `search_docs_fts` écrit à l'identique. Les GIN de la table entière restent : ils
  servent la recherche des sources ;
- `idx_docs_travail_projet (project_id) WHERE kind <> 'source'` : la liste par défaut ;
- `idx_docs_sources_recentes (project_id, created_at DESC, id DESC) WHERE kind = 'source'
  AND archived_at IS NULL` : la liste paginée des sources, son total, et la sonde de
  présence que la recherche signale (`db/projects.py`).

**Posés CONCURRENTLY**, sous le verdict commun (`db/index_concurrent.py`), seuil de
10 000 lignes : la révision construit sur une petite base ; au-delà (la base servie),
elle lève `ConstructionManuelleRequise` et le geste est manuel
(docs/migrations-versionnees.md §5.1) :
`oto-mcp maintenance index-concurrents 20261112_1200_docs_par_genre`, puis
`oto-mcp migrer upgrade head`. Rejouée après le geste, elle constate les index valides et
ne fait rien. Un index INVALIDE lève `IndexInvalide`, jamais pris pour fait.

**Verrous.** `ShareUpdateExclusiveLock` sur `docs` seulement : ni lectures ni écritures
bloquées. `lock_timeout` 5 min.

**Ordre avec le code : indifférent.** Sans eux, les requêtes répondent juste, par les
index de la table entière et le filtre de genre ligne à ligne.

Retour arrière : `DROP INDEX CONCURRENTLY IF EXISTS` des quatre.

Révision : 20261112_1200_docs_par_genre
Précédente : 20261111_1930_dataforseo_decrit
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db import index_concurrent, projects, search

revision = "20261112_1200_docs_par_genre"
down_revision = "20261111_1930_dataforseo_decrit"
branch_labels = None
depends_on = None

_INDEX = (*search.INDEX_PAGES_DE_TRAVAIL, projects.INDEX_TRAVAIL_PAR_PROJET,
          projects.INDEX_SOURCES_RECENTES)


def upgrade() -> None:
    for index in _INDEX:
        index_concurrent.poser_par_revision(op, index)


def downgrade() -> None:
    for index in reversed(_INDEX):
        index_concurrent.retirer_par_revision(op, index)
