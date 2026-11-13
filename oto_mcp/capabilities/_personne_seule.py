"""Les gestes réservés à une PERSONNE : un agent hébergé ne les fait pas.

**La garde commune.** Un appel vient d'un agent hébergé quand la requête porte un jeton de
délégation (`verrou_org.travail_de_la_requete`, posé au bord MCP et au bord REST pour un
travail hébergé, quel que soit le mode du verrou d'org). Un agent lit des données tierces :
un geste qui étend ses droits, ceux d'un autre agent ou ceux d'une personne se ferait sur
une injection de prompt. Chaque famille de gestes a son code, déclaré par les capacités
qui le lèvent ; hors jeton de délégation, rien ne change.

Les familles réservées ici :

- **gérer les agents** (`agents_human_only`, `oto_trigger`) : créer, supprimer, partager,
  prêter son abonnement, reprendre, et retoucher un agent hors des champs de
  `CHAMPS_D_AGENT_PERMIS` ;
- **les comptes** (`accounts_human_only`) : prêter une instance, ouvrir ou retirer un
  compte de messagerie à un tiers, installer, mettre en pause ou retirer un connecteur, en
  régler l'exposition ou le niveau d'accès ;
- **l'org** (`org_human_only`) : créer, modifier ou archiver une org, inviter, accepter ou
  refuser une invitation, régler l'org, gérer les équipes et leurs membres.

Les autres gestes de personne gardent leur code, mais passent par le même prédicat
(`depuis_un_agent_heberge`) : `code_human_only` (`_code_humain`), `tools_human_only` et
`platform_tools_human_only` (`_outils_plateforme`), `links_human_only`, `rerun_human_only`,
`give_human_only`, `hook_door_human_only` (`runner_triggers`), `agent_preset_human_only`
(`preset_agents`).

⚠️ Un code par famille, LITTÉRAL au point de levée : le relevé des refus déclarés
(`tests/_refus_atteignables.py`) ne lit qu'un code écrit en clair."""
from __future__ import annotations

from .. import verrou_org
from ._types import AuthzDenied, DeclaredError

#: Les champs d'`oto_trigger op=update` qu'un agent hébergé pose encore. `folder` range
#: un agent dans l'écran : il ne change ni ce que l'agent fait, ni quand, ni sous quelle
#: identité. `tools` n'y est pas, même pour réduire la liste : la liste d'outils d'un
#: agent est posée par une personne.
CHAMPS_D_AGENT_PERMIS = frozenset({"folder"})

REFUS_AGENTS = DeclaredError(
    403, "agents_human_only",
    "un geste de gestion d'agent depuis un agent hébergé (jeton de délégation) : "
    "`create`, `delete`, `share`, `unshare`, `lend`, `unlend`, `take_over`, ou "
    "`update` d'un autre champ que "
    f"{', '.join(f'`{c}`' for c in sorted(CHAMPS_D_AGENT_PERMIS))}")

REFUS_COMPTES = DeclaredError(
    403, "accounts_human_only",
    "un geste sur les comptes ou les connecteurs depuis un agent hébergé (jeton de "
    "délégation) : prêter une instance, ouvrir ou retirer un compte à un tiers, "
    "installer, mettre en pause, retirer ou régler un connecteur")

REFUS_ORG = DeclaredError(
    403, "org_human_only",
    "un geste sur l'org ou ses équipes depuis un agent hébergé (jeton de délégation) : "
    "créer, modifier, archiver, inviter, accepter ou refuser une invitation, régler, "
    "gérer les membres ou le guide d'une équipe")


def depuis_un_agent_heberge() -> bool:
    """L'appel vient d'un travail hébergé : la requête porte un jeton de délégation."""
    return verrou_org.travail_de_la_requete() is not None


def exiger_pour_les_agents(geste: str) -> None:
    """Lève `agents_human_only` sous un jeton de délégation."""
    if depuis_un_agent_heberge():
        raise AuthzDenied(403, "agents_human_only",
                          f"{geste}: a hosted agent does not manage agents. A person does "
                          "it, from their own account.")


def exiger_pour_les_comptes(geste: str) -> None:
    """Lève `accounts_human_only` sous un jeton de délégation."""
    if depuis_un_agent_heberge():
        raise AuthzDenied(403, "accounts_human_only",
                          f"{geste}: a hosted agent does not lend, open or install accounts "
                          "and connectors. A person does it, from their own account.")


def exiger_pour_l_org(geste: str) -> None:
    """Lève `org_human_only` sous un jeton de délégation."""
    if depuis_un_agent_heberge():
        raise AuthzDenied(403, "org_human_only",
                          f"{geste}: a hosted agent does not manage an org, its members or "
                          "its teams. A person does it, from their own account.")
