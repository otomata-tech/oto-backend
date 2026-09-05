"""Relever qui pose la couche `origine` — l'instrument du préavis (oto#70 lot 2).

Le DDL vit dans `db/schema/origine.py` ; ici, les deux gestes qu'on fait dessus.

⚠️ **Le relevé ne casse jamais l'écriture qu'il observe.** L'appelant est en train de
réussir une écriture qui, aujourd'hui, est encore permise : une panne de relevé n'a pas
à la faire échouer. Best-effort, journalisé, jamais avalé en silence.

⚠️ **On compte une POPULATION, pas un trafic** : un upsert par (écrivain, tableau,
colonne). Le volume d'écritures peut être élevé et ne nous apprendrait rien de plus —
ce qu'on veut savoir, c'est combien d'écrivains il faudra prévenir, et depuis quand
chacun écrit.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ._conn import _connect

logger = logging.getLogger(__name__)


def relever(*, sub: Optional[str], org_id: Optional[int], ns_id: int,
            colonnes: list, face: Optional[str] = None,
            format_declare: bool = False) -> int:
    """Relève une écriture de couche `origine`. Rend le nombre de colonnes relevées.

    Rend `0` plutôt que de lever : l'écriture observée est légitime aujourd'hui, et
    l'appelant n'a rien à faire de cette panne-là. Mais elle est journalisée — un zéro
    silencieux, c'est un préavis dont on croira les chiffres."""
    if not colonnes:
        return 0
    try:
        with _connect() as conn:
            for colonne in colonnes:
                conn.execute(
                    """INSERT INTO origine_ecritures
                         (sub, org_id, ns_id, colonne, face, format_declare)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT (COALESCE(sub, ''), ns_id, colonne) DO UPDATE SET
                         ecritures = origine_ecritures.ecritures + 1,
                         derniere_at = now(),
                         -- La face de la DERNIÈRE écriture : si un même écrivain
                         -- passe des deux côtés, c'est un fait à voir, pas à figer
                         -- sur la première fois.
                         face = COALESCE(EXCLUDED.face, origine_ecritures.face)""",
                    (sub, org_id, int(ns_id), str(colonne), face,
                     bool(format_declare)),
                )
        return len(colonnes)
    except Exception as e:  # noqa: BLE001
        logger.warning("origine: relevé impossible (ns %s, %s) : %s",
                       ns_id, colonnes, e)
        return 0


def population(*, depuis: Optional[str] = None) -> list[dict[str, Any]]:
    """« Qui écrit encore l'origine, et depuis quand » — la lecture qui décidera de la
    longueur du préavis.

    Rend une ligne par (écrivain, cas) : combien de tableaux et de colonnes il touche,
    son volume, et surtout la FRAÎCHEUR de sa dernière écriture. Un écrivain qui n'a
    rien posé depuis six semaines n'appelle pas le même préavis que celui d'hier.

    ⚠️ **Groupé aussi par `format_declare`**, et c'est le point : `false` = une origine
    posée sur une colonne où la plateforme n'en pose jamais, donc forcément forgée par
    l'écrivain. C'est le cas que la définition interdit. Les fondre dans un total ferait
    disparaître la population qu'on cherche dans celle qui l'entoure."""
    clause, args = "", []
    if depuis:
        clause, args = " WHERE derniere_at >= %s::timestamptz", [depuis]
    with _connect() as conn:
        return list(conn.execute(
            "SELECT sub, format_declare, count(DISTINCT ns_id) AS tableaux, "
            "       count(*) AS colonnes, sum(ecritures) AS ecritures, "
            "       min(premiere_at) AS depuis, max(derniere_at) AS derniere "
            f"FROM origine_ecritures{clause} "
            "GROUP BY sub, format_declare ORDER BY max(derniere_at) DESC", tuple(args)).fetchall())
