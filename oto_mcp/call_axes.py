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
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS
from starlette.concurrency import run_in_threadpool

from . import providers, session_org
from .auth_hooks import current_user_sub_from_token
from .tool_visibility import namespace_of

logger = logging.getLogger(__name__)

# Entrée d'annulation d'un axe posé : (fonction de reset, token ContextVar).
UndoEntry = tuple[Callable[[object], None], object]


@dataclass(frozen=True)
class CallAxis:
    """Un axe-contexte injectable sur les tools plats. `schema` = fragment JSON-Schema
    de la propriété (optionnelle) ajoutée. `applies(name)` décide, tool par tool, si
    l'axe est advertisé/lu. `pin(value)` garde/pose la/les ContextVar(s) et renvoie la
    LISTE d'entrées d'annulation (vide si l'axe est inerte pour cette valeur ; plusieurs
    si l'axe co-pose — ex. project= pose projet + org dérivée). `pin_named(value, name)`
    = variante qui reçoit AUSSI le nom du tool (garde dépendante du tool, ex. le match
    connecteur d'`instance=`) — prime sur `pin` si présent."""
    param: str
    schema: dict
    applies: Callable[[str], bool]
    pin: Optional[Callable[[object], Awaitable[list[UndoEntry]]]] = None
    pin_named: Optional[Callable[[object, str], Awaitable[list[UndoEntry]]]] = None

    async def pin_for(self, value: object, tool_name: str) -> list[UndoEntry]:
        """Pose l'axe pour CE tool (dispatch pin/pin_named)."""
        if self.pin_named is not None:
            return await self.pin_named(value, tool_name)
        return await self.pin(value)  # type: ignore[misc]


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


async def resolve_org_guarded(org: object) -> int:
    """Résout un `org=` (id ou nom) → org_id, gardé par la MÊME résolution qu'`oto_use_org`
    (`org_store.resolve_org_for_user` : appartenance réelle du sub). McpError PROPRE en cas
    d'échec — jamais une exception opaque. Partagé par le middleware (org= des capacités,
    `CallContextMiddleware._pin_org`), l'axe plat `org=` et `oto_call(org=)`. DB en
    threadpool (chemin inbound chaud, mono-loop)."""
    org_id = require_axis_int(org, "org")
    sub = require_axis_sub("org")
    from . import org_store
    try:
        return await run_in_threadpool(org_store.resolve_org_for_user, sub, str(org_id))
    except ValueError:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=f"Paramètre `org`={org_id} refusé : tu n'es membre d'aucune "
                    f"org #{org_id}. Vérifie avec oto_list_orgs."))
    except McpError:
        raise
    except Exception:
        logger.exception("garde `org=` a levé pour sub=%s org=%s", sub, org_id)
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=f"Impossible de vérifier ton accès à l'org #{org_id} "
                    f"(erreur interne). Réessaie."))


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
            "l'org (ex. « 2 Zoho »). Le label listé par oto_identity(op='list'). "
            "Omets si un seul compte est configuré."
        ),
    },
    applies=_is_multi_account_tool,
    pin=_pin_account,
)


# ── Axe project= (slots de tableau — enforcement serveur ADR 0035) ────────────

def _is_project_scopable_tool(name: str) -> bool:
    """Tool de TRAVAIL (connecteurs + `data_*`) : `project=` est le jeton PRIMAIRE
    du modèle sans état (ADR 0038 §A — l'org en dérive, les slots `slot:<name>` s'y
    résolvent, l'identité connecteur préfaite du projet s'y épingle). Élargi de
    `data_*` seul à toute la surface de travail au retrait du bracelet
    `oto_use_project` (B3b) — l'axe est le SEUL porteur du contexte projet."""
    ns = namespace_of(name)
    return ns == "data" or providers.connector_for_namespace(ns) is not None


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
            "Projet dans le cadre duquel exécuter CET appel (id, cf. `oto_project "
            "op=list`) — le jeton PRIMAIRE : scope l'action sur l'org du projet, résout "
            "les `slot:<nom>` contre ses bindings et épingle ses identités connecteur "
            "préfaites. À passer sur CHAQUE appel fait pour un projet (aucun état de "
            "session ne le retient)."
        ),
    },
    applies=_is_project_scopable_tool,
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


# Spine « surface de travail » : ces tools oto_* AGISSENT dans un déroulé (lier un
# tableau, poser un doc, partager une ressource) — un agent qui propage run_id comme
# le prescrivent les instructions run_start/finish ne doit pas se faire rejeter
# (« Unexpected keyword argument », feedback #168). SEULEMENT l'axe run_id : org=
# leur est déjà injecté par `_mcp_adapter` (capacités), pas de double-traitement.
_RUN_SPINE_TOOLS = frozenset({"oto_project", "oto_project_files", "oto_doc", "oto_resource"})


def _is_run_correlatable_tool(name: str) -> bool:
    """Surface de corrélation d'un run = tools de travail + spine projet. Le reste
    du spine méta/identité/boucle d'usage (`oto_whoami`, `run_*`, `feedback`) reste
    exclu — s'y corréler n'a pas de sens."""
    return _is_work_tool(name) or name in _RUN_SPINE_TOOLS


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
    applies=_is_run_correlatable_tool,
    pin=_pin_run,
)


# ── Axe org= (org d'exécution de l'appel — connecteurs + data + whoami) ───────

def _is_org_scopable_tool(name: str) -> bool:
    """Tool PLAT dont l'action dépend de l'org (résolution de credential/visibilité/
    données) : tools de TRAVAIL (connecteurs + `data_*`) + `oto_whoami` (lecture de
    l'identité effective). Les CAPACITÉS reçoivent déjà `org=` par `_mcp_adapter` (elles
    ne sont pas des tools de travail → exclues ici, pas de double-traitement)."""
    return _is_work_tool(name) or name == "oto_whoami"


async def _pin_org_flat(value: object) -> list[UndoEntry]:
    """Épingle l'org d'exécution de l'appel (même garde qu'`oto_use_org`, via
    `resolve_org_guarded`). Lue par le seam `current_org` → credentials/visibilité/
    données résolus sous cette org, sans dépendre du bracelet de session."""
    if value is None:
        return []
    return [(session_org.reset_call_org, session_org.set_call_org(
        await resolve_org_guarded(value)))]


ORG = CallAxis(
    param="org",
    schema={
        "type": "integer",
        "title": "Org",
        "description": (
            "Organisation (id) sous laquelle exécuter CET appel — résout les credentials, "
            "la visibilité et les données de cette org. Alternative fiable et sans état au "
            "bracelet `oto_use_org` (qui ne persiste pas). Omets pour ton org courante."
        ),
    },
    applies=_is_org_scopable_tool,
    pin=_pin_org_flat,
)


# ── Axe group= (équipe d'exécution de l'appel — ADR 0038 B3) ──────────────────

def _resolve_group_guarded(sub: str, gid: int) -> dict:
    """Garde de lecture du groupe (chemin DB sync, appelé en threadpool). Même garde
    que la re-garde de `current_group` (`roles.can_read_group` : membre du groupe ou
    escalade org_admin). McpError actionnable sinon."""
    from . import group_store, roles
    g = group_store.get_group(gid)
    if not g:
        raise McpError(ErrorData(
            code=INVALID_PARAMS, message=f"Paramètre `group`={gid} : groupe inconnu."))
    if not roles.can_read_group(sub, gid):
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"Paramètre `group`={gid} refusé : tu n'es pas membre de ce "
                     "groupe. Vérifie avec oto_group(op='list').")))
    return g


async def _pin_group(value: object) -> list[UndoEntry]:
    """Épingle l'équipe de l'appel + co-pose l'org PARENTE du groupe (invariant
    « groupe ⊂ org » par construction — comme `project=` co-pose l'org du projet).
    Si `org=` est aussi passé, l'org du groupe prime (le jeton le plus spécifique)."""
    if value is None:
        return []
    gid = require_axis_int(value, "group")
    sub = require_axis_sub("group")
    g = await run_in_threadpool(_resolve_group_guarded, sub, gid)
    undo: list[UndoEntry] = []
    org = g.get("org_id")
    if org is not None:
        undo.append((session_org.reset_call_org, session_org.set_call_org(int(org))))
    undo.append((session_org.reset_call_group, session_org.set_call_group(gid)))
    return undo


GROUP = CallAxis(
    param="group",
    schema={
        "type": "integer",
        "title": "Group",
        "description": (
            "Équipe (groupe, id) sous laquelle exécuter CET appel — résout les secrets "
            "et la doctrine du groupe, et scope l'action sur son org parente. Omets "
            "hors contexte d'équipe."
        ),
    },
    applies=_is_org_scopable_tool,
    pin=_pin_group,
)


# ── Axe instance= (instance de connecteur explicite — ADR 0038 §C / B6) ───────

def _is_instance_scopable_tool(name: str) -> bool:
    """Tool d'un connecteur du REGISTRE (le ref d'instance projette le coffre, qui
    est keyé par provider). Exclut `data_*` (le datastore n'est pas encore un
    connecteur — B7) et le spine."""
    return providers.connector_for_namespace(namespace_of(name)) is not None


async def _pin_instance(value: object, tool_name: str) -> list[UndoEntry]:
    """Épingle l'instance explicite de l'appel (§C : `instance=` prime sur la
    préférence de proximité, jamais de fallback si elle ne résout pas) + co-pose
    son org. Gardes : ref bien formé, connecteur du ref = connecteur du TOOL
    (anti-confusion), accès par niveau."""
    if value is None or value == "":
        return []
    from . import instance_refs
    try:
        ref = instance_refs.parse_ref(str(value))
    except ValueError:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"Paramètre `instance` invalide : {value!r}. Un ref s'obtient "
                     "via oto_instance(op='list') (opaque, à repasser tel quel).")))
    sub = require_axis_sub("instance")
    con = providers.connector_for_namespace(namespace_of(tool_name))
    if con is not None and ref.connector is not None and ref.connector != con.name:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"Paramètre `instance` refusé : ce ref est une instance "
                     f"`{ref.connector}`, pas `{con.name}` (le connecteur de ce tool).")))
    from . import access
    org = await run_in_threadpool(access.guard_instance_access, sub, ref)
    undo: list[UndoEntry] = []
    if org is not None:
        undo.append((session_org.reset_call_org, session_org.set_call_org(int(org))))
    undo.append((session_org.reset_call_instance, session_org.set_call_instance(ref)))
    return undo


INSTANCE = CallAxis(
    param="instance",
    schema={
        "type": "string",
        "title": "Instance",
        "description": (
            "Ref d'instance de connecteur (obtenu via oto_instance(op='list')) sous "
            "laquelle exécuter CET appel — résout EXACTEMENT ce credential-là (ta clé, "
            "celle d'un de tes groupes ou celle de l'org), jamais un autre. Omets pour "
            "la résolution de proximité normale."
        ),
    },
    applies=_is_instance_scopable_tool,
    pin_named=_pin_instance,
)


# Axes exposés sur les tools plats (chacun via les 3 mécanismes : advertise / strip+pose / seam).
# ⚠️ Ordre = ordre de pose : GROUP après ORG pour que son org co-posée prime (plus
# spécifique) ; INSTANCE en dernier (le jeton le plus spécifique de tous).
AXES: tuple[CallAxis, ...] = (ACCOUNT, PROJECT, RUN, ORG, GROUP, INSTANCE)


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
