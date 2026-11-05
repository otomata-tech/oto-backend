"""connector_credentials : un prêt de compte Google ou Microsoft prête une LISTE de services.

Prêter un compte Google prêtait le compte entier, et chaque service que le prêteur
autorisait ensuite devenait accessible à l'emprunteur sans qu'il le sache. La colonne
`share_services JSONB NOT NULL DEFAULT '{}'` porte, pour chaque cible du `share_side`
(`user:<sub>`, `group:<id>`, `org:<id>`), la liste des services prêtés
(`access/prets.PORTEURS_A_SERVICES` : comptes Google et Microsoft seulement). À l'usage,
un service hors de la liste est refusé (`access/prets.exiger_service_prete`) ; une cible
sans liste n'a AUCUN service prêté.

**Les prêts existants sont figés ici**, une fois : chaque cible d'un prêt Google ou
Microsoft reçoit les services que le compte a autorisés AU MOMENT de la révision, lus sur
sa ligne (`meta.scopes`, `connectors/services_accordes`). Rien ne casse, et rien ne
s'élargit ensuite. Un compte dont la ligne ne dit aucun service autorisé reçoit une liste
vide : il ne servait rien de toute façon (un service non autorisé est refusé au compte
lui-même).

**Verrous.** L'`ALTER` avec un défaut constant est une écriture de catalogue seule (pas de
réécriture) ; il prend un `AccessExclusiveLock` sur `connector_credentials`, lue à chaque
appel de connecteur : `lock_timeout` 5 s, un échec net qu'on rejoue. Le remplissage met à
jour les seules lignes de membre prêtées d'un compte Google ou Microsoft (quelques-unes),
par leur clé primaire.

Une base NEUVE reçoit la colonne du `CREATE TABLE` (`db/schema/connectors.py`) et n'a
aucun prêt à figer.

⚠️ **Ordre avec le code : AVANT la fusion.** Le code du même lot lit la colonne à chaque
appel servi par un compte prêté et l'écrit à chaque prêt : jouée après, ces appels
échoueraient (`UndefinedColumn`), et un prêt existant joué avant le remplissage serait
refusé faute de liste. L'ancien code l'ignore (ajout pur) : la jouer avant le déploiement
est sûr. Prod et préprod partagent la base. Un prêt POSÉ par l'ancien code entre cette
révision et le déploiement n'a pas de liste : refusé à l'usage avec un message qui dit au
prêteur de prêter à nouveau — jamais servi en entier.

Retour arrière : retire la colonne — après le retrait du code qui la lit.

Révision : 0060_services_pretes
Précédente : 0059_transcription_key_mode
"""
from __future__ import annotations

import json
import logging

from alembic import context, op

revision = "0060_services_pretes"
down_revision = "0059_transcription_key_mode"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"
logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    from oto_mcp.access import PORTEURS_A_SERVICES
    from oto_mcp.connectors.services_accordes import services_autorises

    op.execute(_ATTENTE_MAX)
    op.execute("ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS "
               "share_services JSONB NOT NULL DEFAULT '{}'::jsonb")
    if context.is_offline_mode():
        # L'essai à blanc (`--sql`) n'a pas de base à lire : le remplissage dépend des
        # prêts en place, il ne s'imprime pas — il se lit au journal de la vraie montée.
        return
    conn = op.get_bind()
    if not conn.exec_driver_sql(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'connector_credentials' AND column_name = 'share_side'"
            ).first():
        # Une base née après la bascule des comptes (#1499, `20261105_1000`) n'a plus de
        # prêts : son schéma de démarrage ne porte pas `share_side`, rien à figer.
        return
    lignes = conn.exec_driver_sql(
        "SELECT entity_type, entity_id, connector, account, meta, share_side "
        "FROM connector_credentials WHERE entity_type = 'member' "
        "AND connector = ANY(%(porteurs)s) AND share_side <> '[]'::jsonb "
        "AND share_services = '{}'::jsonb",
        {"porteurs": sorted(PORTEURS_A_SERVICES)}).mappings().all()
    for ligne in lignes:
        services = services_autorises(ligne["connector"], ligne["meta"] or {})
        par_cible = {str(cible): services for cible in ligne["share_side"] or []}
        conn.exec_driver_sql(
            "UPDATE connector_credentials SET share_services = %(s)s::jsonb "
            "WHERE entity_type = %(t)s AND entity_id = %(i)s AND connector = %(c)s "
            "AND account = %(a)s",
            {"s": json.dumps(par_cible), "t": ligne["entity_type"], "i": ligne["entity_id"],
             "c": ligne["connector"], "a": ligne["account"]})
    logger.info("0060 : %d prêt(s) Google/Microsoft figé(s) sur leurs services autorisés",
                len(lignes))


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute("ALTER TABLE connector_credentials DROP COLUMN IF EXISTS share_services")
