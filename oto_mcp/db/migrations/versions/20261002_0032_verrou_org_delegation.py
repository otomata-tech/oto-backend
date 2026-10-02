"""user_api_tokens : un jeton de délégation porte son travail et l'org de ce travail
(`job_id`, `verrou_org`, `verrou_org_id`).

Le jeton d'un travail du runner est émis au nom de son porteur, qui peut appartenir à
plusieurs orgs. L'org du travail, posée sur le jeton à l'émission, borne désormais
toutes ses résolutions (`oto_mcp/verrou_org.py`).

Trois colonnes NULLABLES, sans défaut ni index : une écriture de catalogue seule, sans
réécriture ni parcours. L'`ALTER` prend un `AccessExclusiveLock` sur `user_api_tokens`,
lue et écrite par chaque requête authentifiée par jeton : d'où le `lock_timeout` — un
échec net, qu'on rejoue — et pas de pose au démarrage (même raison que la révision
0007). Une base NEUVE les reçoit du `CREATE TABLE` (`db/schema/tokens.py`).

⚠️ Prod et préprod partagent la MÊME base. L'ancien code ne lit ni n'écrit ces
colonnes : jouer cette révision AVANT la fusion est sûr. L'inverse ne l'est pas — le
code du lot les lit à chaque authentification par jeton et répondrait
`UndefinedColumn` : **la révision s'applique AVANT la fusion** (main = préprod).
Retour arrière : les colonnes partent, les jetons de délégation en cours perdent leur
verrou jusqu'à la fin de leur bail (quelques minutes).

Révision : 0032_verrou_org_delegation
Précédente : 0031_selection_org_reelle
"""
from __future__ import annotations

from alembic import op

revision = "0032_verrou_org_delegation"
down_revision = "0031_selection_org_reelle"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"

_COLONNES = (("job_id", "BIGINT"), ("verrou_org", "BOOLEAN"),
             ("verrou_org_id", "BIGINT"))


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("ALTER TABLE user_api_tokens " + ", ".join(
        f"ADD COLUMN IF NOT EXISTS {nom} {type_}" for nom, type_ in _COLONNES))


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("ALTER TABLE user_api_tokens " + ", ".join(
        f"DROP COLUMN IF EXISTS {nom}" for nom, _ in _COLONNES))
