"""Ce qu'un agent hébergé ne pose pas lui-même : ses outils d'admin exercés avec le rôle
plateforme de son porteur (`platform_tools`, revue de #1274) — qui peut poser la liste, et
ce qu'elle peut nommer —, et tout élargissement d'une liste d'outils (`tools`), à la
modification comme à la création d'un agent.

Sorti de `runner_triggers` : la capacité des agents dépassait 1 900 lignes, et cette
règle est un tout — les refus d'auteur et de contenu, avec leur borne.

**Une personne, pas un agent.** Sous un jeton de délégation (`verrou_org.travail_de_la_requete`,
posé au bord MCP et au bord REST pour un travail hébergé), un agent qui a `oto_trigger` dans
sa liste élargirait la sienne — une injection de prompt suffirait —, ou créerait un agent
plus outillé que lui, qui tournerait ensuite avec ces outils. Élargir reste un geste de
personne ; réduire, reposer la même liste, ou créer un agent qui n'a que des outils du
travail appelant reste permis."""
from __future__ import annotations

import logging
from typing import Optional

from .. import access, db, deprecations, outils_du_travail, verrou_org
from ._types import AuthzDenied, ResolvedCtx

logger = logging.getLogger(__name__)

#: La borne de `platform_tools` : une liste d'outils d'admin NOMMÉS, jamais tout l'admin.
PLATFORM_TOOLS_MAX = 10


def _depuis_un_agent_heberge() -> bool:
    """L'appel vient d'un travail hébergé : la requête porte un jeton de délégation."""
    return verrou_org.travail_de_la_requete() is not None


def _refuser_l_elargissement(outils: list[str], permis: set | frozenset) -> None:
    ajoutes = [n for n in outils if deprecations.tool_canonique(n) not in permis]
    if ajoutes:
        raise AuthzDenied(403, "tools_human_only",
                          f"un agent hébergé ne donne pas d'outil à un agent ({ajoutes}) : "
                          "c'est une personne qui l'élargit, depuis son compte.")


def exiger_aucun_ajout_d_agent(actuel: dict, outils: list[str]) -> None:
    """`op=update` : refuse `tools_human_only` quand un agent hébergé AJOUTE un outil à
    la liste d'un agent (`outils` hors de celle stockée, au canonique). Réduire la liste,
    ou la reposer telle quelle, passe : cela ne donne rien de plus. La création a sa
    règle (`exiger_aucun_outil_hors_du_travail`). Hors jeton de délégation, rien ne
    change — la personne qui modifie l'agent l'élargit comme avant."""
    if not _depuis_un_agent_heberge():
        return
    _refuser_l_elargissement(outils, {deprecations.tool_canonique(n)
                                      for n in (actuel.get("tools") or [])
                                      if isinstance(n, str)})


def exiger_aucun_outil_hors_du_travail(outils: list[str]) -> None:
    """`op=create` : refuse `tools_human_only` quand un agent hébergé crée un agent qui
    aurait un outil hors de ce que SON travail peut appeler (`outils_du_travail.
    permis_du_travail`, au canonique). `outils` est la liste qui sera écrite — fournie,
    ou déduite de la procédure quand `tools` est omis : un agent ne s'outille pas plus
    que lui par la procédure qu'il nomme. Un sous-ensemble passe. Hors jeton de
    délégation, rien ne change."""
    verrou = verrou_org.travail_de_la_requete()
    if verrou is None:
        return
    _refuser_l_elargissement(outils, outils_du_travail.permis_du_travail(verrou.job_id))


def outils_plateforme_valides(ctx: ResolvedCtx, noms: list[str], actuel: dict,
                              outils: Optional[list[str]]) -> list[str]:
    """La liste BORNÉE des `oto_admin_*` qu'un agent exerce avec le rôle plateforme de
    son porteur sous le verrou des rôles (`verrou_org.outils_permis`, revue de #1274).

    ⚠️ Elle ne se pose que par une PERSONNE : sous un jeton de délégation, un agent qui a
    `oto_trigger` dans sa liste élargirait la sienne — une injection de prompt suffirait.
    Seul le PROPRIÉTAIRE la pose (c'est son rôle qu'elle prête), et une liste non vide
    exige qu'il soit opérateur plateforme : un membre n'a rien à prêter, et une liste
    posée d'avance s'armerait le jour où il le devient. Chaque nom est un `oto_admin_*`
    déjà dans les outils de l'agent (`outils`, sinon ceux stockés) : la liste ne sort pas
    de la liste d'outils, que le serveur tient de toute façon (`outils_du_travail`)."""
    if _depuis_un_agent_heberge():
        raise AuthzDenied(403, "platform_tools_human_only",
                          "`platform_tools` ne se pose pas depuis un agent hébergé : "
                          "c'est son propriétaire qui la pose, depuis son compte.")
    if str(actuel.get("sub")) != str(ctx.sub):
        raise AuthzDenied(403, "platform_tools_owner_only",
                          "`platform_tools` prête le rôle plateforme du propriétaire : "
                          "lui seul la pose.")
    liste = list(dict.fromkeys(n.strip() for n in noms if isinstance(n, str) and n.strip()))
    if not liste:
        return []
    if not access.is_platform_operator(ctx.sub):
        raise AuthzDenied(403, "platform_tools_operator_only",
                          "`platform_tools` ne vaut que pour un opérateur plateforme : "
                          "un membre n'a aucun rôle plateforme à prêter.")
    if len(liste) > PLATFORM_TOOLS_MAX:
        raise AuthzDenied(400, "platform_tools_too_many",
                          f"`platform_tools` : au plus {PLATFORM_TOOLS_MAX} outils — une "
                          "liste BORNÉE, pas tout l'admin.")
    # Un agent que d'autres modifient (tout partage en écriture — la création en pose
    # un à l'org depuis le 23/10/2026) n'exerce jamais ses `oto_admin_*`
    # (`db.outils_plateforme_du_travail`) : les armer serait un réglage inerte.
    if db.partage_en_ecriture(int(actuel["id"])):
        raise AuthzDenied(409, "platform_tools_agent_shared",
                          "`platform_tools` ne vaut que pour un agent que personne d'autre "
                          "ne modifie : rends-le privé d'abord (`unshare everyone=true`, et "
                          "retire ses éditeurs).")
    hors = [n for n in liste if not n.startswith(verrou_org.PREFIXE_ADMIN)]
    if hors:
        raise AuthzDenied(400, "platform_tools_not_admin",
                          f"`platform_tools` ne nomme que des `oto_admin_*` : {hors}.")
    declares = set(outils if outils is not None else (actuel.get("tools") or []))
    absents = [n for n in liste if n not in declares]
    if absents:
        raise AuthzDenied(400, "platform_tools_not_in_tools",
                          f"`platform_tools` : {absents} absents des `tools` de l'agent — "
                          "ajoute-les d'abord à sa liste d'outils.")
    logger.warning("agent %s (org %s) : %s pose ses outils d'admin au rôle plateforme %s",
                   actuel.get("id"), ctx.org_id, ctx.sub, liste)
    return liste
