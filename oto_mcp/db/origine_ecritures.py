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
            format_declare: bool = False, declare: bool = False) -> int:
    """Relève une écriture de couche `origine`. Rend le nombre de colonnes relevées.

    `declare` = l'appelant a passé le paramètre (`origine_override`). Compté à part,
    parce que c'est la seule façon de lire, après la date du refus, si un écrivain
    s'est ADAPTÉ ou a DISPARU : les écritures non déclarées tombent à zéro dans les
    deux cas, et un compteur unique les confondrait.

    Rend `0` plutôt que de lever : l'écriture observée est légitime, et l'appelant n'a
    rien à faire de cette panne-là. Mais elle est journalisée — un zéro silencieux,
    c'est un préavis dont on croira les chiffres."""
    if not colonnes:
        return 0
    try:
        with _connect() as conn:
            for colonne in colonnes:
                conn.execute(
                    """INSERT INTO origine_ecritures
                         (sub, org_id, ns_id, colonne, face, format_declare,
                          ecritures_declarees, derniere_declaree_at)
                       VALUES (%s, %s, %s, %s, %s, %s,
                               CASE WHEN %s THEN 1 ELSE 0 END,
                               CASE WHEN %s THEN now() END)
                       ON CONFLICT (COALESCE(sub, ''), ns_id, colonne) DO UPDATE SET
                         ecritures = origine_ecritures.ecritures + 1,
                         derniere_at = now(),
                         ecritures_declarees = origine_ecritures.ecritures_declarees
                                               + CASE WHEN %s THEN 1 ELSE 0 END,
                         -- ⚠️ `COALESCE` dans CET ordre : une écriture NON déclarée
                         -- ne doit pas effacer la date de la dernière déclarée, sinon
                         -- « s'est adapté puis a rechuté » se lirait « n'a jamais
                         -- déclaré ».
                         derniere_declaree_at = CASE WHEN %s THEN now()
                             ELSE origine_ecritures.derniere_declaree_at END,
                         -- La face de la DERNIÈRE écriture : si un même écrivain
                         -- passe des deux côtés, c'est un fait à voir, pas à figer
                         -- sur la première fois.
                         face = COALESCE(EXCLUDED.face, origine_ecritures.face)""",
                    (sub, org_id, int(ns_id), str(colonne), face,
                     bool(format_declare), bool(declare), bool(declare),
                     bool(declare), bool(declare)),
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

    ⚠️ **`declarees` et `derniere_declaree` répondent à la question d'APRÈS la date** :
    un écrivain dont les écritures non déclarées se sont arrêtées et dont les déclarées
    montent s'est adapté ; celui dont les deux se sont arrêtées a disparu — ou s'est
    cassé sans le dire. Le total seul ne distingue pas les deux.

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
            "       sum(ecritures_declarees) AS declarees, "
            "       max(derniere_declaree_at) AS derniere_declaree, "
            "       min(premiere_at) AS depuis, max(derniere_at) AS derniere "
            f"FROM origine_ecritures{clause} "
            "GROUP BY sub, format_declare ORDER BY max(derniere_at) DESC", tuple(args)).fetchall())
