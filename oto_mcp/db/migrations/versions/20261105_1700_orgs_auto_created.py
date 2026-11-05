"""Plus d'org perso : la marque de l'org posée par l'inscription (`orgs.auto_created`).

docs/ownership.md § Plus d'org perso. Une colonne NEUVE, `BOOLEAN NOT NULL DEFAULT
FALSE` — le DDL est `db/org_d_inscription.py::DDL_COLONNE`, exécuté tel quel. TRUE =
l'org qu'`org_store.ensure_home_org` a posée à l'inscription d'un compte qui n'en avait
aucune ; tant que ce compte y est seul (`seule_d_inscription`), elle cède la place
d'org active à l'org qu'il rejoint et reste hors quota de créations.

Puis la révision MARQUE les orgs perso d'avant (`personal_of` renseigné,
`MARQUER_LES_ORGS_PERSO`) : sans elle, le code de ce lot, qui ne lit plus
`personal_of`, les prendrait pour des orgs choisies — un invité resterait sur son
ancien espace perso au lieu d'atterrir dans l'org qui l'invite, l'espace compterait
dans son quota, et l'export d'un périmètre refuserait le compte (membre d'une org hors
périmètre). `scripts/retirer_orgs_perso.py` rejoue le même marquage (idempotent).

**Verrous.** `AccessExclusiveLock` bref sur `orgs` (défaut constant non volatil :
catalogue seul, aucune réécriture, PostgreSQL ≥ 11), puis `RowExclusiveLock` pour le
marquage (une ligne par org perso vivante, sans bloquer les lectures).
`lock_timeout` 5 s : `orgs` est lue à chaque requête ; un échec net, qu'on rejoue.

**Ordre avec le code : APRÈS le tag** (`oto-mcp migrer upgrade head`). Le démarrage
pose la colonne avant de servir (même DDL, sous garde de catalogue) ; le marquage, lui,
n'est fait que par cette révision. Jouée avant le tag, l'ancien code ignore la colonne.

Retour arrière : retire la colonne (la marque se perd ; `personal_of`, que rien
n'efface ici, reste la source de l'ancien code).

Révision : 20261105_1700_orgs_auto_created
Précédente : 20261105_0900_regles_de_comptes
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.org_d_inscription import COLONNE, DDL_COLONNE, MARQUER_LES_ORGS_PERSO

revision = "20261105_1700_orgs_auto_created"
down_revision = "20261105_0900_regles_de_comptes"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_COLONNE)
    op.execute(MARQUER_LES_ORGS_PERSO)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE orgs DROP COLUMN IF EXISTS {COLONNE}")
