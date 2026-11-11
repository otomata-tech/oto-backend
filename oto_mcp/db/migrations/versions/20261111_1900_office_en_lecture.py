"""Les partages d'org posés d'office sur les agents d'avant le 02/10 passent en lecture.

La révision `0035_agents_partages_a_l_org` a posé sur chaque agent d'alors un partage
`editor` à son org entière, marqué (`runner_triggers.MARQUE_MIGRATION_0035`), sans geste
d'une personne. Un agent tourne sous son propriétaire : l'écriture se confie nommément.

**Ce qui change.** Chaque partage d'org d'agent `editor` sous la marque 0035 devient
`viewer` (`permission = 'read'`) et prend la marque de cette révision
(`partages_office.MARQUE_RABATTU_EN_LECTURE`). Les membres gardent la vue et perdent
l'écriture. Le propriétaire et les admins de l'org gardent tout : leur droit ne passe
pas par ce partage. Un partage reposé par une personne a perdu la marque 0035 (l'upsert
réécrit `granted_by`) et ne bouge pas. Le choix des lignes est celui du compte à blanc
(`oto-mcp maintenance partages-office --dry-run`), une seule définition
(`db/partages_office.py`).

**Journal.** Le nombre de partages rabattus par org, une ligne par org (`org_id`, nombre)
et un total : des identifiants, jamais un nom ni un contenu. L'essai à blanc (`--sql`)
imprime l'ordre sans compter.

**Verrous.** `RowExclusiveLock` sur `resource_grants` et le verrou des seules lignes
visées ; la table est lue à chaque accès, d'où `lock_timeout` 5 s : un échec net, qu'on
rejoue, plutôt qu'une file de lectures bloquées.

**Ordre avec le code : indifférent.** Le code lit `role`/`permission` comme avant.

**Retour arrière : exact.** Les lignes rabattues portent la marque de cette révision ;
le retour les remet en `editor` sous la marque 0035. Un partage reposé ou retiré depuis
par une personne n'a plus cette marque et n'est pas touché.

Après le tag : `oto-mcp migrer upgrade head` par le lanceur
(docs/migrations-versionnees.md §5.1).

Révision : 20261111_1900_office_en_lecture
Précédente : 20261111_1400_ouvertures_par_run
"""
from __future__ import annotations

import logging

from alembic import context, op

from oto_mcp.db import partages_office

revision = "20261111_1900_office_en_lecture"
down_revision = "20261111_1400_ouvertures_par_run"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"
logger = logging.getLogger("alembic.runtime.migration")


def _jouer(ordre: str, verbe: str) -> None:
    op.execute(_ATTENTE_MAX)
    if context.is_offline_mode():
        op.execute(ordre)
        return
    compte = partages_office.par_org(
        op.get_bind().exec_driver_sql(ordre).mappings().all())
    for ligne in partages_office.lignes_du_compte(compte):
        logger.info("%s : partages d'org d'office %s : %s", revision, verbe, ligne)


def upgrade() -> None:
    _jouer(partages_office.RABATTRE, "rabattus en lecture")


def downgrade() -> None:
    _jouer(partages_office.RETABLIR, "remis en écriture")
