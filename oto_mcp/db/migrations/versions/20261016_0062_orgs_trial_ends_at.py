"""orgs : la date de fin d'essai de l'org, poussée par le commerce (`orgs.trial_ends_at`).

L'essai n'est PAS un droit déclaré (docs/droits-declares.md, décision du 16/10/2026) : il
appartient entièrement au service commerce, qui suspend l'org à sa fin
(`service.org.suspension`, motif `trial_ended`) et libère lui-même les comptes Unipile.
Il pousse ici la date de fin de chaque org (`service.org.trial`, `admin.org_trial`), pour
que la plateforme, les admins et les agents lisent la vraie date (`oto_admin_org
op=get`). Le cœur ne décide rien sur elle. NULL = pas d'essai en cours (jamais posé, ou
effacé quand l'org a payé).

Une colonne NULLABLE, sans défaut, sans index ni clé étrangère : pour PostgreSQL, une
écriture de catalogue seule, sans réécriture ni parcours. L'`ALTER` prend un
`AccessExclusiveLock` sur `orgs`, lue à presque chaque requête : d'où le `lock_timeout`
— un échec net, qu'on rejoue, plutôt qu'une file de lecteurs derrière nous (incident du
18/09 sur `orgs`, `docs/migrations-versionnees.md`).

Une base NEUVE la reçoit du `CREATE TABLE` (`db/schema/orgs.py`).

⚠️ **Ordre avec le code : AVANT la fusion.** Le code du même lot la LIT dans la fiche
d'org (`org_store.get_trial_ends_at`, lecture ciblée : `org.get`, `org.admin.get`,
`oto_admin_org op=get`) et l'écrit (`service.org.trial`, `admin.org_trial`) : jouée
après, ces gestes-là échoueraient en `UndefinedColumn` — `get_org`, lu partout, ne la lit
pas. L'ancien code l'ignore (ajout pur) : jouer la révision avant le
déploiement est sûr. Prod et préprod partagent la base.

Retour arrière : retire la colonne — les dates poussées sont perdues, le commerce les
repousse.

Révision : 0062_orgs_trial_ends_at
Précédente : 0061_valeurs_materialisees
"""
from __future__ import annotations

from alembic import op

revision = "0062_orgs_trial_ends_at"
down_revision = "0061_valeurs_materialisees"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("ALTER TABLE orgs DROP COLUMN IF EXISTS trial_ends_at")
