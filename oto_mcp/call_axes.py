"""Axes-contexte d'appel sur la surface des tools PLATS (modèle sans état de session,
#108/#112).

claude.ai renouvelle le `Mcp-Session-Id` à CHAQUE appel → tout état de session serveur
(compte de connecteur actif, projet actif, run en cours) est perdu d'un appel au
suivant. La parade : porter le contexte en **identifiants d'appel** explicites plutôt
qu'en bracelet serveur. Pour les tools de capacité, `_org=` est injecté par
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

**Les descriptions de `schema` sont VOLONTAIREMENT courtes** (issue #277). Elles sont
recopiées à l'identique dans le schéma de CHAQUE tool concerné : ce qu'on écrit ici est
multiplié par ~350. Mesuré avant la coupe : 1 845 caractères de prose d'axe par tool,
soit **61 % du poids total des schémas** de `mcp.oto.cx` (561 k caractères sur 914 k) —
six paragraphes répétés, que la plupart des clients paient sans jamais s'en servir.
Le *pourquoi* de ces jetons (modèle sans état, préfixe `_`, quoi faire quand plusieurs
comptes existent…) vit donc **une seule fois**, dans le bloc A des instructions serveur
(`instructions.py`, « Porte ton contexte DANS l'appel ») — injecté au handshake, donc lu
par tout client. Ici : ce que l'axe fait, et où trouver sa valeur. Rien de plus.

**Deuxième coupe (14/08), sur la même règle.** Re-mesuré : les axes pesaient encore
**424 744 c., soit 48,2 % des 880 520 c. de schémas servis** — la première coupe avait
raccourci les paragraphes, pas fermé la multiplication par 410 outils. Les six
descriptions redisaient ce que le bloc A dit déjà (le *pourquoi*, la marche à suivre
quand plusieurs comptes existent, ce qui se passe si on omet le jeton), et ce redit-là
se paie à CHAQUE tour de CHAQUE agent. Ne restent que les deux choses qu'un lecteur de
schéma seul ne peut pas déduire : **ce que l'axe fait, et l'outil qui liste ses valeurs**
— la clause « Omets pour ton défaut » part, parce qu'un paramètre optionnel omis prend
son défaut, ce que le modèle sait sans qu'on le lui écrive 2 000 fois.
⚠️ **La règle est un BUDGET, pas une consigne** : `tests/test_call_axes_budget.py` borne
le coût agrégé des axes. Rallonger une description ici est un choix qui doit se voir.

**Les jetons sont NAMESPACÉS `_` (issue #250)** : `_org`, `_project`, `_group`,
`_account`, `_instance`, `_run_id`. Ils occupaient auparavant les noms NUS, dans le même
espace plat que les arguments métier des tools — or `account`, `org`, `group`, `project`
sont le vocabulaire courant d'une API B2B. Deux collisions vécues en prod (`oto_use_org`
2026-07-04 : l'org cible mangée ; `aiark_company_search` 2026-07-28 : le filtre société
mangé, AI Ark renvoyant sa base entière sans la moindre erreur) avant qu'on préfixe. Le
préfixe `_` est la convention méta déjà en vigueur en SORTIE (`_mcp_adapter` injecte
l'écho `_org` dans les payloads) : `_org` entre, `_org` sort.
"""
from __future__ import annotations

import copy
import dataclasses
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from .mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS
from starlette.concurrency import run_in_threadpool

from . import providers, session_org
from .auth.hooks import current_user_sub_from_token
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
    si l'axe co-pose — ex. _project= pose projet + org dérivée). `pin_named(value, name)`
    = variante qui reçoit AUSSI le nom du tool (garde dépendante du tool, ex. le match
    connecteur d'`_instance=`) — prime sur `pin` si présent."""
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


# ── Helpers partagés (aussi utilisés par le middleware pour `_org=`) ───────────

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
    # Un échec d'identité MONTE (journalisé par le seam avec sa raison, #464) : le
    # refus ci-dessous ne vaut que pour un appel réellement sans jeton.
    sub = current_user_sub_from_token()
    if not sub:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=f"Le paramètre `{axis}` requiert une session authentifiée."))
    return sub


async def resolve_org_guarded(org: object) -> int:
    """Résout un `_org=` (id ou nom) → org_id, gardé par la MÊME résolution qu'`oto_use_org`
    (`org_store.resolve_org_for_user` : appartenance réelle du sub). McpError PROPRE en cas
    d'échec — jamais une exception opaque. Partagé par le middleware (org= des capacités,
    `CallContextMiddleware._pin_org`), l'axe plat `_org=` et `oto_call(_org=)`. DB en
    threadpool (chemin inbound chaud, mono-loop)."""
    org_id = require_axis_int(org, "_org")
    sub = require_axis_sub("_org")
    # Un jeton de délégation n'agit que dans l'org de son travail (`verrou_org.py`) :
    # une autre org est refusée ici, nommément, avant la garde d'appartenance.
    from . import session_org
    session_org._hors_verrou(org_id, "_org")
    from . import org_store
    try:
        return await run_in_threadpool(org_store.resolve_org_for_user, sub, str(org_id))
    except ValueError:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=f"Paramètre `_org`={org_id} refusé : tu n'es membre d'aucune "
                    f"org #{org_id}. Vérifie avec oto_list_orgs."))
    except McpError:
        raise
    except Exception:
        logger.exception("garde `_org=` a levé pour sub=%s org=%s", sub, org_id)
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=f"Impossible de vérifier ton accès à l'org #{org_id} "
                    f"(erreur interne). Réessaie."))


# ── Axe _account= (connecteurs multi-compte) ──────────────────────────────────

def _has_account_axis(name: str) -> bool:
    """Le tool ADVERTISE-t-il l'axe compte dans son schéma, STATIQUEMENT ? Deux familles
    (ADR 0024/0051), toutes deux CURÉES :
    - **annonce statique déclarée** (`Connector.account_axis_static` : google
      « N comptes OAuth », zoho « 2 Zoho », browser, folk) — depuis le 29/08 c'est un
      champ de l'entrée du connecteur, plus la liste transverse
      `MULTI_ACCOUNT_PROVIDERS`, qui mélangeait cette annonce et la cardinalité ;
    - **porteur d'identités** (`personal_cross_org`) : 1 clé → N identités opérées
      (unipile : le compte LinkedIn/WhatsApp à opérer — le tien, ou un compte accordé #55).

    ⚠️ Depuis 2026-08-25, TOUT connecteur à clé d'API est multi-compte
    (`Connector.auth_multi_account`) — mais l'axe n'est PAS advertisé statiquement sur
    tous : chaque propriété d'axe est recopiée dans le schéma de chaque tool à chaque
    handshake (test_call_axes_budget : ~50 connecteurs de plus = la moitié de la
    surface). Il l'est **dynamiquement** là où il a un sens pour l'appelant — les
    connecteurs où il détient ≥ 2 clés (`account_axis_advertised_for`) — et il est
    **accepté** à l'appel sur tout connecteur multi-compte (`accepts_account_axis`),
    advertisé ou non. Dérivé du registre via le namespace ; spine (`oto_*`) → None → exclus."""
    con = providers.connector_for_namespace(namespace_of(name))
    return con is not None and (con.account_axis_static or con.personal_cross_org)


def accepts_account_axis(name: str) -> bool:
    """Le tool LIT-il `_account=` s'il est fourni ? Fonctionnel, pas cosmétique : tout
    connecteur multi-compte (clé d'API par défaut, backends curés, porteurs d'identités).
    Un agent qui a listé ses comptes (`oto_identity(op='list')`) peut viser l'un d'eux
    même si le schéma ne l'annonçait pas — le schéma n'annonce que ce qui a un sens
    pour lui (cf. `account_axis_advertised_for`)."""
    con = providers.connector_for_namespace(namespace_of(name))
    if con is None:
        return False
    if con.personal_cross_org:
        return True
    # Volontairement org-AGNOSTIQUE : cet axe est lu par le middleware d'appel, où
    # l'org de contexte coûterait une requête PAR APPEL. Et il n'autorise rien — il
    # NOMME un compte, la résolution refuse (actionnable) si ce compte n'existe pas au
    # palier. Le refuser rendrait en revanche une org ÉLARGIE par surcharge incapable
    # de viser son second compte : la clé posée que rien ne va lire (oto-backend#409).
    from .connectors import cardinality
    return cardinality.accepted_anywhere(con.name)


def _palier_entities(sub: str) -> list[tuple[str, str, str]]:
    """Les paliers du coffre où l'appelant peut avoir des comptes nommés, avec leur
    étiquette : les siens, ceux de son équipe active, ceux de son org de contexte.
    Mêmes paliers que `connectors.identities.keyed_entity` (member/group/org)."""
    from . import access, credentials_store
    org = access.current_org(sub)
    if org is None:
        return []
    out = [(credentials_store.MEMBER, credentials_store.member_id(org, sub), "")]
    gid = access.current_group(sub)
    if gid is not None:
        out.append(("group", str(gid), "équipe"))
    out.append(("org", str(org), "org"))
    return out


def account_axis_advertised_for(sub: Optional[str]) -> dict[str, str]:
    """Connecteurs pour lesquels l'APPELANT atteint ≥ 2 comptes, tous paliers
    confondus (les siens, son équipe, son org) → description de l'axe `_account`
    qui NOMME ces comptes. Là, et là seulement, l'axe vaut d'être annoncé : un compte
    unique se résout tout seul. Un compte nommé se vise à n'importe quel palier
    (access/resolve.py), donc la liste est l'union. Une requête par palier et par
    tools/list ; jamais d'exception (une liste d'outils ne tombe pas pour un coffre
    injoignable → axe non annoncé, encore accepté à l'appel)."""
    if not sub:
        return {}
    try:
        from . import access, credentials_store
        by_name = {c.name: c for c in providers._REGISTRY_LIST}
        found: dict[str, list[str]] = {}
        from . import group_store
        for etype, eid, palier in _palier_entities(sub):
            rows = list(credentials_store.list_credentials(etype, eid))
            if etype == "group":
                rows += [{"connector": i["connector"], "account": i["account"], "meta": {}}
                         for i in group_store.lent_instances(int(eid))]
            for row in rows:
                con = by_name.get(row["connector"])
                if con is None or not con.auth_multi_account:
                    continue
                marks = [m for m in (palier, "défaut" if (row.get("meta") or {}).get(
                    "is_default") else "") if m]
                nom = f"`{row['account']}`" if row["account"] else "(sans nom)"
                found.setdefault(con.name, []).append(
                    f"{nom} ({', '.join(marks)})" if marks else nom)
        return {name: (f"{access.account_noun(name).capitalize()} à OPÉRER parmi : "
                       f"{', '.join(comptes)}. `oto_identity(op='list')`.")
                for name, comptes in found.items() if len(comptes) >= 2}
    except Exception:
        logger.exception("account_axis_advertised_for: relevé des comptes impossible")
        return {}


def axes_for_listing(name: str, advertised_accounts: dict[str, str]) -> list["CallAxis"]:
    """Axes à ANNONCER dans le schéma de ce tool : les statiques (`axes_for`) + l'axe
    compte quand l'appelant atteint plusieurs comptes de ce connecteur — sa
    description nomme alors ces comptes (`account_axis_advertised_for`)."""
    axes = axes_for(name)
    if advertised_accounts and not any(a.param == ACCOUNT.param for a in axes):
        con = providers.connector_for_namespace(namespace_of(name))
        if con is not None and con.name in advertised_accounts:
            axes = [*axes, dataclasses.replace(ACCOUNT, schema={
                **ACCOUNT.schema, "description": advertised_accounts[con.name]})]
    return axes


def axes_for_call(name: str) -> list["CallAxis"]:
    """Axes LUS à l'appel : les statiques + l'axe compte sur tout connecteur qui
    l'accepte (annoncé ou non)."""
    axes = axes_for(name)
    if accepts_account_axis(name) and not any(a.param == ACCOUNT.param for a in axes):
        axes = [*axes, ACCOUNT]
    return axes


async def _pin_account(value: object) -> list[UndoEntry]:
    """Épingle le compte de connecteur de l'appel courant. Pas de garde DB ici : le
    compte n'est qu'un LABEL, la garde vit à la résolution (`resolve_credential` lève
    une McpError actionnable si ce compte n'existe pas au palier membre — jamais de
    repli muet vers un autre compte). None/'' ⇒ inerte (mono-compte legacy)."""
    if value is None or value == "":
        return []
    return [(session_org.reset_call_account, session_org.set_call_account(str(value)))]


ACCOUNT = CallAxis(
    param="_account",
    schema={
        "type": "string",
        "title": "Account",
        "description": (
            "Compte à OPÉRER quand plusieurs existent. `oto_identity(op='list')`."
        ),
    },
    applies=_has_account_axis,
    pin=_pin_account,
)


# ── Axe _project= (slots de tableau — enforcement serveur ADR 0035) ────────────

# Verbes de RUN (`run_start`/`run_finish`). Ce ne sont pas des tools de travail — mais
# le run est l'objet dont le métier est de CORRÉLER des appels de travail, donc il doit
# porter les mêmes jetons de contexte qu'eux (#290). Sans ça, le modèle sans état de
# l'ADR 0038 était appliqué à moitié : tout était devenu jeton d'appel SAUF l'objet qui
# relie les appels. Conséquences mesurées en prod — `runs.project_id` TOUJOURS NULL (son
# seul alimentateur est `access.current_project()`, que seul `_pin_project` remplit), donc
# quatre lecteurs qui interrogent une colonne morte ; et `runs.org_id` = l'org MAISON même
# quand tout le déroulé se fait sous `_org=`, donc un run rangé sous la mauvaise lentille.
# ⚠️ SEULEMENT les axes de CONTEXTE (`_project`/`_org`/`_group`) : `_run_id=` reste hors
# de portée (`_is_run_correlatable_tool`) — corréler `run_start` à un autre run n'a pas
# de sens, c'est lui qui en ouvre un.
_RUN_VERBS = frozenset({"run_start", "run_finish"})


def _is_project_scopable_tool(name: str) -> bool:
    """Tool de TRAVAIL (connecteurs + `data_*`) : `_project=` est le jeton PRIMAIRE
    du modèle sans état (ADR 0038 §A — l'org en dérive, les slots `slot:<name>` s'y
    résolvent, l'identité connecteur préfaite du projet s'y épingle). Élargi de
    `data_*` seul à toute la surface de travail au retrait du bracelet
    `oto_use_project` (B3b) — l'axe est le SEUL porteur du contexte projet.

    Les **verbes de run** s'y ajoutent (#290) : `run_start` GÈLE le projet dans
    `runs.project_id` en lisant ce même seam, et un run se déroule dans un projet
    aussi souvent que les appels qu'il encadre."""
    ns = namespace_of(name)
    return (ns == "data" or providers.connector_for_namespace(ns) is not None
            or name in _RUN_VERBS)


def _resolve_project_context_guarded(
    sub: str, pid: int, subdomain_org: Optional[int],
) -> tuple[Optional[int], Optional[int], object]:
    """Garde d'accès + dérivation du CONTEXTE (org, groupe) propriétaire du projet
    (chemin DB sync, appelé en threadpool). Lève une McpError actionnable si l'acteur
    n'a pas accès en lecture (privacy-by-default ADR 0030 — jamais is_org_member), si le
    projet n'existe pas, ou s'il échappe au lock de sous-domaine.

    L'org dérivée (`heritage.contexte_du_projet`) est co-posée pour que
    credentials/redaction/datastore résolvent sous l'org du projet. **Pour un projet
    d'ÉQUIPE, le groupe propriétaire est co-posé** si l'appelant le lit
    (`can_read_group`) : ouvrir le projet résout alors la cascade de cette équipe de
    façon DÉTERMINISTE, indépendante du groupe actif (#218). Jamais l'inverse (poser
    un groupe sur un projet org-owned).

    3e valeur : le verdict des CLÉS du projet (`access.heritage`, #480) — posé quand
    l'appelant n'atteint pas de lui-même toutes les clés du propriétaire (hors de son
    org, ou hors de son équipe). Un bénéficiaire d'un simple partage travaille avec
    SES clés ; celles du propriétaire ne lui sont prêtées que par héritage déclaré au
    partage, borné aux droits du partageur. Même règle pour les trois types de
    bénéficiaire (personne, équipe, org)."""
    from . import ownership, roles
    from .access import heritage
    if not ownership.can_access(sub, "project", str(pid), "read"):
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"Projet #{pid} inaccessible (tu n'y as pas accès en lecture, ou il "
                     "n'existe pas). Liste tes projets avec `oto_project op=list`.")))
    ctx = heritage.contexte_du_projet(pid)
    if ctx is None:
        raise McpError(ErrorData(
            code=INVALID_PARAMS, message=f"Projet #{pid} introuvable."))
    org, owner_group = ctx
    group = owner_group if (owner_group is not None
                            and roles.can_read_group(sub, owner_group)) else None
    if subdomain_org is not None and org is not None and org != subdomain_org:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"Le projet #{pid} appartient à une autre org que celle de ce "
                     "endpoint (verrou de sous-domaine) — impossible de l'activer ici.")))
    return org, group, heritage.evaluer(sub, pid, org, owner_group, group)


async def _pin_project(value: object) -> list[UndoEntry]:
    """Épingle le projet de l'appel + co-pose l'org dérivée (+ le groupe propriétaire
    pour un projet d'équipe, + le verdict des clés pour un bénéficiaire). Rejette
    l'anonyme AVANT toute pose. Garde `can_access` en threadpool (DB sur le chemin
    inbound chaud)."""
    if value is None:
        return []
    pid = require_axis_int(value, "_project")
    sub = require_axis_sub("_project")
    cand = session_org.current_subdomain_candidate()
    org, group, cles = await run_in_threadpool(_resolve_project_context_guarded, sub, pid,
                                               cand)
    undo: list[UndoEntry] = []
    if org is not None:
        undo.append((session_org.reset_call_org, session_org.set_call_org(org)))
    if group is not None:
        undo.append((session_org.reset_call_group, session_org.set_call_group(group)))
    if cles is not None:
        undo.append((session_org.reset_call_cles, session_org.set_call_cles(cles)))
    undo.append((session_org.reset_call_project, session_org.set_call_project(pid)))
    return undo


PROJECT = CallAxis(
    param="_project",
    schema={
        "type": "integer",
        "title": "Project",
        "description": (
            "Projet (id) de CET APPEL — jeton PRIMAIRE. `oto_project(op='list')`."
        ),
    },
    applies=_is_project_scopable_tool,
    pin=_pin_project,
)


# ── Axe _run_id= (corrélation d'un appel à un déroulé — ADR 0017) ──────────────

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
# `oto_fleet` (#830) : pas pour corréler un travail, mais parce que ses deux gardes
# anti-agent (`not_from_a_run`, `not_your_own_fleet`) se décident sur le run de
# l'appel — et que le runner hébergé ne pose `_run_id` QUE sur un outil qui le
# déclare. Hors de cette liste, les gardes étaient vertes et inertes.
_RUN_SPINE_TOOLS = frozenset({"oto_project", "oto_project_files", "oto_doc", "oto_resource",
                              "oto_fleet"})


def _is_run_correlatable_tool(name: str) -> bool:
    """Surface de corrélation d'un run = tools de travail + spine projet. Le reste
    du spine méta/identité/boucle d'usage (`oto_whoami`, `run_*`, `feedback`) reste
    exclu — s'y corréler n'a pas de sens."""
    return _is_work_tool(name) or name in _RUN_SPINE_TOOLS


async def _pin_run(value: object) -> list[UndoEntry]:
    """Épingle le run_id de l'appel courant (corrélation calllog, modèle sans état de
    session : la pile session-scopée de `guide_run` ne survit pas au renouvellement
    de session claude.ai). Le sink calllog lit `current_call_run()` EN PRIORITÉ, repli
    sur la pile. Pas de garde : un run_id est un identifiant opaque de corrélation, pas
    un axe de droits. None/'' ⇒ inerte. L'ORG du run, elle, se pose après tous les axes
    et gardée (`run_org.pin_for_call`, #639) — une autre pose, pas celle-ci."""
    if value is None or value == "":
        return []
    return [(session_org.reset_call_run, session_org.set_call_run(str(value)))]


# ⚠️ **Cette description est la SEULE de call_axes qui dépasse le plafond ordinaire**
# (110 c., `test_call_axes_budget`) — et c'est un choix mesuré, pas une dérive.
# L'ancienne (« run_id d'un `run_start` — le run ACTIF s'applique déjà. ») disait au
# modèle qu'il pouvait OMETTRE le jeton : vrai sur un serveur qui tient une session avec
# un run actif, faux sur le chemin de production où chaque appel a sa propre session
# (claude.ai renouvelle le `Mcp-Session-Id`). Mesuré le 29/08/2026 (#547) : `_run_id`
# passé sur 140/140 réservations puis omis à l'écriture, et **31 écritures refusées sur
# 100**, toutes sur la ligne que l'appelant tenait lui-même. Le texte de l'outil, relu à
# chaque appel, pèse plus que la consigne lue une fois au handshake.
# Trois faits qu'un lecteur de schéma ne peut pas déduire, et qu'on paie donc ici :
# **obligatoire**, **ce que coûte l'omission**, **l'exception nommée** (l'héritage).
# Coût mesuré : +135 c. sur 548 des 558 outils, soit ~74 k c. de handshake
# (633 → 766 c. d'axes par outil) — le prix de 31 lignes perdues sur 100.
# ⚠️ **Phrase UNIQUE, jamais choisie à l'appel** : beaucoup de clients récupèrent
# `tools/list` une fois à la poignée de main et le figent pour la session. Une variante
# « tu tiens déjà un run » n'atteindrait jamais un modèle en session longue et ne
# marcherait que là où chaque appel est sa propre session — un correctif en trompe-l'œil.
RUN = CallAxis(
    param="_run_id",
    schema={
        "type": "string",
        "title": "Run Id",
        "description": (
            "OBLIGATOIRE à chaque appel dès `run_start` ou `data_claim_next` : sans lui, "
            "l'écriture sur une ligne réservée est refusée. Hérité seulement si le "
            "serveur tient un run actif — n'y compte pas."
        ),
    },
    applies=_is_run_correlatable_tool,
    pin=_pin_run,
)


# Les tools qui DÉCLARENT `_run_id` dans leur propre signature et le traitent
# eux-mêmes : le middleware ne doit pas le leur retirer avant le dispatch, sinon il
# leur mange un paramètre déclaré. Aujourd'hui `oto_call` seul — il rejoue la boucle
# d'axes pour sa CIBLE (`tools/meta`), donc le jeton doit lui parvenir intact.
# ⚠️ Une liste par nom rouille en silence : `test_call_axes_run.py` vérifie que
# chacun de ces noms déclare bien le paramètre sur la surface servie.
RUN_SELF_HANDLED: frozenset[str] = frozenset({"oto_call"})


# ── Axe _org= (org d'exécution de l'appel — connecteurs + data + whoami) ───────

def _is_org_scopable_tool(name: str) -> bool:
    """Tool PLAT dont l'action dépend de l'org (résolution de credential/visibilité/
    données) : tools de TRAVAIL (connecteurs + `data_*`) + `oto_whoami` (lecture de
    l'identité effective) + les **verbes de run** (#290 — l'org d'un run est celle sous
    laquelle il se déroule, pas l'org d'habitude de celui qui l'ouvre ; c'est elle que
    `runs.org_id` gèle et que scope la lentille d'org). Les CAPACITÉS reçoivent déjà
    `_org=` par `_mcp_adapter` (elles ne sont pas des tools de travail → exclues ici, pas
    de double-traitement).

    L'axe `_group=` partage ce prédicat : un run ouvert sous une équipe co-pose l'org
    parente, invariant `groupe ⊂ org` par construction (cf. `_pin_group`)."""
    return _is_work_tool(name) or name == "oto_whoami" or name in _RUN_VERBS


async def _pin_org_flat(value: object) -> list[UndoEntry]:
    """Épingle l'org d'exécution de l'appel (même garde qu'`oto_use_org`, via
    `resolve_org_guarded`). Lue par le seam `current_org` → credentials/visibilité/
    données résolus sous cette org, sans dépendre du bracelet de session."""
    if value is None:
        return []
    return [(session_org.reset_call_org, session_org.set_call_org(
        await resolve_org_guarded(value)))]


ORG = CallAxis(
    param="_org",
    schema={
        "type": "integer",
        "title": "Org",
        "description": (
            "Org (id) de CET APPEL. `oto_list_orgs()`."
        ),
    },
    applies=_is_org_scopable_tool,
    pin=_pin_org_flat,
)


# ── Axe _group= (équipe d'exécution de l'appel — ADR 0038 B3) ──────────────────

def _resolve_group_guarded(sub: str, gid: int) -> dict:
    """Garde de lecture du groupe (chemin DB sync, appelé en threadpool). Même garde
    que la re-garde de `current_group` (`roles.can_read_group` : membre du groupe ou
    escalade org_admin). McpError actionnable sinon."""
    from . import group_store, roles
    g = group_store.get_group(gid)
    if not g:
        raise McpError(ErrorData(
            code=INVALID_PARAMS, message=f"Paramètre `_group`={gid} : groupe inconnu."))
    if not roles.can_read_group(sub, gid):
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"Paramètre `_group`={gid} refusé : tu n'es pas membre de ce "
                     "groupe. Vérifie avec oto_group(op='list').")))
    return g


async def _pin_group(value: object) -> list[UndoEntry]:
    """Épingle l'équipe de l'appel + co-pose l'org PARENTE du groupe (invariant
    « groupe ⊂ org » par construction — comme `_project=` co-pose l'org du projet).
    Si `_org=` est aussi passé, l'org du groupe prime (le jeton le plus spécifique)."""
    if value is None:
        return []
    gid = require_axis_int(value, "_group")
    sub = require_axis_sub("_group")
    g = await run_in_threadpool(_resolve_group_guarded, sub, gid)
    undo: list[UndoEntry] = []
    org = g.get("org_id")
    if org is not None:
        undo.append((session_org.reset_call_org, session_org.set_call_org(int(org))))
    undo.append((session_org.reset_call_group, session_org.set_call_group(gid)))
    return undo


GROUP = CallAxis(
    param="_group",
    schema={
        "type": "integer",
        "title": "Group",
        "description": (
            "Équipe (id) de CET APPEL, sous son org parente."
        ),
    },
    applies=_is_org_scopable_tool,
    pin=_pin_group,
)


# ── Axe _instance= (instance de connecteur explicite — ADR 0038 §C / B6) ───────

def _is_instance_scopable_tool(name: str) -> bool:
    """Tool d'un connecteur du REGISTRE (le ref d'instance projette le coffre, qui
    est keyé par provider). Exclut `data_*` (le datastore n'est pas encore un
    connecteur — B7) et le spine."""
    return providers.connector_for_namespace(namespace_of(name)) is not None


async def _pin_instance(value: object, tool_name: str) -> list[UndoEntry]:
    """Épingle l'instance explicite de l'appel (§C : `_instance=` prime sur la
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
            message=(f"Paramètre `_instance` invalide : {value!r}. Un ref s'obtient "
                     "via oto_instance(op='list') (opaque, à repasser tel quel).")))
    sub = require_axis_sub("_instance")
    con = providers.connector_for_namespace(namespace_of(tool_name))
    if con is not None and ref.connector is not None and ref.connector != con.name:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"Paramètre `_instance` refusé : ce ref est une instance "
                     f"`{ref.connector}`, pas `{con.name}` (le connecteur de ce tool).")))
    from . import access
    org = await run_in_threadpool(access.guard_instance_access, sub, ref)
    undo: list[UndoEntry] = []
    if org is not None:
        undo.append((session_org.reset_call_org, session_org.set_call_org(int(org))))
    undo.append((session_org.reset_call_instance, session_org.set_call_instance(ref)))
    return undo


INSTANCE = CallAxis(
    param="_instance",
    schema={
        "type": "string",
        "title": "Instance",
        "description": (
            "Instance de connecteur PRÉCISE (un credential exact). `oto_instance(op='list')`."
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


# Ancien nom NU → jeton namespacé (issue #250). Dérivé des axes : rien à tenir à jour.
LEGACY_PARAM_RENAMES: dict[str, str] = {a.param.lstrip("_"): a.param for a in AXES}


def reject_legacy_axis_names(args: dict, parameters: Optional[dict]) -> None:
    """Lève si un ANCIEN nom nu (`org`, `project`, `account`…) est passé à un tool qui ne
    le déclare pas comme argument métier.

    Pas d'alias — le nom nu appartient désormais aux tools (ADR 0047 : on assume les
    ruptures de surface MCP). Mais surtout **pas de retrait silencieux** : un `org=3`
    avalé sans bruit ferait tourner l'appel sous une AUTRE org que celle demandée, et
    c'est précisément le mode de panne que ce renommage corrige. Un refus qui NOMME le
    nouveau jeton est la seule sortie honnête."""
    declared = set((parameters or {}).get("properties") or {})
    for legacy, param in LEGACY_PARAM_RENAMES.items():
        if legacy in args and legacy not in declared:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=(
                    f"`{legacy}` n'est plus un jeton de contexte d'appel : utilise "
                    f"`{param}`. Les jetons de plateforme sont préfixés `_` (issue #250) "
                    "— le nom nu est réservé aux arguments métier des tools.")))


def strip_unconsumed_axes(args: dict) -> None:
    """Retire des arguments les jetons de contexte restants (mutation en place).

    À appeler APRÈS la boucle de pose (`axes_for` + `pin_for`, qui consomme déjà les axes
    applicables). Ce qui reste est un jeton **sans effet pour cette cible** — `_instance=`
    sur un `data_*`, `_org=` sur un tool non org-scopable — et le laisser ferait échouer la
    validation de la cible, qui ne le déclare pas.

    Le balayage est sans condition, et c'est le préfixe `_` qui le rend sûr : aucun tool
    n'a d'argument métier nommé `_org`/`_account`. Tant que les jetons portaient les noms
    NUS, ce même balayage mangeait de vrais arguments (`aiark_company_search(account=…)`,
    le filtre société) — sans erreur, avec un résultat faux. Cf. issue #250."""
    for param in (a.param for a in AXES):
        args.pop(param, None)


def inject_schema(parameters: Optional[dict], axes: list[CallAxis]) -> dict:
    """Copie le schéma d'entrée du tool en y ajoutant les propriétés d'axe (optionnelles,
    jamais `required`). `additionalProperties` inchangé (l'axe est désormais déclaré)."""
    params = copy.deepcopy(parameters) if isinstance(parameters, dict) else {
        "type": "object", "properties": {}}
    props = params.setdefault("properties", {})
    for axis in axes:
        props.setdefault(axis.param, dict(axis.schema))
    return params
