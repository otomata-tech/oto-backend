"""Le responsable d'une page et d'une procédure (docs/projects.md §Responsable d'une page).

Deux colonnes NEUVES, nullables, sans défaut — le DDL est `db/responsables.py`,
exécuté tel quel : `docs.owner_sub TEXT` et `org_instructions.owner_sub TEXT`. Le
compte de la personne à qui s'adresser au sujet de la page ou de la procédure ; NULL =
son auteur, LU comme tel (rien n'est recopié : l'état de toutes les lignes d'avant).

**Verrous.** Un `AccessExclusiveLock` bref sur `docs`, puis un sur `org_instructions`
(colonnes nullables sans défaut : catalogue seul, aucune réécriture). `lock_timeout`
5 s : les deux tables sont lues à chaque lecture de page ou de procédure ; un échec net,
qu'on rejoue, plutôt qu'une attente qui bloquerait ces lectures derrière elle.

**Ordre avec le code : indifférent.** Le code de ce lot lit les colonnes (`_DOC_COLS`,
`responsables.responsable_de_procedure`), mais le démarrage les pose avant de servir
(`ADD COLUMN IF NOT EXISTS`, même DDL, sous garde de catalogue) ; l'ancien code les
ignore.

Retour arrière : retire les deux colonnes (les responsables posés se perdent : chaque
page et chaque procédure revient à son auteur).

Révision : 20261120_1108_responsables
Précédente : 20261119_1600_repar_consentie
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.responsables import COLONNE, DDL_COLONNE_PAGES, DDL_COLONNE_PROCEDURES

revision = "20261120_1108_responsables"
down_revision = "20261119_1600_repar_consentie"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_COLONNE_PAGES)
    op.execute(DDL_COLONNE_PROCEDURES)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE org_instructions DROP COLUMN IF EXISTS {COLONNE}")
    op.execute(f"ALTER TABLE docs DROP COLUMN IF EXISTS {COLONNE}")
