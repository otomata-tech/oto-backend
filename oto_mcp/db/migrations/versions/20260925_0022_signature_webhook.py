"""Webhook : l'authentification PAR SIGNATURE (Standard Webhooks), la déduplication,
le plafond journalier et l'adresse privée.

Trois gestes, tous additifs :

- `runner_triggers.hook_auth` (`bearer` par défaut) — le mode d'authentification d'un
  agent déclenché. `DEFAULT 'bearer'` : toute ligne existante garde exactement sa porte.
- `runner_triggers.hook_signing_secret_enc` — le secret de signature fourni par la
  source, CHIFFRÉ. NULL partout à la pose.
- `runner_triggers.max_per_day` (NULL = aucun plafond) et `runner_triggers.hook_slug`
  (NULL = adresse numérique) + son index unique partiel — les deux OPTIONNELS, NULL
  partout à la pose : tout agent existant garde exactement son comportement.
- `runner_hook_deliveries.external_id` + l'index unique PARTIEL
  `(trigger_id, external_id) WHERE external_id IS NOT NULL AND outcome IN
  ('queued','delayed')` — la clé de déduplication d'une source qui signe.

Le démarrage pose les mêmes objets (`db/_init.py`, `IF NOT EXISTS`) : idempotents l'un
envers l'autre.

⚠️ Prod et préprod partagent la MÊME base. L'ancien code ne lit aucune des trois
colonnes et n'écrit pas `external_id` (NULL, hors de l'index partiel) : jouer cette
révision avant le déploiement ne change rien pour lui. Le code du lot, lui, les LIT
(`_COLS` sert `hook_auth`, la route du porteur filtre `hook_auth = 'bearer'`) : à jouer
AVANT la fusion. `ADD COLUMN … DEFAULT` constant ne réécrit pas la table (PG ≥ 11).
L'index est construit sur une colonne entièrement NULL, donc vide : le verrou est bref,
borné par `lock_timeout`.

Révision : 0022_signature_webhook
Précédente : 0021_limites_du_run
"""
from __future__ import annotations

from alembic import op

revision = "0022_signature_webhook"
down_revision = "0021_limites_du_run"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("ALTER TABLE runner_triggers ADD COLUMN IF NOT EXISTS hook_auth TEXT "
               "NOT NULL DEFAULT 'bearer'")
    op.execute("ALTER TABLE runner_triggers ADD COLUMN IF NOT EXISTS "
               "hook_signing_secret_enc TEXT")
    op.execute("ALTER TABLE runner_hook_deliveries ADD COLUMN IF NOT EXISTS "
               "external_id TEXT")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_hook_deliveries_externe "
               "ON runner_hook_deliveries(trigger_id, external_id) "
               "WHERE external_id IS NOT NULL AND outcome IN ('queued', 'delayed')")
    op.execute("ALTER TABLE runner_triggers ADD COLUMN IF NOT EXISTS max_per_day INT")
    op.execute("ALTER TABLE runner_triggers ADD COLUMN IF NOT EXISTS hook_slug TEXT")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_runner_triggers_hook_slug "
               "ON runner_triggers(hook_slug) WHERE hook_slug IS NOT NULL")


def downgrade() -> None:
    # ⚠️ Retirer ces colonnes EFFACE les secrets de signature posés : les agents en
    # mode signature n'ouvriraient plus à personne. Le code qui les lit doit être
    # retiré avant.
    op.execute(_ATTENTE_MAX)
    op.execute("DROP INDEX IF EXISTS idx_runner_triggers_hook_slug")
    op.execute("ALTER TABLE runner_triggers DROP COLUMN IF EXISTS hook_slug")
    op.execute("ALTER TABLE runner_triggers DROP COLUMN IF EXISTS max_per_day")
    op.execute("DROP INDEX IF EXISTS idx_hook_deliveries_externe")
    op.execute("ALTER TABLE runner_hook_deliveries DROP COLUMN IF EXISTS external_id")
    op.execute("ALTER TABLE runner_triggers DROP COLUMN IF EXISTS hook_signing_secret_enc")
    op.execute("ALTER TABLE runner_triggers DROP COLUMN IF EXISTS hook_auth")
