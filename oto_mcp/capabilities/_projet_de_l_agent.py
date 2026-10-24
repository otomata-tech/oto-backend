"""Le projet où travaille un agent : un de ceux de sa procédure (tranché le 24/10/2026).

Une procédure peut être rangée dans plusieurs projets de l'org ; un agent, lui, travaille
dans UN seul — chaque run se fait dans un projet, qui décide des tableaux que ses slots
désignent et des comptes qu'il utilise. Pour qu'une procédure travaille pour deux
projets, on lui donne un agent dans chacun (`runner_triggers._projet_de_la_pose`).

Un agent qui ne porte pas de projet à lui (posé par MCP sans `project_id`, ou d'avant la
règle) travaille dans le premier projet de sa procédure (`db.maison_de_la_procedure`) :
c'est ce que son run reçoit (`de_l_agent`) et ce que sa lecture sert (`servis`), pour
que l'écran et le run disent la même chose.

⚠️ DÉDUIT, pas recopié sur l'agent : la plupart des agents tournent sur le forfait de
leur propriétaire, que lui seul (et qui il nomme) peut modifier — une copie écrite au
rangement de la procédure sauterait précisément ceux-là. Le projet stocké sur l'agent,
quand il en a un, reste lu d'abord : rien ne change pour un agent déjà rangé.
"""
from __future__ import annotations

import logging
from typing import Optional

from .. import db

log = logging.getLogger(__name__)


def de_l_agent(t: dict) -> Optional[int]:
    """Le projet de la charge d'un run de `t` : le sien, sinon le premier de sa procédure.

    Une lecture qui échoue laisse partir le run sans projet, comme avant la règle : un
    run bloqué parce qu'on n'a pas su lire où ranger son résultat serait pire que le
    défaut qu'on corrige."""
    if t.get("project_id") is not None:
        return int(t["project_id"])
    if not t.get("org_id") or not t.get("procedure"):
        return None
    try:
        return db.maison_de_la_procedure(int(t["org_id"]), t["procedure"])
    except Exception as e:  # noqa: BLE001 — voir ci-dessus
        log.warning("déclencheur %s : projet de la procédure illisible (%s)", t.get("id"), e)
        return None


def servis(org_id: int, triggers: list[dict]) -> list[dict]:
    """Les agents tels que servis : un agent sans projet à lui porte le premier de sa
    procédure — la même réponse que reçoit son run. Une requête pour toute la liste."""
    sans = [t["procedure"] for t in triggers
            if t.get("project_id") is None and t.get("procedure")]
    if not sans:
        return triggers
    maisons = db.maisons_des_procedures(int(org_id), sans)
    return [{**t, "project_id": maisons[t["procedure"]]}
            if t.get("project_id") is None and t.get("procedure") in maisons else t
            for t in triggers]
