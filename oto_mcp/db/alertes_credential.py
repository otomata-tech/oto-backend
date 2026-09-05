"""Le journal des clés retirées sous des agents programmés — écriture, drain, marquage.

Une ligne = un moment où quelqu'un a retiré un credential alors que des **agents
programmés actifs** de l'org en dépendaient (oto#59). Le DDL vit dans
`db/schema/alertes.py` ; ici, les trois gestes qu'on fait dessus.

⚠️ **L'écriture ne casse jamais le retrait qu'elle observe.** Le credential est déjà
parti quand on écrit : faire échouer l'appel laisserait l'appelant devant une erreur
alors que sa clé n'est plus là — le pire des deux mondes. Best-effort, journalisé,
jamais avalé en silence (`scripts/lint_silences.py` l'exigerait de toute façon).

⚠️ **Le drain regroupe par ORG, pas par ligne.** Trois clés retirées le même jour font
un courriel, pas trois : un destinataire qui reçoit trois messages pour un incident
apprend à les ignorer, et c'est exactement ce qu'on cherche à éviter.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from ._conn import _connect

logger = logging.getLogger(__name__)

#: Libellés d'agents gardés par ligne. Le COMPTE reste juste au-delà ; c'est le courriel
#: qu'on borne, pas la mesure.
MAX_AGENTS = 10


def enregistrer(*, org_id: int, connector: str, account: str, acteur_sub: Optional[str],
                agents: list) -> Optional[int]:
    """Écrit la disparition. Rend son id, ou `None` si l'écriture a échoué.

    `agents` = les libellés lisibles des déclencheurs actifs qui dépendaient de la clé.
    Rendre `None` plutôt que lever : l'appelant vient de RÉUSSIR un retrait légitime et
    n'a rien à faire de cette panne-là. Mais elle est journalisée — un `None` qu'on
    ignore, c'est une alerte dont on croira les chiffres."""
    libelles = [str(a) for a in (agents or []) if a][:MAX_AGENTS]
    try:
        with _connect() as conn:
            row = conn.execute(
                """INSERT INTO credential_disparitions
                     (org_id, connector, account, acteur_sub, agents_count, agents)
                   VALUES (%s, %s, %s, %s, %s, %s::jsonb) RETURNING id""",
                (int(org_id), connector, account or "", acteur_sub,
                 len(agents or []), json.dumps(libelles, ensure_ascii=False)),
            ).fetchone()
            return int(row["id"]) if row else None
    except Exception as e:  # noqa: BLE001
        logger.warning("alerte credential : enregistrement impossible (%s org %s) : %s",
                       connector, org_id, e)
        return None


def a_notifier() -> list[dict[str, Any]]:
    """Ce qui n'a pas encore été annoncé, **groupé par org**.

    Une org = un courriel, quel que soit le nombre de clés parties. Rend, par org : les
    ids à marquer, les connecteurs concernés, et le plus gros compte d'agents touchés —
    de quoi écrire un message sans relire la table."""
    with _connect() as conn:
        return list(conn.execute(
            """SELECT org_id,
                      array_agg(id ORDER BY created_at) AS ids,
                      array_agg(DISTINCT connector) AS connectors,
                      max(agents_count) AS agents_max,
                      min(created_at) AS depuis
                 FROM credential_disparitions
                WHERE notifie_at IS NULL
                GROUP BY org_id
                ORDER BY min(created_at)"""))


def marquer_notifie(ids: list) -> int:
    """Pose `notifie_at` sur les lignes annoncées. Rend le nombre marqué.

    ⚠️ Appelé APRÈS l'envoi, jamais avant : marquer d'abord transformerait un envoi
    raté en silence définitif, ce qui est précisément la panne que cette table existe
    pour supprimer."""
    if not ids:
        return 0
    with _connect() as conn:
        return conn.execute(
            "UPDATE credential_disparitions SET notifie_at = NOW() "
            "WHERE id = ANY(%s) AND notifie_at IS NULL",
            ([int(i) for i in ids],)).rowcount or 0
