"""Le partage par défaut d'une org : ce qu'un objet NEUF reçoit d'office, par type.

**Privé par défaut, toujours.** Le code ne partage jamais d'office un objet neuf : seul
ce réglage, posé par un admin de l'org, le fait. Une valeur par type d'objet,
`{"agent": "aucun" | "lecture", "app": "aucun" | "lecture" | "ecriture"}` ; lu par
`oto_trigger op=create` et `oto_apps op=create`. Sans réglage : `aucun` pour chaque type.

Une app accepte `ecriture` : elle n'a pas d'identité à elle — chaque geste d'une app
ouverte agit au nom de son LECTEUR, borné par les droits de son AUTEUR (le dernier à
l'avoir écrite, `apps_appel.py`), et le manifeste se rejuge contre qui la modifie.

⚠️ **`ecriture` est REFUSÉ pour un agent** (`default_sharing_write_unsupported`). Un agent
tourne sous l'identité de son propriétaire à chaque run d'horloge ou de webhook
(`runner_tick` et `runner_hook` enfilent avec `sub=t["sub"]`) : un membre qui le modifie
ferait exécuter SES consignes avec les connexions personnelles du propriétaire au
prochain run, sans personne qui « lance ». Seul `oto_trigger op=run` tourne sous celui
qui lance (`sub=ctx.sub`). Tant qu'un run planifié n'a pas d'autre identité que le
propriétaire, une écriture accordée d'office à toute l'org confierait son identité sans
qu'il l'ait voulu — le partage en écriture reste un geste nommé (`op=share`).

**Une valeur stockée illisible LÈVE** (`default_sharing_unreadable`) : un réglage qu'on ne
sait pas lire n'est ni `aucun` ni autre chose — le deviner ouvrirait ou fermerait en
silence. L'admin le repose.

Le partage né du réglage est MARQUÉ (`db.MARQUE_PARTAGE_PAR_DEFAUT` en `granted_by`) : il
se retire sans toucher aux partages faits à la main. Il ne prête jamais un forfait
personnel : prêter est un geste du TITULAIRE, agent par agent (nommer un éditeur, ou
`oto_trigger op=lend` pour une équipe ou l'org), et `db._forfait_prete` ne lit que les
partages posés par le propriétaire. Aucun réglage
d'admin ne partage en écriture ni ne prête un forfait à sa place.

MCP seul, par la console `oto_org_settings domain=partage_par_defaut` (`org_console.py`) :
`get` membre, `set` admin de l'org.
"""
from __future__ import annotations

from typing import Optional

from ... import db
from .._types import AuthzDenied

AUCUN, LECTURE, ECRITURE = "aucun", "lecture", "ecriture"
VALEURS = (AUCUN, LECTURE, ECRITURE)

#: Par type d'objet : les valeurs qu'il accepte. `ecriture` est une valeur CONNUE du
#: réglage, refusée pour un agent (tête de module) — d'où un refus nommé, pas
#: « valeur inconnue ».
TYPES: dict[str, tuple[str, ...]] = {"agent": (AUCUN, LECTURE),
                                     "app": (AUCUN, LECTURE, ECRITURE)}

#: Le rôle du partage posé à la naissance, par valeur.
ROLES = {LECTURE: "viewer", ECRITURE: "editor"}


def _illisible(org_id: int, stocke) -> AuthzDenied:
    return AuthzDenied(
        409, "default_sharing_unreadable",
        f"Le partage par défaut stocké pour l'org #{org_id} est illisible ({stocke!r}) : "
        "un admin de l'org le repose (`oto_org_settings domain=partage_par_defaut "
        "op=set`). Rien n'a été partagé.")


def lire(org_id: int) -> dict[str, str]:
    """Le réglage de l'org, une valeur par type connu (`aucun` quand rien n'est posé).
    Lève `default_sharing_unreadable` sur une valeur stockée qu'on ne sait pas lire."""
    stocke = db.partage_par_defaut_stocke(org_id)
    reglage = {t: AUCUN for t in TYPES}
    if stocke is None:
        return reglage
    if not isinstance(stocke, dict):
        raise _illisible(org_id, stocke)
    for t, v in stocke.items():
        if t not in TYPES or v not in TYPES[t]:
            raise _illisible(org_id, stocke)
        reglage[t] = v
    return reglage


def role_a_la_naissance(org_id: int, type_objet: str) -> Optional[str]:
    """Le rôle du partage à toute l'org qu'un objet NEUF de ce type reçoit, ou None."""
    return ROLES.get(lire(org_id)[type_objet])


def poser(org_id: int, partage: Optional[dict]) -> dict[str, str]:
    """Pose les types nommés dans `partage` (les autres gardent leur valeur) et rend le
    réglage appliqué. Rien n'est écrit si une seule valeur est refusée."""
    if not partage:
        raise AuthzDenied(400, "missing_default_sharing",
                          "`partage` requis pour set : {type: aucun|lecture}, "
                          f"types : {', '.join(TYPES)}.")
    for t, v in partage.items():
        if t not in TYPES:
            raise AuthzDenied(400, "unknown_type",
                              f"Type inconnu : {t} — connus : {', '.join(TYPES)}. "
                              "Rien n'a été modifié.")
        if t == "agent" and v == ECRITURE:
            raise AuthzDenied(
                400, "default_sharing_write_unsupported",
                "Un agent ne naît pas partagé en écriture : à chaque run planifié, il "
                "tourne sous l'identité de son propriétaire, et un membre qui le modifie "
                "agirait avec ses connexions. Partage en écriture un agent nommément "
                "(`oto_trigger op=share`). Rien n'a été modifié.")
        if v not in TYPES[t]:
            raise AuthzDenied(400, "invalid_default_sharing",
                              f"`{t}` vaut {' | '.join(TYPES[t])} (reçu {v!r}). "
                              "Rien n'a été modifié.")
    reglage = {**lire(org_id), **partage}
    a_stocker = {t: v for t, v in reglage.items() if v != AUCUN}
    if not db.poser_partage_par_defaut(org_id, a_stocker or None):
        raise AuthzDenied(404, "unknown_org", f"Org #{org_id} inconnue.")
    return reglage


def servi(org_id: int, reglage: dict[str, str]) -> dict:
    return {"org_id": org_id, "partage_par_defaut": reglage,
            "values": {t: list(v) for t, v in TYPES.items()}}
