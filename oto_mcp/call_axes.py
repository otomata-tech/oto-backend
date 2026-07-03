"""Axes-contexte d'appel sur la surface des tools PLATS (modèle sans état de session,
#108/#112).

claude.ai renouvelle le `Mcp-Session-Id` à CHAQUE appel → tout état de session serveur
(compte de connecteur actif, projet actif, run en cours) est perdu d'un appel au
suivant. La parade : porter le contexte en **identifiants d'appel** explicites plutôt
qu'en bracelet serveur. Pour les tools de capacité, `org=` est injecté par
`_mcp_adapter` ; pour les tools **plats** (connecteurs, `data_*`), les axes vivent ici.

Mécanisme (zéro modification des fonctions de tools) :
  1. `on_list_tools` (middleware) advertise l'axe dans le schéma des tools CONCERNÉS
     (sélectif, dérivé du registre) → claude.ai sait l'envoyer (les schémas sont en
     `additionalProperties:false`, un axe non déclaré serait refusé côté client) ;
  2. `on_call_tool` (middleware) lit l'axe des args BRUTS, pose la ContextVar, et
     **retire l'axe des arguments** avant le dispatch → la fonction du tool, qui ne le
     déclare pas, valide clean ;
  3. les **seams de résolution existants** lisent la ContextVar (`resolve_credential`
     lit `current_call_account`, `current_project` lit `current_call_project`…) → le
     comportement du tool s'adapte sans qu'il connaisse l'axe.

Exposition SÉLECTIVE (pas sur toute la surface — coût tokens de `tools/list`) : chaque
axe porte un prédicat `applies` dérivé du registre.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from . import providers, session_org
from .tool_visibility import namespace_of

# Entrée d'annulation d'un axe posé : (fonction de reset, token ContextVar).
UndoEntry = tuple[Callable[[object], None], object]


@dataclass(frozen=True)
class CallAxis:
    """Un axe-contexte injectable sur les tools plats. `schema` = fragment JSON-Schema
    de la propriété (optionnelle) ajoutée. `applies(name)` décide, tool par tool, si
    l'axe est advertisé/lu. `pin(value)` garde/pose la ContextVar et renvoie l'entrée
    d'annulation (ou None si l'axe est inerte pour cette valeur)."""
    param: str
    schema: dict
    applies: Callable[[str], bool]
    pin: Callable[[object], Awaitable[Optional[UndoEntry]]]


def _is_multi_account_tool(name: str) -> bool:
    """Le tool appartient-il à un connecteur MULTI-COMPTE (coffre à N comptes,
    `Connector.auth_multi_account`) ? Dérivé du registre via le namespace → seuls
    zoho/google exposent `account=` aujourd'hui (« 2 Zoho », gmail/tasks/calendar).
    Les caps spine (`oto_*`) et les connecteurs mono-compte renvoient None → exclus."""
    con = providers.connector_for_namespace(namespace_of(name))
    return con is not None and con.auth_multi_account


async def _pin_account(value: object) -> Optional[UndoEntry]:
    """Épingle le compte de connecteur de l'appel courant. Pas de garde DB ici : le
    compte n'est qu'un LABEL, la garde vit à la résolution (`resolve_credential` lève
    une McpError actionnable si ce compte n'existe pas au palier membre — jamais de
    repli muet vers un autre compte). None/'' ⇒ inerte (mono-compte legacy)."""
    if value is None or value == "":
        return None
    return (session_org.reset_call_account, session_org.set_call_account(str(value)))


ACCOUNT = CallAxis(
    param="account",
    schema={
        "type": "string",
        "title": "Account",
        "description": (
            "Compte du connecteur à utiliser quand plusieurs sont configurés dans "
            "l'org (ex. « 2 Zoho »). Le label listé par oto_connector_identities. "
            "Omets si un seul compte est configuré."
        ),
    },
    applies=_is_multi_account_tool,
    pin=_pin_account,
)


# Axes exposés sur les tools plats. `project=`/`run_id=` s'ajouteront ici (mêmes 3
# mécanismes) au fil des barreaux suivants.
AXES: tuple[CallAxis, ...] = (ACCOUNT,)


def axes_for(name: str) -> list[CallAxis]:
    """Axes advertisés/lus pour ce tool (sélectif). Vide pour la plupart des tools."""
    return [a for a in AXES if a.applies(name)]


def inject_schema(parameters: Optional[dict], axes: list[CallAxis]) -> dict:
    """Copie le schéma d'entrée du tool en y ajoutant les propriétés d'axe (optionnelles,
    jamais `required`). `additionalProperties` inchangé (l'axe est désormais déclaré)."""
    params = copy.deepcopy(parameters) if isinstance(parameters, dict) else {
        "type": "object", "properties": {}}
    props = params.setdefault("properties", {})
    for axis in axes:
        props.setdefault(axis.param, dict(axis.schema))
    return params
