"""Calcul + application de la visibilité des tools d'une session MCP.

Extrait de `middleware.disabled_tools.UserDisabledToolsMiddleware` (ADR 0009/0011/0015) pour
être **rejoué après une bascule de profil** (org/groupe actif) sans dupliquer la
logique — « derive don't duplicate ».

Deux appelants :
- **handshake** : le middleware appelle `apply_session_visibility(ctx, sub)` à
  `on_initialize` (reset=False — comportement historique : juste poser la denylist).
- **bascule à chaud** : l'adaptateur capacité (`capabilities._mcp_adapter`) l'appelle
  après `oto_use_org`/`oto_clear_org`/… avec `reset=True` pour repartir de l'état
  « tout visible » puis re-poser la denylist de la NOUVELLE org. fastmcp émet alors
  `tools/list_changed` à la session courante (cf. `disable_components`/`reset_visibility`).
"""
from __future__ import annotations

import logging
from typing import Optional

from fastmcp.server.transforms.visibility import disable_components, reset_visibility
from starlette.concurrency import run_in_threadpool

from . import access, providers, credentials_store, db, org_store
from .connectors import access_level_gate
from .connectors import activation as connector_activation
from .connectors import orphelins as connector_orphelins
from .connectors import selection as connector_selection
from .connectors import usable as connector_usable
from .error_taxonomy import _is_client_disconnect
from .tool_visibility import (
    BETA_OPTION,
    BETA_TOOLS,
    DEFAULT_HIDDEN_TOOLS,
    effective_disabled,
    is_protected,
    namespace_of,
)

logger = logging.getLogger(__name__)

# Sentinelle « dérive l'org de current_org » (défaut) — distincte de org=None/0
# (perso/global), qui est une valeur LÉGITIME.
_DERIVE_ORG = object()


# Les COUCHES du masquage, nommées. Deux familles : celles qui ne touchent qu'à
# l'AFFICHAGE (l'outil reste appelable par `oto_call`, ADR 0036) et celles derrière
# lesquelles une garde d'appel existe aussi (activation, bêta, plancher de rôle) —
# un outil masqué par l'une d'elles n'est pas appelable. Le catalogue
# (`oto_list_my_tools`) en dérive l'état de chaque outil : « installé »,
# « installable » ou « non exposé » — sans recopier une seule des règles ci-dessous.
COUCHE_TOGGLE = "toggle"              # désactivé par la personne, par un admin, ou masqué par défaut
COUCHE_ACTIVATION = "activation"      # connecteur non exposé à l'org (ou coupé par l'équipe)
COUCHE_SELECTION = "selection"        # connecteur non installé / en pause dans la boîte
COUCHE_BETA = "beta"                  # surface bêta sans l'option
COUCHE_HORS_DE_PORTEE = "hors_de_portee"   # plancher de rôle plateforme non atteint
COUCHE_NIVEAU = "access_level"        # niveau d'accès org/équipe qui ne couvre pas l'outil
#: Les couches qui ne masquent que l'AFFICHAGE : l'outil y reste appelable.
COUCHES_INSTALLABLES = frozenset({COUCHE_TOGGLE, COUCHE_SELECTION})


async def compute_hidden_tools(ctx, sub: str, *, org=_DERIVE_ORG) -> set[str]:
    """Ensemble effectif des tools à masquer pour `(sub, org active)` — l'union des
    couches de `compute_hidden_layers`, qui porte la documentation."""
    couches = await compute_hidden_layers(ctx, sub, org=org)
    return set().union(*couches.values())


async def compute_hidden_layers(ctx, sub: str, *, org=_DERIVE_ORG,
                                noms: "set[str] | None" = None) -> dict[str, set[str]]:
    """Les tools à masquer pour `(sub, org active)`, PAR COUCHE (`COUCHE_*` → noms).
    Chaque couche est déjà amputée des outils protégés (anti-lockout) ; leur union
    est exactement ce que `compute_hidden_tools` masque.

    Profil de visibilité = (sub, org active) ; 0 = perso/global (ADR 0015). Lit
    l'org active à CHAQUE appel → après `set_active_org`, recalcule pour la
    nouvelle org. `ctx` = `Context` fastmcp (pour `ctx.fastmcp.list_tools`).

    `org` = org de scope EXPLICITE (défaut = dérive de `current_org(sub)`, le
    comportement handshake/bascule à chaud MCP). À passer quand la vue doit
    refléter une org CONSULTÉE précise plutôt que re-dériver le contexte de
    l'acteur — ex. la carte contexte du dashboard, qui affichait sinon la
    sélection GLOBALE (org 0) au lieu de l'org consultée (oto/#5.3).

    ⚠️ **DB SYNC, hors boucle (14/09/2026)** : le corps est coupé en deux moitiés
    SYNC (`_resolve_toggle_context`, `_compute_couches`) de part et d'autre de
    l'unique `await` réellement asynchrone (`ctx.fastmcp.list_tools`), chacune
    appelée via `run_in_threadpool` — même patron que
    `middleware/dynamic_instructions.py`. Sans ça, ce hook (appelé à CHAQUE
    handshake `initialize` par `UserDisabledToolsMiddleware`) gèle tout le serveur
    mono-loop le temps d'une bonne dizaine de lectures/écritures PG : mode n°2 de
    `docs/event-loop-perf.md`, même classe que la composition d'instructions du
    15/08, restée ouverte ici faute d'un garde-fou qui la voie.

    `noms` = les outils sur lesquels calculer les couches, quand l'appelant les a
    déjà (le catalogue : le registre BRUT). ⚠️ Sans lui, la base est
    `ctx.fastmcp.list_tools`, qui applique les transformations de visibilité de la
    SESSION : juste au handshake (rien n'est encore masqué), FAUX en cours de session
    — un outil déjà masqué n'y figure plus, donc dans aucune couche, et un lecteur
    qui part du registre brut le croit « installé ». C'est ce qui faisait dire à
    `oto_list_my_tools` que les 818 outils étaient installés pendant qu'`oto_connector`
    rendait leurs connecteurs `not_selected` (oto-backend#1112)."""
    active_org, prof_org, disabled, enabled_override, role_plateforme = (
        await run_in_threadpool(_resolve_toggle_context, sub, org))
    try:
        all_names = (set(noms) if noms is not None else
                     {t.name for t in await ctx.fastmcp.list_tools(run_middleware=False)})
    except Exception as e:
        logger.warning("Cannot list tools for %s: %s", sub, e)
        # repli FAIL-CLOSED : disabled explicites + masqués-par-défaut
        # (sinon ils resteraient visibles, denylist incomplète).
        all_names = disabled | DEFAULT_HIDDEN_TOOLS
    return await run_in_threadpool(
        _compute_couches, sub, active_org, prof_org, role_plateforme,
        disabled, enabled_override, all_names)


def _resolve_toggle_context(sub: str, org):
    """1ʳᵉ moitié SYNC (DB) de `compute_hidden_layers` — résolution d'org puis
    toggles perso + rôle plateforme. Extraite À L'IDENTIQUE (même fail-open) pour
    être appelée via `run_in_threadpool` ; ne PAS y lire ni écrire de ContextVar
    (`access.current_org`/`current_group` ne font que LIRE celles de
    `session_org.py`, vérifié le 14/09/2026 — leur copie par `run_in_threadpool`
    suffit, cf. docs/event-loop-perf.md mode n°4)."""
    try:
        # Les toggles perso sont scopés par org → on lit ceux de l'org active.
        active_org = access.current_org(sub) if org is _DERIVE_ORG else org
        prof_org = active_org or 0
        disabled = set(db.list_user_disabled_tools(sub, prof_org))
        enabled_override = set(db.list_user_enabled_tools(sub, prof_org))
        # Lu UNE fois : le plancher plateforme d'un outil se compare à un RÔLE,
        # pas à un booléen — `admin` et `super_admin` n'ouvrent pas les mêmes.
        role_plateforme = access.get_user_role(sub)
    except Exception as e:
        # Sur erreur DB : repli neutre (rien de désactivé). La sécurité d'accès ne
        # dépend PAS de cette visibilité — elle est gardée au call-time (credential
        # + activation + remote credential).
        logger.warning("Cannot read tool visibility for %s: %s", sub, e)
        disabled, enabled_override, role_plateforme = set(), set(), "member"
        active_org, prof_org = None, 0
    return active_org, prof_org, disabled, enabled_override, role_plateforme


def _compute_couches(sub: str, active_org, prof_org, role_plateforme: str,
                      disabled: set[str], enabled_override: set[str],
                      all_names: set[str]) -> dict[str, set[str]]:
    """2ᵉ moitié SYNC (DB) de `compute_hidden_layers`, jouée APRÈS le seul `await`
    du calcul (`ctx.fastmcp.list_tools`, résolu dans `all_names`) — extraite À
    L'IDENTIQUE (même fail-open par couche) pour être appelée via
    `run_in_threadpool` ; aucune écriture de ContextVar ici non plus (vérifié
    14/09/2026 : `connector_activation`/`connector_selection`/`org_store` ne
    touchent aucune ContextVar — `connector_selection.seed_active` ÉCRIT en base,
    pas en ContextVar, une écriture DB depuis un thread est déjà le régime commun
    du reste de la plateforme, ADR 0004)."""
    # Denylist ADMIN (org + équipe active) : gouvernance de visibilité au grain
    # TOOL, PAS une barrière de sécurité (ADR 0031) — l'override perso positif lu
    # ci-dessus (`enabled_override`) la lève toujours, `effective_disabled` en
    # décide via `is_tool_visible`. Fail-OPEN INDÉPENDANT par palier : un hoquet
    # sur l'équipe ne doit pas priver l'org de son denylist, et inversement.
    admin_hidden: set[str] = set()
    try:
        admin_hidden |= access.org_admin_hidden_tools(active_org)
    except Exception as e:
        logger.warning("org tool denylist skipped for %s (fail-open): %s", sub, e)
    try:
        admin_hidden |= access.group_admin_hidden_tools(access.current_group(sub))
    except Exception as e:
        logger.warning("group tool denylist skipped for %s (fail-open): %s", sub, e)
    couches: dict[str, set[str]] = {
        COUCHE_TOGGLE: effective_disabled(all_names, disabled, enabled_override,
                                          frozenset(admin_hidden))}
    # Activation (ADR 0011) : masque les tools d'un connecteur non activé pour
    # l'org de la session — à chaud, per-org. Fail-OPEN (gouvernance d'exposition,
    # pas une barrière de sécurité ; le grant-only reste fail-closed ci-dessus).
    # Les tools plateforme (oto/data/guide) n'ont pas de connecteur au
    # registre → jamais gatés.
    try:
        # Tier ÉQUIPE (ADR 0012, restrict-only) : l'équipe active peut COUPER un
        # connecteur pour ses membres — `exposed_for` retranche ses coupures de l'exposé
        # (jamais d'ajout : invariant monotone). Même régime fail-open que l'org.
        exposed = connector_activation.exposed_for(active_org, access.current_group(sub))
        couches[COUCHE_ACTIVATION] = {
            n for n in all_names
            if (c := providers.connector_for_namespace(namespace_of(n))) is not None
            and c.name not in exposed
        }
    except Exception as e:
        logger.warning("activation visibility skipped for %s (fail-open): %s", sub, e)
    # Niveau d'accès (connectors/access_levels.py) : un outil qu'un niveau RESTREINT
    # (posé par l'org, ou l'équipe active qui ne fait que restreindre) ne couvre en rien
    # sort de la boîte ; les ops non couvertes d'un outil multiplexé sortent de son schéma
    # (`CallContextMiddleware.on_list_tools`). Au niveau `full`, rien. Fail-OPEN comme
    # l'activation : la barrière est la garde d'appel `access_level_denied`.
    try:
        couches[COUCHE_NIVEAU] = access_level_gate.hidden_tools(
            all_names, active_org, access.current_group(sub))
    except Exception as e:
        logger.warning("access level visibility skipped for %s (fail-open): %s", sub, e)
    # (La règle dédiée « bridges remote per-namespace » a été retirée — ADR 0034 B4 :
    # le connecteur `bridge` universel suit le régime commun ci-dessus ; sans
    # credential, l'exécution lève proprement.)
    # Sélection marketplace (ADR 0019/0050) : régime NOMINAL « non-sélectionné =
    # masqué ». Un connecteur en PAUSE ou non-installé masque ses tools. Le seed
    # de la 1re session d'un (sub, org) installe le socle `default_active` ∩ exposé
    # — VIDE depuis le 16/07 : un nouveau compte démarre SANS connecteurs installés,
    # l'agent guide depuis les tools spine + le catalogue injecté (bloc A). Les
    # pairs pré-0050 ont été backfillés avec leur visible d'alors (db._init).
    # Depuis peu : la baseline PLATEFORME est complétée par la baseline PROPRE à
    # l'org active (`org_store.get_org_default_connectors`, ex-« recommended »,
    # posée par `providers.recommend`) — un org_admin peut donc faire démarrer
    # SES nouveaux membres avec un socle non-vide, sans toucher au socle plateforme.
    # Fail-OPEN sur glitch (ergonomie, jamais une barrière : les gates call-time
    # restent) ; `oto_call` = échappatoire d'appel ponctuel d'un tool non listé
    # (ADR 0036).
    try:
        # Sans org active, rien n'est semé : la sentinelle `0` n'est plus une org
        # (ADR 0030 §8) et y poser une marque ou une ligne n'est rendu par aucune
        # lecture (#959).
        if active_org is not None and not connector_selection.is_seeded(sub, prof_org):
            org_defaults = set(org_store.get_org_default_connectors(active_org) or []) if active_org else set()
            # Un nom du kit absent du catalogue servi ne s'installe pour rien : journalisé
            # en erreur, une fois par (table, nom) et par processus, et semé tel quel —
            # ni écarté en silence, ni corrigé ici (#905).
            connector_orphelins.signaler("orgs.default_connectors", org_defaults)
            exposed_now = connector_activation.exposed_connectors(active_org)
            socle = providers.DEFAULT_ACTIVE_CONNECTORS & exposed_now
            # Le KIT n'est plus filtré par l'exposition (ADR 0050 §E2, oto#166) : un
            # connecteur que l'org a coupé APRÈS l'avoir mis au kit s'installe comme
            # les autres — masqué plus bas par le bloc d'activation tant que la coupure
            # dure, et visible seul à la réouverture. Il était ÉCARTÉ EN SILENCE (une
            # entrée sur 182 en production le 11/09). La garde d'exposition vaut pour
            # le geste qui AJOUTE au kit (`connectors.kit`), pas pour ce semis.
            # Provenance (§E7) : le socle prime sur le kit — un connecteur que la
            # plateforme installe d'office ne sort pas avec un retrait du kit.
            origins = {n: connector_selection.KIT for n in org_defaults}
            origins.update({n: connector_selection.SOCLE for n in socle})
            connector_selection.seed_active(sub, origins, prof_org)
        # Active = utilisable (22/10/2026, `connectors/usable.py`) : la sélection suit
        # les clés que le membre peut utiliser — réconciliée ICI, à la session, pour
        # tout membre et tout run hébergé, au plus une fois par TTL et à chaque
        # écriture du coffre qui l'atteint. Fail-INERTE et à part : un échec n'écrit
        # rien, journalise, et la boîte monte ce que la table porte déjà.
        if active_org is not None:
            try:
                connector_usable.run_if_due(sub, active_org, access.current_group(sub))
            except Exception as e:
                logger.warning("selection reconcile skipped for %s (fail-inert): %s", sub, e)
        _sel = connector_selection.list_selection(sub, prof_org)
        couches[COUCHE_SELECTION] = {
            n for n in all_names
            if (c := providers.connector_for_namespace(namespace_of(n))) is not None
            and _sel.get(c.name) != connector_selection.ACTIVE
        }
    except Exception as e:
        logger.warning("selection visibility skipped for %s (fail-open): %s", sub, e)
    # Surfaces BÊTA (2026-09-01) : réservées aux comptes à qui un admin a posé
    # l'option. Le nouvel univers de contenu part de vide et son contrat est
    # provisoire — le proposer à tous, c'est offrir à chaque agent une surface
    # qui ne trouve rien et une écriture dont l'utilisateur ignore la
    # destination.
    #
    # ⚠️ **Fail-CLOSED, à contre-courant des blocs ci-dessus.** Ils sont
    # fail-open parce qu'un hoquet de base ne doit pas priver quelqu'un de ses
    # outils : le pire y est une toolbox trop pauvre pendant une seconde. Ici le
    # pire est l'inverse — une surface non finie qui réapparaît à tout le monde
    # sur un glitch, sans que personne ne le voie. On masque donc en cas de
    # doute : ne pas proposer une bêta n'a jamais bloqué personne.
    try:
        if not access.has_option(sub, BETA_OPTION, org=active_org):
            couches[COUCHE_BETA] = all_names & BETA_TOOLS
    except Exception as e:
        logger.warning("beta visibility fail-CLOSED for %s: %s", sub, e)
        couches[COUCHE_BETA] = all_names & BETA_TOOLS
    # Outils hors de portée : masqués d'après l'AUTORISATION DÉCLARÉE, pas d'après
    # le nom. Visibilité seulement — l'autz reste appliquée à l'appel, ici comme avant.
    couches[COUCHE_HORS_DE_PORTEE] = _hors_de_portee_plateforme(
        all_names, role_plateforme, _roles_des_outils_permis(sub))
    # Garde anti-lockout STRUCTUREL (signal d’usage #213) : AUCUN bloc de gating ci-dessus
    # (connecteur/RBAC/sélection/admin) ne peut masquer un tool SPINE/protégé. Jusqu'ici
    # le spine n'était sauvé que parce que son namespace ne résolvait aucun connecteur
    # (effet de bord fragile : un connecteur déclarant `oto`/`data` aurait tout évincé).
    # Ici c'est explicite et robuste — source unique `is_protected`, appliquée à
    # CHAQUE couche : l'union reste ce qu'elle était, et aucun lecteur par couche ne
    # peut croire un outil protégé masqué.
    proteges = {n for n in all_names if is_protected(n)}
    return {nom: noms - proteges for nom, noms in couches.items()}


def _roles_des_outils_permis(sub: str) -> dict[str, str]:
    """Sous un jeton de délégation : le rôle plateforme qu'aura le porteur PENDANT l'appel
    de chacun des `oto_admin_*` que son agent permet (`verrou_org.outils_permis`). Sans
    lui, `tools/list` jugerait ces outils au rôle lu hors appel (`member` en `enforce`)
    et les masquerait — or un outil masqué est INJOIGNABLE (fastmcp filtre `get_tool`) :
    la liste de l'agent ne servirait jamais. Lu sans trace (`lecture_pour_outil`) : lister
    n'exerce rien. Vide hors délégation ; repli vide sur erreur (le masquage reste celui
    du rôle lu hors appel — fail-closed)."""
    from . import verrou_org
    try:
        permis = verrou_org.outils_permis()
        roles: dict[str, str] = {}
        for nom in permis:
            with verrou_org.lecture_pour_outil(nom):
                roles[nom] = access.get_user_role(sub)
        return roles
    except Exception as e:
        logger.warning("Cannot read the platform tools of the job for %s: %s", sub, e)
        return {}


def _hors_de_portee_plateforme(all_names: set[str], role_plateforme: str,
                               roles_par_outil: Optional[dict[str, str]] = None) -> set[str]:
    """Outils qu'AUCUN appel de ce user ne pourrait faire aboutir, d'après le plancher
    de rôle plateforme que leur autorisation DÉCLARE (`capabilities/_authz.py`).

    ⚠️ **Un nom ne porte pas un droit.** La règle d'avant masquait tout `oto_admin_*` à
    qui n'était pas SUPER admin — deux erreurs dans une ligne. `oto_admin_org_member`
    accorde `remove` à un `ORG_ADMIN_OF("org_id")` depuis juin et le dashboard s'en
    sert : un responsable d'organisation était renvoyé au dashboard pour un geste que
    son autorisation lui donne (#471). Et un opérateur plateforme `admin` — le palier
    pour lequel `PLATFORM_ADMIN` a été écrit — ne voyait aucun outil admin non plus.
    Le masquage bloque aussi l'APPEL (fastmcp filtre `get_tool`, pas seulement
    `list_tools`), donc l'outil n'était pas seulement discret : il était injoignable.

    ⚠️ Ce masquage est de la **gouvernance, pas une barrière** (ADR 0031) : la vraie
    barrière est l'autz de la capacité, appliquée de toute façon à l'appel. Le risque
    est donc asymétrique — montrer de trop ne donne aucun droit, masquer de trop rend
    un geste légitime introuvable. D'où la règle : ne masquer que ce qui est
    inatteignable PAR CONSTRUCTION, jamais ce qui dépend d'une cible (une org, une
    équipe, une ressource) que le handshake ne connaît pas.

    Repli par le NOM pour les outils écrits à la main :
    leur garde vit dans leur handler, rien n'est déclaré ici, donc rien n'est
    dérivable — le préfixe reste le seul indice, au cran `operator` (ce que ce
    handler-là vérifie). Le jour où ils deviennent des capacités, ils tombent sous la
    règle générale sans qu'on y pense."""
    from .capabilities import _authz
    from .capabilities.registry import CAPABILITIES

    plancher_par_tool = {c.mcp: _authz.platform_floor(c.authz)
                         for c in CAPABILITIES if c.mcp}
    return {
        n for n in all_names
        if not _authz.meets_platform_floor(
            plancher_par_tool.get(n, "operator" if n.startswith("oto_admin_") else None),
            (roles_par_outil or {}).get(n, role_plateforme))
    }


def _log_visibility_failure(quoi: str, sub: str, e: BaseException) -> None:
    """Journalise un échec de pose de visibilité au bon NIVEAU.

    Poser la visibilité, c'est pousser une notification `tools/list_changed` sur le
    stream de la session. Quand le client a déjà fermé (nos workers runner ferment le
    POST sitôt le corps lu), le push lève une déconnexion — **attendu, rien à corriger** :
    337 de ces warnings en 2 h le 15/08 (#352), aucun actionnable, et ils noyaient le
    reste du journal. Cette classe passe donc en `debug`.

    Toute AUTRE cause reste un `warning` : un échec de visibilité pour une vraie raison
    (registre, DB, bug) est un fait à voir — la session tourne alors avec une toolbox
    plus large que prévu."""
    if _is_client_disconnect(e):
        logger.debug("Failed to %s tool visibility for %s (client parti): %s", quoi, sub, e)
    else:
        logger.warning("Failed to %s tool visibility for %s: %s", quoi, sub, e)


async def apply_session_visibility(ctx, sub: str, *, reset: bool = False, org=_DERIVE_ORG) -> None:
    """Calcule la denylist de `(sub, org active)` et la pose sur la session `ctx`.

    `reset=False` (handshake) : pose seulement la denylist (comportement
    historique). `reset=True` (bascule à chaud) : remet d'abord tout visible
    (`reset_visibility`) pour effacer la denylist de l'ANCIENNE org, puis re-pose
    celle de la nouvelle — fastmcp émet `tools/list_changed` à la session.

    `org` : org de scope EXPLICITE (défaut = dérive de `current_org(sub)`, la
    maison). À passer pour une session de FLOTTE (#1058) : sa boîte doit suivre
    l'org de la MISSION (posée par le runner, `X-Oto-Org` à l'`initialize`), pas
    la maison mutable du compte porteur — une bascule de préférence dashboard ne
    doit jamais changer, en cours de route, la boîte d'une flotte en vol."""
    to_hide = await compute_hidden_tools(ctx, sub, org=org)
    if reset:
        try:
            await reset_visibility(ctx)
        # noqa: SILENT — fail-open par palier, déjà journalisé par _log_visibility_failure
        except Exception as e:
            _log_visibility_failure("reset", sub, e)
    if not to_hide:
        return
    try:
        await disable_components(ctx, names=to_hide, components={"tool"})
    # noqa: SILENT — fail-open par palier, déjà journalisé par _log_visibility_failure
    except Exception as e:
        _log_visibility_failure("apply", sub, e)
