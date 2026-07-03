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
  2. `on_call_tool` (middleware) lit l'axe des args BRUTS, pose la/les ContextVar(s), et
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

from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS
from starlette.concurrency import run_in_threadpool

from . import providers, session_org
from .auth_hooks import current_user_sub_from_token
from .tool_visibility import namespace_of

# Entrée d'annulation d'un axe posé : (fonction de reset, token ContextVar).
UndoEntry = tuple[Callable[[object], None], object]


@dataclass(frozen=True)
class CallAxis:
    """Un axe-contexte injectable sur les tools plats. `schema` = fragment JSON-Schema
    de la propriété (optionnelle) ajoutée. `applies(name)` décide, tool par tool, si
    l'axe est advertisé/lu. `pin(value)` garde/pose la/les ContextVar(s) et renvoie la
    LISTE d'entrées d'annulation (vide si l'axe est inerte pour cette valeur ; plusieurs
    si l'axe co-pose — ex. project= pose projet + org dérivée)."""
    param: str
    schema: dict
    applies: Callable[[str], bool]
    pin: Callable[[object], Awaitable[list[UndoEntry]]]


# ── Helpers partagés (aussi utilisés par le middleware pour `org=`) ───────────

def require_axis_int(value: object, axis: str) -> int:
    """Convertit un axe-contexte d'appel en id entier ou lève un McpError actionnable."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=f"Paramètre `{axis}` invalide : {value!r} (attendu un id)."))


def require_axis_sub(axis: str) -> str:
    """sub authentifié courant, requis pour garder un axe-contexte ; McpError sinon
    (un axe piloté par un tenant n'a aucun sens sans identité — vaut aussi pour
    l'endpoint MCP anonyme, cf. #108)."""
    sub = None
    try:
        sub = current_user_sub_from_token()
    except Exception:
        pass
    if not sub:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=f"Le paramètre `{axis}` requiert une session authentifiée."))
    return sub


# ── Axe account= (connecteurs multi-compte) ──────────────────────────────────

def _is_multi_account_tool(name: str) -> bool:
    """Le tool appartient-il à un connecteur MULTI-COMPTE (coffre à N comptes,
    `Connector.auth_multi_account`) ? Dérivé du registre via le namespace → seuls
    zoho/google exposent `account=` aujourd'hui (« 2 Zoho », gmail/tasks/calendar).
    Les caps spine (`oto_*`) et les connecteurs mono-compte renvoient None → exclus."""
    con = providers.connector_for_namespace(namespace_of(name))
    return con is not None and con.auth_multi_account


async def _pin_account(value: object) -> list[UndoEntry]:
    """Épingle le compte de connecteur de l'appel courant. Pas de garde DB ici : le
    compte n'est qu'un LABEL, la garde vit à la résolution (`resolve_credential` lève
    une McpError actionnable si ce compte n'existe pas au palier membre — jamais de
    repli muet vers un autre compte). None/'' ⇒ inerte (mono-compte legacy)."""
    if value is None or value == "":
        return []
    return [(session_org.reset_call_account, session_org.set_call_account(str(value)))]


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


# ── Axe project= (slots de tableau — enforcement serveur ADR 0035) ────────────

def _is_slot_aware_tool(name: str) -> bool:
    """Le tool résout-il un `slot:<name>` contre le projet actif ? Les tools `data_*`
    (namespace `data`, ADR 0035 B3) — `resolve_slot_tableau` LÈVE sans projet actif
    (enforcement dur, jamais de fallback) → c'est le cas où la perte du bracelet de
    session casse un flux. L'épinglage d'IDENTITÉ connecteur par projet reste fail-soft
    (repli sur le défaut user) → hors périmètre de l'axe pour l'instant."""
    return namespace_of(name) == "data"


def _resolve_project_org_guarded(sub: str, pid: int, subdomain_org: Optional[int]) -> Optional[int]:
    """Garde d'accès + dérivation de l'org propriétaire du projet (chemin DB sync,
    appelé en threadpool). Lève une McpError actionnable si l'acteur n'a pas accès en
    lecture (privacy-by-default ADR 0030 — jamais is_org_member), si le projet n'existe
    pas, ou s'il échappe au lock de sous-domaine. L'org dérivée est co-posée pour que
    credentials/redaction/datastore résolvent sous l'org du projet."""
    from . import group_store, org_store, ownership
    if not ownership.can_access(sub, "project", str(pid), "read"):
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"Projet #{pid} inaccessible (tu n'y as pas accès en lecture, ou il "
                     "n'existe pas). Liste tes projets avec `oto_project op=list`.")))
    owner = ownership.owner_of("project", str(pid))
    if owner is None:
        raise McpError(ErrorData(
            code=INVALID_PARAMS, message=f"Projet #{pid} introuvable."))
    owner_type, owner_id = owner
    if owner_type == "org":
        org = int(owner_id)
    elif owner_type == "group":
        g = group_store.get_group(int(owner_id))
        org = g.get("org_id") if g else None
    elif owner_type == "user":
        # Projet perso : l'org co-posée est l'org PERSO du PROPRIÉTAIRE (jamais
        # int(sub) — le sub n'est pas un id d'org).
        org = org_store.get_personal_org(owner_id)
    else:
        org = None
    if subdomain_org is not None and org is not None and org != subdomain_org:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"Le projet #{pid} appartient à une autre org que celle de ce "
                     "endpoint (verrou de sous-domaine) — impossible de l'activer ici.")))
    return org


async def _pin_project(value: object) -> list[UndoEntry]:
    """Épingle le projet de l'appel + co-pose l'org dérivée. Rejette l'anonyme AVANT
    toute pose. Garde `can_access` en threadpool (DB sur le chemin inbound chaud)."""
    if value is None:
        return []
    pid = require_axis_int(value, "project")
    sub = require_axis_sub("project")
    cand = session_org.current_subdomain_candidate()
    org = await run_in_threadpool(_resolve_project_org_guarded, sub, pid, cand)
    undo: list[UndoEntry] = []
    if org is not None:
        undo.append((session_org.reset_call_org, session_org.set_call_org(org)))
    undo.append((session_org.reset_call_project, session_org.set_call_project(pid)))
    return undo


PROJECT = CallAxis(
    param="project",
    schema={
        "type": "integer",
        "title": "Project",
        "description": (
            "Projet à activer pour CET appel (id, cf. `oto_project op=list`). Résout les "
            "`slot:<nom>` de `namespace` contre les bindings du projet et scope l'action "
            "sur son org. Alternative sans état au bracelet `oto_use_project`."
        ),
    },
    applies=_is_slot_aware_tool,
    pin=_pin_project,
)


# ── Axe run_id= (corrélation d'un appel à un déroulé — ADR 0017) ──────────────

def _is_work_tool(name: str) -> bool:
    """Le tool fait-il un TRAVAIL qu'on voudrait rattacher à un run ? = tool d'un
    connecteur (registre) OU `data_*` (datastore). Exclut le spine méta/identité/
    boucle d'usage (`oto_*`, `run_*`, `feedback`) — corréler `run_start` à lui-même
    ou `oto_whoami` à un run n'a pas de sens. Sélectif : la corrélation vit sur la
    surface de travail, pas sur toute la surface (coût tokens de `tools/list`)."""
    ns = namespace_of(name)
    return ns == "data" or providers.connector_for_namespace(ns) is not None


async def _pin_run(value: object) -> list[UndoEntry]:
    """Épingle le run_id de l'appel courant (corrélation calllog, modèle sans état de
    session : la pile session-scopée de `doctrine_run` ne survit pas au renouvellement
    de session claude.ai). Le sink calllog lit `current_call_run()` EN PRIORITÉ, repli
    sur la pile. Pas de garde : un run_id est un identifiant opaque de corrélation, pas
    un axe de droits. None/'' ⇒ inerte."""
    if value is None or value == "":
        return []
    return [(session_org.reset_call_run, session_org.set_call_run(str(value)))]


RUN = CallAxis(
    param="run_id",
    schema={
        "type": "string",
        "title": "Run Id",
        "description": (
            "run_id d'un déroulé ouvert par `run_start`, à repasser sur les appels de "
            "CE run pour les corréler (la corrélation ne survit pas sinon au modèle sans "
            "état de session). Omets hors de tout run."
        ),
    },
    applies=_is_work_tool,
    pin=_pin_run,
)


# Axes exposés sur les tools plats (chacun via les 3 mécanismes : advertise / strip+pose / seam).
AXES: tuple[CallAxis, ...] = (ACCOUNT, PROJECT, RUN)


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
