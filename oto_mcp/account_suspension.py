"""Compte mis en pause : le prédicat, et la phrase qui le refuse.

**Le geste qui manquait.** Un compte n'avait que deux états : vivant, ou supprimé —
et « supprimé » n'existe même pas comme geste de produit : le seul `DELETE FROM users`
du dépôt est l'étape 4 de `db.migrate_sub`. Or supprimer ne neutralise pas
proprement : la plupart des tables keyed-by-sub n'ont **aucune** FK vers `users`
(appartenances, projets, documents, journal…), donc la suppression laisse des
pointeurs morts plutôt que de nettoyer, et ce qui a une FK part en cascade. Entre les
deux, il n'y avait rien.

`users.suspended_at` est ce cran manquant : **le compte ne peut plus rien faire, et
rien de ce qui pend de lui n'est touché.** Ses appartenances restent, ses projets et
ses documents restent à lui, le journal continue de dire qu'il a fait ce qu'il a fait.
Un document qu'il a écrit dans une org continue de le nommer comme auteur.

**Ce que ce module porte, et pourquoi il existe séparément.** Le prédicat vit dans
`db.get_suspension` ; la DÉCISION de refuser et le TEXTE du refus vivent ici, une
seule fois, pour que les deux faces disent exactement la même chose. Une pause qui se
raconterait autrement au dashboard qu'à l'agent serait un mécanisme dont personne ne
peut vérifier l'effet.

**Le refus tombe à l'entrée de CHAQUE requête, pas au login.** C'est le seul point qui
compte : un jeton émis avant la pause reste signé et valide jusqu'à son expiration —
une heure pour un JWT, potentiellement sans limite pour un jeton `oto_`. Une pause
vérifiée à la connexion ne protégerait de rien pendant tout ce temps ; ce serait un
bouton qui rassure sans agir. Le coût est une lecture sur clé primaire par requête, à
côté d'un `upsert_user` que la face REST fait déjà à chaque appel.

**Ce qu'il partage s'arrête avec lui** (oto-backend#898, arbitrage du 23/09/2026,
option A). Un compte personnel dont le titulaire est en pause ne donne plus aucun niveau
aux autres (`access/niveaux._proprietaire_present`, #1499), et le retrouve au réveil
sans rien reconfigurer : rien n'est détaché, ses règles restent, c'est la résolution
qui lit l'état du titulaire à chaque appel. Celui qui le nomme reçoit le refus de niveau
(`insufficient_level`), qui dit qui gère le compte.

Ce que ce module ne fait PAS : décider QUI peut mettre en pause (c'est l'autorisation
de la capacité `admin.account`), ni empêcher un compte en pause de disparaître d'un
merge (c'est la garde de `db.migrate_sub`).
"""
from __future__ import annotations

import logging
from typing import Optional

from . import db

logger = logging.getLogger(__name__)

# Le code servi aux DEUX faces, sans traduction : un signal remonté par un agent se
# retrouve tel quel dans le journal, et un intégrateur n'a qu'une chaîne à connaître.
CODE = "account_suspended"


def etat(sub: str) -> Optional[dict]:
    """L'état de pause d'un compte, ou `None` s'il est vivant — le cas de tout le monde.

    ⚠️ Ne rattrape rien : un hoquet de base REMONTE. Rendre `None` sur une panne
    servirait la requête d'un compte neutralisé comme s'il était vivant, c'est-à-dire
    qu'un incident de base ferait sauter la garde, en silence et sans une ligne. Le
    fail-safe d'une neutralisation est le refus, pas le laisser-passer."""
    if not sub:
        return None
    return db.get_suspension(sub)


def message(pause: dict) -> str:
    """La phrase servie au porteur du jeton — la même des deux côtés.

    Elle dit trois choses, et c'est le minimum pour que le refus soit actionnable :
    que le compte est en pause (pas cassé, pas inconnu), le motif écrit par celui qui
    l'a posée, et que le retour passe par un humain. Elle ne nomme pas l'opérateur :
    le motif suffit à retrouver la décision, et l'identité de qui l'a prise appartient
    à l'exploitant, pas au porteur du jeton."""
    motif = (pause.get("suspended_reason") or "").strip()
    fin = f" Motif : {motif}" if motif else ""
    return ("Ce compte est en pause : il ne peut plus agir, et rien de ce qui lui "
            "appartient n'a été supprimé." + fin +
            " Le réveil est un acte d'administration — demandez-le à l'administrateur "
            "de votre espace.")


def refus(sub: str) -> Optional[tuple[str, dict]]:
    """`(message, état)` si le compte est en pause, `None` sinon.

    Appelé par la seule garde d'identité (`garde_identite.refus`), que traversent les
    portes d'entrée REST, MCP et le lien d'upload signé. Journalise chaque refus en `warning` : un
    compte en pause qui continue de frapper à la porte est un fait d'exploitation qu'on
    veut voir — c'est ce qui dira si la pause a été comprise, ou si une automatisation
    tourne encore sous cette identité."""
    pause = etat(sub)
    if not pause:
        return None
    logger.warning("compte en pause refusé à l'entrée : sub=%s depuis=%s",
                   sub, pause.get("suspended_at"))
    return message(pause), pause
