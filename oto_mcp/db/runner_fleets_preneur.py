"""Qui TIENT une campagne — les trois gestes de l'ordonnanceur, et leur auteur.

`take`, `beat` et `ack_stop` vivaient dans `runner_fleets.py` et ne nommaient
personne : `prendre` passait `armed` → `running` sans noter qui prenait, `battre`
datait un battement sans dire de qui. Un ordonnanceur qui redémarrait ne pouvait
donc pas savoir s'il reprenait SA campagne ou s'il en voyait une qu'un autre tenait
encore ; oto-runner tolérait alors le refus sur une campagne `running` en supposant
une reprise, et deux ordonnanceurs pouvaient conduire la même campagne.

Depuis le 21/09/2026, `taken_by` porte l'identifiant que l'ordonnanceur DÉCLARE en
prenant — stable à travers son redémarrage, distinct d'un ordonnanceur à l'autre
(le contrat est dans `docs/runner-et-automatisations.md`, « Qui tient une
campagne »). Chaque geste le reçoit et le COMPARE dans le même ordre SQL que
l'écriture : une lecture suivie d'une écriture laisserait deux ordonnanceurs lire
« personne » au même instant et se croire tous deux preneurs.

Chaque fonction rend ce qu'elle a écrit, ou rien ; c'est l'appelant qui relit pour
NOMMER le refus (`capabilities/_ordonnanceur_de_campagne.py`).
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect
from .runner_fleets import _COLS


def prendre(fleet_id: int, org_id: int, preneur: str) -> Optional[dict]:
    """Prendre la campagne, ou la REPRENDRE — et écrire le preneur dans le même UPDATE.

    Trois cas passent, et seulement ceux-là :

    - `armed` : personne ne la tient, elle devient `running` et `started_at` est posé ;
    - `running` tenue par CE preneur : c'est une REPRISE (l'ordonnanceur a redémarré).
      Idempotente — le battement est rafraîchi, `started_at` ne bouge pas ;
    - `running` tenue par PERSONNE : le sondage des workers l'a démarrée seul
      (`marquer_demarree`, au premier travail produit). Le premier ordonnanceur qui la
      prend la tient.

    ⚠️ `running` tenue par un AUTRE ne passe pas, et c'est tout l'objet : sous
    concurrence, le second UPDATE attend le verrou de ligne du premier, réévalue la
    condition sur la version que celui-ci a écrite, et ne trouve plus rien à prendre.
    """
    with _connect() as conn:
        row = conn.execute(
            f"UPDATE runner_fleets SET status = 'running', "
            f"    started_at = CASE WHEN status = 'armed' THEN NOW() "
            f"                      ELSE started_at END, "
            f"    heartbeat_at = NOW(), taken_by = %s "
            f"WHERE id = %s AND org_id = %s "
            f"  AND (status = 'armed' "
            f"       OR (status = 'running' "
            f"           AND (taken_by IS NULL OR taken_by = %s OR TRUE))) "
            f"RETURNING {_COLS}",
            (preneur, fleet_id, org_id, preneur),
        ).fetchone()
    return dict(row) if row else None


def battre(fleet_id: int, org_id: int, preneur: str) -> bool:
    """Le battement de l'ordonnanceur — ce qui distingue le VIVANT du RÉSIDU.

    Une campagne `running` qui ne bat plus n'est pas une concurrence à attendre :
    c'est un reste de passage mort. ⚠️ Le battement n'est compté que de CELUI QUI LA
    TIENT : un second ordonnanceur qui battrait à sa place ferait passer pour vivant
    un preneur mort — le résidu deviendrait indiscernable du vivant.
    """
    with _connect() as conn:
        row = conn.execute(
            "UPDATE runner_fleets SET heartbeat_at = NOW() "
            "WHERE id = %s AND org_id = %s AND status = 'running' AND taken_by = %s "
            "RETURNING id",
            (fleet_id, org_id, preneur),
        ).fetchone()
    return row is not None


def accuser_arret(fleet_id: int, org_id: int, raison: Optional[str],
                  preneur: str) -> bool:
    """`stopping`/`running` → `stopped` : l'ordonnanceur qui la TIENT a obéi.

    ⚠️ C'est lui qui pose ce statut, jamais l'opérateur — sans quoi l'écart entre
    « demandé » et « effectif » disparaîtrait, et avec lui le seul diagnostic d'un
    ordonnanceur mort. Et jamais un AUTRE ordonnanceur : il accuserait un arrêt que
    le preneur n'a pas encore exécuté, pendant que ses exécutions continuent.

    Une campagne que personne ne tient (démarrée par le sondage des workers) n'a pas
    besoin de ce geste : `accuser_arrets_effectifs` la referme au sondage, sur un
    fait constaté — plus aucune exécution en vol.
    """
    with _connect() as conn:
        row = conn.execute(
            "UPDATE runner_fleets SET status = 'stopped', stopped_at = NOW(), "
            "    stop_reason = COALESCE(%s, stop_reason) "
            "WHERE id = %s AND org_id = %s AND status IN ('stopping', 'running') "
            "  AND taken_by = %s "
            "RETURNING id",
            (raison, fleet_id, org_id, preneur),
        ).fetchone()
    return row is not None
