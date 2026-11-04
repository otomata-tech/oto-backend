"""« Pour ce connecteur, pas de clé perso dans cette org » — la politique et son refus.

Décision d'Alexis du 13/10/2026 (oto-backend#1239). Un admin d'org déclare, connecteur
par connecteur, que les clés PERSO (palier membre du coffre) ne servent pas dans son
org. Seules restent résolues les clés partagées : celle d'une équipe du membre, celle
de l'org, celle du tenant, et la clé incluse (clé plateforme accordée).

**Ce n'est PAS la restriction par membre retirée le 24/09** (ADR 0053 D1). Celle-ci tenait une LISTE de qui avait le droit, posée par-dessus la
résolution. Ici, aucune liste, aucun membre nommé : la politique retire un PALIER de la
cascade, pour tout le monde dans l'org. Réserver un connecteur à une équipe reste
« poser la clé au bon niveau » (une clé d'équipe ne sert que ses membres) ; la politique
ferme seulement la porte que la clé perso laissait ouverte à côté.

**Où elle se lit, et une seule fois par endroit :**

- à l'APPEL, dans la traversée — le walker (`access.cascade.walk_cascade`) et la chaîne
  (`access.chain_resolution._paliers`) sautent le barreau membre, la même règle des deux
  côtés de la fenêtre L7 ; la résolution (`access.resolve`) en fait un refus NOMMÉ quand
  plus rien ne sert, et refuse l'instance perso désignée par `_instance=` ou le projet ;
- à la CONNEXION (`connectors.activation_gate.exiger_connectable`) et à la POSE d'une clé
  perso (`capabilities.me_credentials`) : on ne branche pas ce qui ne servirait pas.
  Le compte perso d'un autre, nommé par l'appel, tombe sous le même refus (instance
  désignée).

**Stockage** : une ligne de `connector_settings` (scope `org`, clé `personal_key`, valeur
`forbidden`), la table des propriétés de connecteur surchargeables par org — aucune
migration. Elle se lit À LA DEMANDE (une lecture par org, mémorisée pour l'appel), et
seulement quand une clé perso existe : l'instantané en mémoire de la cardinalité est par process, et une
politique de sécurité posée sur une couleur doit valoir sur toutes au prochain appel.

Le connecteur est normalisé sur le PORTEUR du credential (délégation : les canaux
unipile pointent sur `unipile`), à l'écriture comme à la lecture, comme la colonne
`connector_settings.connector` le dit.
"""
from __future__ import annotations

from typing import Optional

from mcp.types import INVALID_PARAMS, ErrorData

from .. import providers
from ..db import connector_settings
from ..mcp_errors import McpError

#: La clé de la politique dans `connector_settings`, et sa seule valeur.
CLE = "personal_key"
INTERDITE = "forbidden"
#: Le code du refus, le même aux trois moments (appel, connexion, pose).
CODE = "personal_key_forbidden"

#: Les trois moments du refus — ce que le texte dit de ce qui vient d'être refusé.
A_L_APPEL = "and no team, organization or included key is reachable for you"
A_L_INSTANCE = "so the personal instance named by this call is not served"
A_LA_CONNEXION = "so no personal key can be connected for it"


class ClePersoInterdite(McpError):
    """Refus de résolution : la politique de l'org a sauté la clé perso, et rien de
    partagé ne sert. Distinct de `CredentialUnavailable` exprès : les appelants qui
    reformulent « aucune clé » à leur façon ne doivent pas effacer celui-ci."""


def porteur(connector: str) -> str:
    return providers.credential_provider(connector)


def interdite(org_id: Optional[int], connector: str) -> bool:
    """La clé perso est-elle interdite pour `connector` dans l'org `org_id` ?
    Sans org de contexte, il n'y a pas de clé perso (scope membre, ADR 0033) : non.
    Une valeur inconnue en base LÈVE — l'interpréter serait décider à la place de qui
    l'a posée."""
    if org_id is None:
        return False
    valeur = connector_settings.org_settings_for_key(str(org_id), CLE).get(porteur(connector))
    if valeur is None:
        return False
    if valeur != INTERDITE:
        raise ValueError(f"connector_settings org:{org_id} {porteur(connector)} {CLE}="
                         f"{valeur!r} : valeur inconnue (attendu {INTERDITE!r})")
    return True


def poser(org_id: int, connector: str, forbidden: bool, set_by: Optional[str]) -> None:
    """Pose (`forbidden=True`) ou lève la politique. Lever SUPPRIME la ligne : sans
    elle, la clé perso sert comme partout ailleurs."""
    if forbidden:
        connector_settings.set_connector_setting(
            "org", str(org_id), porteur(connector), CLE, INTERDITE, set_by)
    else:
        connector_settings.clear_connector_setting("org", str(org_id), porteur(connector), CLE)


def politiques(org_id: int) -> list[dict]:
    """Les connecteurs dont l'org interdit la clé perso, avec qui l'a posé et quand."""
    return [{"connector": r["connector"], "set_by": r.get("set_by"),
             "set_at": r.get("set_at")}
            for r in connector_settings.list_connector_settings(CLE)
            if r["scope_type"] == "org" and str(r["scope_id"]) == str(org_id)]


def texte_refus(org_id: int, connector: str, moment: str) -> str:
    """Le refus NOMME la politique et dit à qui s'adresser — jamais « aucune clé »,
    qui enverrait poser une clé perso que la politique refuserait aussitôt."""
    from .. import org_store
    org = org_store.get_org(org_id) or {}
    nom = f"« {org['name']} » (#{org_id})" if org.get("name") else f"#{org_id}"
    return (
        f"Refusal `{CODE}`: organization {nom} forbids personal keys for "
        f"`{porteur(connector)}`, {moment}. Only a shared key serves here: one of your "
        f"teams' key, the organization's key, or the included key (a platform or host "
        f"key granted to your team or organization). Ask an admin of this organization: "
        f"they set a team or organization key, have the included key granted to your "
        f"team, or lift the policy (PUT /api/orgs/{org_id}/connectors/"
        f"{porteur(connector)}/personal-key-policy).")


def refus(org_id: int, connector: str, moment: str) -> ErrorData:
    return ErrorData(code=INVALID_PARAMS, message=texte_refus(org_id, connector, moment),
                     data={"code": CODE, "retryable": False,
                           "connector": porteur(connector), "org_id": org_id})


def refuser_si_interdite(org_id: Optional[int], connector: str, moment: str) -> None:
    """Lève `ClePersoInterdite` si la politique tient. Sert à la résolution, quand plus
    rien ne sert ou qu'une instance perso est désignée."""
    if interdite(org_id, connector):
        raise ClePersoInterdite(refus(org_id, connector, moment))
