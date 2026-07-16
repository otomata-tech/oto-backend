"""Calcul + application de la visibilité des tools d'une session MCP.

Extrait de `middleware.UserDisabledToolsMiddleware` (ADR 0009/0011/0015) pour
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

from fastmcp.server.transforms.visibility import disable_components, reset_visibility

from . import (access, connector_activation, connector_selection, connectors,
               credentials_store, db, providers)
from .tool_visibility import (
    DEFAULT_HIDDEN_TOOLS,
    effective_disabled,
    namespace_of,
)

logger = logging.getLogger(__name__)


async def compute_hidden_tools(ctx, sub: str) -> set[str]:
    """Ensemble effectif des tools à masquer pour `(sub, org active)`.

    Profil de visibilité = (sub, org active) ; 0 = perso/global (ADR 0015). Lit
    l'org active à CHAQUE appel → après `set_active_org`, recalcule pour la
    nouvelle org. `ctx` = `Context` fastmcp (pour `ctx.fastmcp.list_tools`)."""
    try:
        # Les toggles perso sont scopés par org → on lit ceux de l'org active.
        active_org = access.current_org(sub)
        prof_org = active_org or 0
        disabled = set(db.list_user_disabled_tools(sub, prof_org))
        enabled_override = set(db.list_user_enabled_tools(sub, prof_org))
        is_admin = access.is_super_admin(sub)
    except Exception as e:
        # Sur erreur DB : repli neutre (rien de désactivé). La sécurité d'accès ne
        # dépend PAS de cette visibilité — elle est gardée au call-time (credential
        # + require_connector_access ADR 0025 + activation + remote credential).
        logger.warning("Cannot read tool visibility for %s: %s", sub, e)
        disabled, enabled_override, is_admin = set(), set(), False
        active_org, prof_org = None, 0
    try:
        all_tools = await ctx.fastmcp.list_tools(run_middleware=False)
        all_names = {t.name for t in all_tools}
    except Exception as e:
        logger.warning("Cannot list tools for %s: %s", sub, e)
        # repli FAIL-CLOSED : disabled explicites + masqués-par-défaut
        # (sinon ils resteraient visibles, denylist incomplète).
        all_names = disabled | DEFAULT_HIDDEN_TOOLS
    to_hide = effective_disabled(all_names, disabled, enabled_override)
    # Activation (ADR 0011) : masque les tools d'un connecteur non activé pour
    # l'org de la session — à chaud, per-org. Fail-OPEN (gouvernance d'exposition,
    # pas une barrière de sécurité ; le grant-only reste fail-closed ci-dessus).
    # Les tools plateforme (oto/data/doctrine) n'ont pas de connecteur au
    # registre → jamais gatés.
    try:
        exposed = connector_activation.exposed_connectors(active_org)
        # Tier ÉQUIPE (ADR 0012, restrict-only) : l'équipe active peut COUPER un
        # connecteur pour ses membres — on retranche ses coupures de l'exposé (jamais
        # d'ajout : invariant monotone). Même régime fail-open que l'org.
        active_group = access.current_group(sub)
        if active_group is not None:
            exposed = connector_activation.effective_for_group(
                exposed, connector_activation.group_cut_connectors(active_group))
        to_hide |= {
            n for n in all_names
            if (c := connectors.connector_for_namespace(namespace_of(n))) is not None
            and c.name not in exposed
        }
    except Exception as e:
        logger.warning("activation visibility skipped for %s (fail-open): %s", sub, e)
    # (La règle dédiée « bridges remote per-namespace » a été retirée — ADR 0034 B4 :
    # le connecteur `bridge` universel suit le régime commun ci-dessus ; sans
    # credential, l'exécution lève proprement.)
    # RBAC connecteur interne à l'org (ADR 0025) : un connecteur RESTREINT dans
    # l'org active est masqué pour un membre non autorisé (département/user). Le
    # backstop DUR est au call-time (`resolve_credential` → `require_connector_access`) ;
    # ici = ergonomie (best-effort, fail-OPEN sur glitch — le call-time garantit).
    # Seam unique `rbac_denied_connectors` (escalade super_admin + org_admin incluse).
    try:
        deny = access.rbac_denied_connectors(sub, active_org)
        if deny:
            to_hide |= {
                n for n in all_names
                if (c := connectors.connector_for_namespace(namespace_of(n))) is not None
                and c.name in deny
            }
    except Exception as e:
        logger.warning("org connector RBAC visibility skipped for %s (fail-open): %s", sub, e)
    # RBAC connecteur au grain ÉQUIPE (ADR 0012 B2) : l'équipe ACTIVE peut réserver un
    # connecteur à un sous-ensemble de ses membres — masqué pour les autres (narrowing
    # de l'org). Backstop DUR au call-time (`require_connector_access`) ; ici ergonomie
    # (best-effort, fail-OPEN).
    try:
        g_deny = access.group_rbac_denied_connectors(sub, access.current_group(sub))
        if g_deny:
            to_hide |= {
                n for n in all_names
                if (c := connectors.connector_for_namespace(namespace_of(n))) is not None
                and c.name in g_deny
            }
    except Exception as e:
        logger.warning("group connector RBAC visibility skipped for %s (fail-open): %s", sub, e)
    # Sélection marketplace (ADR 0019/0050) : régime NOMINAL « non-sélectionné =
    # masqué ». Un connecteur en PAUSE ou non-installé masque ses tools. Le seed
    # de la 1re session d'un (sub, org) installe le socle `default_active` ∩ exposé
    # — VIDE depuis le 16/07 : un nouveau compte démarre SANS connecteurs installés,
    # l'agent guide depuis les tools spine + le catalogue injecté (bloc A). Les
    # pairs pré-0050 ont été backfillés avec leur visible d'alors (db._init).
    # Fail-OPEN sur glitch (ergonomie, jamais une barrière : les gates call-time
    # restent) ; `oto_call` = échappatoire d'appel ponctuel d'un tool non listé
    # (ADR 0036).
    try:
        if not connector_selection.is_seeded(sub, prof_org):
            connector_selection.seed_active(
                sub,
                providers.DEFAULT_ACTIVE_CONNECTORS
                & connector_activation.exposed_connectors(active_org),
                prof_org)
        _sel = connector_selection.list_selection(sub, prof_org)
        to_hide |= {
            n for n in all_names
            if (c := connectors.connector_for_namespace(namespace_of(n))) is not None
            and _sel.get(c.name) != connector_selection.ACTIVE
        }
    except Exception as e:
        logger.warning("selection visibility skipped for %s (fail-open): %s", sub, e)
    # Tools réservés au platform admin (`oto_admin_*`) : masqués aux non-admins.
    # Inutiles à un user normal (l'autz les refuse à l'appel) → ils ne font
    # qu'alourdir le contexte. Visibilité seulement ; l'autz PLATFORM_ADMIN reste
    # enforced au call-time (jamais une barrière ici).
    if not is_admin:
        to_hide |= {n for n in all_names if n.startswith("oto_admin_")}
    return to_hide


async def apply_session_visibility(ctx, sub: str, *, reset: bool = False) -> None:
    """Calcule la denylist de `(sub, org active)` et la pose sur la session `ctx`.

    `reset=False` (handshake) : pose seulement la denylist (comportement
    historique). `reset=True` (bascule à chaud) : remet d'abord tout visible
    (`reset_visibility`) pour effacer la denylist de l'ANCIENNE org, puis re-pose
    celle de la nouvelle — fastmcp émet `tools/list_changed` à la session."""
    to_hide = await compute_hidden_tools(ctx, sub)
    if reset:
        try:
            await reset_visibility(ctx)
        except Exception as e:
            logger.warning("Failed to reset tool visibility for %s: %s", sub, e)
    if not to_hide:
        return
    try:
        await disable_components(ctx, names=to_hide, components={"tool"})
    except Exception as e:
        logger.warning("Failed to apply tool visibility for %s: %s", sub, e)
