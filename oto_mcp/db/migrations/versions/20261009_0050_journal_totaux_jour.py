"""Totaux du journal par jour UTC : registre des jours consolidés, totaux, jobs distincts.

oto-backend#1147 (étape « totaux par jour », décision du 09/10/2026). Les écrans de
consommation et de monitoring lisent les jours consolidés et le journal direct pour le
reste de la fenêtre (`db/journal_jour.py`), au lieu de relire `tool_calls` entier.

Trois tables NEUVES, nées entières, et quatre index sur elles — le DDL est le fragment
`db/schema/usage.py::JOURNAL_JOUR`, exécuté tel quel (une seule écriture) :

- `journal_jours_consolides (jour PK, lignes, consolide_at)` — le registre ;
- `journal_totaux_jour (jour → registre ON DELETE CASCADE, kind, org_id, sub, tool, ok,
  key_mode, client_name, appels, quantite, duree_n, duree_somme, durees int[], taille_n,
  taille_somme, tailles int[], dernier_at)` + index `(jour)`, `(org_id, jour)`,
  `(sub, jour)` ;
- `journal_jobs_jour (jour → registre ON DELETE CASCADE, org_id, tool, key_mode, job_id)`
  + index `(org_id, jour)`.

**Verrous.** `AccessExclusiveLock` sur les trois tables neuves seulement (vides,
instantané). Aucune table servie n'est touchée : ni `tool_calls`, ni réécriture, ni
parcours. Les deux clés étrangères visent le registre, neuf lui aussi.

**Ordre avec le code : indifférent.** Le démarrage pose les mêmes tables (`CREATE TABLE
IF NOT EXISTS`, même fragment), y compris sur une base existante. Le code qui les
ALIMENTE (le travail de maintenance `journal-jour`, le script de rattrapage) arrive dans
le même commit ; le code qui les LIT arrive après, une fois l'historique rattrapé. Prod
et préprod partagent la base : posées une fois, elles servent les deux.

Retour arrière : retire les trois tables (les totaux se recalculent du journal).

Révision : 0050_journal_totaux_jour
Précédente : 0049_tool_calls_ouvertures_runs
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.usage import JOURNAL_JOUR

revision = "0050_journal_totaux_jour"
down_revision = "0049_tool_calls_ouvertures_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(JOURNAL_JOUR)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS journal_jobs_jour")
    op.execute("DROP TABLE IF EXISTS journal_totaux_jour")
    op.execute("DROP TABLE IF EXISTS journal_jours_consolides")
