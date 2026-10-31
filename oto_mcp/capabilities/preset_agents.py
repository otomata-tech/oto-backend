"""Le réglage PERSONNEL : comment naissent MES agents, dans une org.

`private` (défaut) ou `shared`, posé par la personne pour elle-même
(`oto_trigger op=default_sharing`) et lu à `op=create`, quelle que soit la face qui crée
(application, client MCP, agent hébergé).

`shared` fait, à la naissance, les deux gestes du propriétaire : partage en écriture à
toute l'org, puis prêt du forfait si l'agent tourne sur un abonnement personnel. Le
partage porte son `granted_by`, il prête donc comme un geste manuel et se retire pareil.
Rien de rétroactif. `private=true` à la création l'emporte.

⚠️ **Lu aussi à `op=give`** (31/10/2026, à trancher) : un agent qu'un collègue me DONNE
reçoit les deux mêmes gestes, à mon nom (`runner_triggers._apres_le_don`). Le
consentement s'étend ainsi des agents que je crée à ceux que je reçois ; le refus de
l'admin vaut pareil.

**L'exception, et elle seule, à « le prêt est un geste, jamais un réglage »**
(`_acces_agent`, `partage_par_defaut`) : le titulaire consent une fois pour tous ses
futurs agents de cette org. Ce consentement vaut parce que personne d'autre ne peut le
donner, d'où trois bornes :
- **une personne seule le pose** : sous un jeton de délégation, un agent qui lit des
  données tierces se l'accorderait sur une injection de prompt (le motif de
  `links_human_only`). Il se lit quand même : un agent qui crée doit savoir ce qui naîtra.
- **hors de la fiche du compte** : `oto_profile` et `PUT /api/me/profile` y écrivent, le
  handshake la sert. Il vit sur l'appartenance (`org_members`), et quitter l'org l'efface ;
- **l'admin de l'org peut le refuser** (`partage_par_defaut`, clé `preset_agent`) : plus de
  `shared` posé ni appliqué, sans toucher aux agents nés. Il lit qui l'a posé
  (`preset_agent_shared`, `org_console`). Aucun admin ne le pose pour un autre.
"""
from __future__ import annotations

from .. import db, verrou_org
from ._types import AuthzDenied, DeclaredError
from .orgs import partage_par_defaut

PRIVE, PARTAGE = "private", "shared"

REFUS = (
    DeclaredError(403, "agent_preset_human_only",
                  "`op=default_sharing` qui pose un préréglage depuis un agent hébergé "
                  "(jeton de délégation) ; le lire reste permis"),
    DeclaredError(403, "agent_preset_refused",
                  "`default_sharing=shared` quand l'admin de l'org refuse les préréglages "
                  "partagés (`partage_par_defaut`, `preset_agent=refuse`)"),
    DeclaredError(403, "agent_preset_not_a_member",
                  "`op=default_sharing` qui pose un préréglage dans une org dont on n'est "
                  "pas membre"),
)


def _refuse_par_l_org(org_id: int) -> bool:
    reglage = partage_par_defaut.lire(org_id)
    return reglage[partage_par_defaut.PRESET] == partage_par_defaut.REFUSE


def ouvre_a_la_naissance(sub: str, org_id: int) -> bool:
    """Un agent neuf de cette personne naît-il partagé ? Son préréglage, sauf refus de
    l'org."""
    return db.preset_agent_partage(sub, org_id) and not _refuse_par_l_org(org_id)


def servi(sub: str, org_id: int) -> dict:
    return {"default_sharing": PARTAGE if db.preset_agent_partage(sub, org_id) else PRIVE,
            "default_sharing_refused": _refuse_par_l_org(org_id)}


def poser(sub: str, org_id: int, valeur: str) -> None:
    """Pose le préréglage de `sub` dans cette org. `private` reste permis sous un refus
    de l'admin : on peut toujours se refermer."""
    if verrou_org.travail_de_la_requete() is not None:
        raise AuthzDenied(403, "agent_preset_human_only",
                          "op=default_sharing consents for the person: they set it from "
                          "their own account, never a hosted agent.")
    if valeur == PARTAGE and _refuse_par_l_org(org_id):
        raise AuthzDenied(403, "agent_preset_refused",
                          "An admin of this org refuses personal presets that share new "
                          "agents. Share an agent by name (op=share), or ask them.")
    if not db.poser_preset_agent_partage(sub, org_id, valeur == PARTAGE):
        raise AuthzDenied(403, "agent_preset_not_a_member",
                          f"You are not a member of org #{org_id}: no preset to set there.")
