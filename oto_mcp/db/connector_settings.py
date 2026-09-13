"""Propriétés de connecteur SURCHARGEABLES en base — SQL seul (L6 pièce 2 c2).

La table `connector_settings` est posée par `db/schema/connectors.py` ; ce module est
son unique lecteur/écrivain. Il ne porte AUCUNE politique : qui a le droit de poser une
surcharge est une affaire d'autorisation (`capabilities/`), et ce qu'une surcharge
SIGNIFIE est une affaire de `connectors.cardinality`. Ici, des requêtes.

⚠️ **Cette table ne se lit pas sur le chemin CHAUD d'un appel d'outil** — c'est la
propriété à tenir, et elle a un motif chiffré : la cardinalité est consultée jusqu'à
quatre fois par appel, sur un serveur mono-loop, contre une base managée distante. Une
lecture par consultation serait le mode de panne que `docs/event-loop-perf.md`
documente ; d'où son instantané en mémoire (`connectors.cardinality`), chargé au boot
et sur rechargement explicite.

Un lecteur FROID et HORS BOUCLE satisfait la même propriété sans instantané :
`tools/planity_session.py` lit les coordonnées de l'application Planity à la
construction d'un client — au plus une fois par credential et par TTL de pool
(30 min), via `asyncio.to_thread` — et jamais par appel. Il y gagne ce que
l'instantané ne donne pas : une écriture que toutes les couleurs déployées voient au
client suivant, sans que chacune ait à recharger.

La question à poser en ajoutant un lecteur n'est donc pas « boot ou appel ? » mais
**« à quelle fréquence, et dans la boucle ? »** — un lecteur chaud prend l'instantané,
un lecteur froid hors boucle peut lire la table.
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect

# Le scope « toute la plateforme ». Convention maison — `platform` n'a pas d'id, comme
# `guides.owner_id` et `grants.grantor_id`.
PLATFORM_SCOPE_ID = "platform"


def list_connector_settings(key: Optional[str] = None, conn=None) -> list[dict]:
    """Toutes les surcharges (ou celles d'une `key`). La table se lit ENTIÈREMENT :
    elle porte une poignée de lignes, et son seul lecteur en veut l'intégralité pour
    en faire un dictionnaire en mémoire."""
    sql = ("SELECT scope_type, scope_id, connector, key, value, set_by, set_at "
           "FROM connector_settings")
    params: tuple = ()
    if key is not None:
        sql += " WHERE key = %s"
        params = (key,)
    sql += " ORDER BY scope_type, scope_id, connector, key"
    if conn is not None:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    with _connect() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def set_connector_setting(scope_type: str, scope_id: str, connector: str, key: str,
                          value: str, set_by: Optional[str] = None) -> None:
    """Pose ou remplace une surcharge. Ne valide NI le scope NI la valeur : le CHECK
    de la table ferme le vocabulaire de scope, et le sens de `value` appartient au
    module qui la lit — la valider ici en ferait un second domicile."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO connector_settings "
            "(scope_type, scope_id, connector, key, value, set_by) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (scope_type, scope_id, connector, key) DO UPDATE SET "
            "value = EXCLUDED.value, set_by = EXCLUDED.set_by, set_at = NOW()",
            (scope_type, str(scope_id), connector, key, value, set_by))


def clear_connector_setting(scope_type: str, scope_id: str, connector: str,
                            key: str) -> bool:
    """Retire une surcharge — la propriété retombe sur le défaut du registre.

    Un vrai DELETE, et c'est la seule table de ce lot où c'en est un : une surcharge
    n'est pas un objet qu'on désigne (aucun binding, aucune arête ne la nomme), c'est
    un réglage. L'archiver ne servirait qu'à garder un réglage mort dans les lectures."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM connector_settings WHERE scope_type = %s AND scope_id = %s "
            "AND connector = %s AND key = %s",
            (scope_type, str(scope_id), connector, key))
    return (cur.rowcount or 0) > 0


def get_connector_setting(scope_type: str, scope_id: str, connector: str,
                          key: str) -> Optional[str]:
    """La valeur d'UNE surcharge, ou None si elle n'est pas posée.

    Lecteur FROID (cf. l'en-tête) : son premier appelant est la garde de clé des
    travaux hébergés (`capabilities/_cle_exigee.py`), qui ne le consulte qu'au moment
    où un worker a effectivement RÉSERVÉ un travail — jamais sur un sondage à vide,
    jamais par appel d'outil. Quelques lectures par minute, hors de toute boucle
    d'appel."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM connector_settings WHERE scope_type = %s AND "
            "scope_id = %s AND connector = %s AND key = %s",
            (scope_type, str(scope_id), connector, key)).fetchone()
    return row["value"] if row else None
