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
import os

from fastmcp.server.transforms.visibility import disable_components, reset_visibility

from . import access, connector_activation, connector_selection, connectors, db, group_store, org_store
from .tool_visibility import (
    DEFAULT_HIDDEN_TOOLS,
    effective_disabled,
    is_default_hidden,
    is_grant_only,
    namespace_of,
)

logger = logging.getLogger(__name__)

# Backstop FAIL-CLOSED : noms grant-only / masqués-par-défaut vus lors d'un
# list_tools réussi. Si le listing échoue, on les réinjecte dans `all_names`
# pour qu'ils restent candidats au masquage (la denylist fastmcp les laisserait
# visibles sinon — défaut is_enabled=True). Caches process, alimentés au listing.
_KNOWN_GRANT_ONLY: set[str] = set()
_KNOWN_DEFAULT_HIDDEN: set[str] = set()

# Gate doux alpha (ADR 0013) : un compte non-'active' ne voit QUE ces tools —
# accepter une invitation + méta de visibilité (anti-lockout). Tout le reste est
# masqué tant que le compte est en waitlist.
ALPHA_GATE_ALLOWLIST: frozenset[str] = frozenset({
    "oto_accept_invite", "oto_list_my_tools", "oto_enable_tool", "oto_apply_preset",
})


def alpha_gate_enabled() -> bool:
    """Cran d'enforcement du gate doux (ADR 0013, barreau 4). Off par défaut :
    tant que le flag n'est pas posé, l'état d'accès n'a aucun effet de visibilité."""
    return os.environ.get("OTO_ALPHA_GATE_ENABLED", "").strip().lower() in ("1", "true", "yes")


async def compute_hidden_tools(ctx, sub: str) -> set[str]:
    """Ensemble effectif des tools à masquer pour `(sub, org active)`.

    Profil de visibilité = (sub, org active) ; 0 = perso/global (ADR 0015). Lit
    l'org active à CHAQUE appel → après `set_active_org`, recalcule pour la
    nouvelle org. `ctx` = `Context` fastmcp (pour `ctx.fastmcp.list_tools`)."""
    try:
        # Les toggles/presets sont scopés par org → on lit ceux de l'org active.
        active_org = access.current_org(sub)
        prof_org = active_org or 0
        disabled = set(db.list_user_disabled_tools(sub, prof_org))
        enabled_override = set(db.list_user_enabled_tools(sub, prof_org))
        # Union grants per-user + entitlements de l'org active (source unique).
        granted = access.granted_namespaces_for(sub)
        is_admin = access.is_super_admin(sub)
        # Baseline de toolset (preset de visibilité). Cascade ADR 0015/0012 :
        # le GROUPE actif raffine l'ORG active. Le chef d'équipe a priorité ; à
        # défaut, la baseline curée par l'org_admin pour ses membres. None = pas
        # de baseline (visibilité par défaut, ex. profil perso sans org active).
        group_baseline = None
        active_group = group_store.get_active_group(sub)
        if active_group is not None:
            gt = group_store.get_group_default_tools(active_group)
            if gt is not None:
                group_baseline = frozenset(gt)
        if group_baseline is None and active_org is not None:
            ot = org_store.get_org_default_tools(active_org)
            if ot is not None:
                group_baseline = frozenset(ot)
    except Exception as e:
        # FAIL-CLOSED : sur erreur DB, ne PAS révéler les namespaces grant-only.
        # granted=∅ + is_admin=False → is_tool_visible masque tout grant-only
        # (la visibilité est ergonomie, mais grant-only est une vraie barrière).
        logger.warning("Cannot read tool visibility for %s (fail-closed): %s", sub, e)
        disabled, enabled_override, granted, is_admin = set(), set(), frozenset(), False
        group_baseline = None
        active_org, prof_org = None, 0
    try:
        all_tools = await ctx.fastmcp.list_tools(run_middleware=False)
        all_names = {t.name for t in all_tools}
        _KNOWN_GRANT_ONLY.update(n for n in all_names if is_grant_only(n))
        _KNOWN_DEFAULT_HIDDEN.update(n for n in all_names if is_default_hidden(n))
    except Exception as e:
        logger.warning("Cannot list tools for %s: %s", sub, e)
        # repli FAIL-CLOSED : disabled explicites + masqués connus + tous les
        # grant-only déjà vus (sinon ils resteraient visibles, denylist
        # incomplète). Les noms inconnus de ce process restent couverts par
        # le backstop call-time (access.require_namespace).
        all_names = (
            disabled | DEFAULT_HIDDEN_TOOLS | _KNOWN_DEFAULT_HIDDEN | _KNOWN_GRANT_ONLY
        )
    to_hide = effective_disabled(
        all_names, disabled, enabled_override, granted, is_admin, group_baseline)
    # Activation (ADR 0011) : masque les tools d'un connecteur non activé pour
    # l'org de la session — à chaud, per-org. Fail-OPEN (gouvernance d'exposition,
    # pas une barrière de sécurité ; le grant-only reste fail-closed ci-dessus).
    # Les tools plateforme (scout/oto/data/doctrine) n'ont pas de connecteur au
    # registre → jamais gatés.
    try:
        exposed = connector_activation.exposed_connectors(active_org)
        to_hide |= {
            n for n in all_names
            if (c := connectors.connector_for_namespace(namespace_of(n))) is not None
            and c.name not in exposed
        }
    except Exception as e:
        logger.warning("activation visibility skipped for %s (fail-open): %s", sub, e)
    # Sélection marketplace (ADR 0019, B5) : masque les tools d'un connecteur que
    # le membre a mis en PAUSE (state='paused'). `not_selected` reste visible à ce
    # barreau (rétro-compatible ; le flip du défaut « non-sélectionné = masqué » =
    # B6). Derrière flag `OTO_CONNECTOR_SELECTION_ENABLED`, fail-OPEN.
    if os.environ.get("OTO_CONNECTOR_SELECTION_ENABLED"):
        try:
            _strict = bool(os.environ.get("OTO_CONNECTOR_SELECTION_STRICT"))
            # B6 : seed lazy à la 1re session sous le régime strict — pré-remplit la
            # sélection avec l'exposé courant (le membre garde tout ; seuls les
            # connecteurs exposés APRÈS le seed restent à installer depuis la library).
            if _strict and not connector_selection.is_seeded(sub, prof_org):
                connector_selection.seed_active(
                    sub, connector_activation.exposed_connectors(active_org), prof_org)
            _sel = connector_selection.list_selection(sub, prof_org)
            # B5 : un connecteur en PAUSE masque ses outils. B6 (strict) : un
            # connecteur NON-sélectionné les masque aussi (`_st is None`).
            to_hide |= {
                n for n in all_names
                if (c := connectors.connector_for_namespace(namespace_of(n))) is not None
                and ((_st := _sel.get(c.name)) == connector_selection.PAUSED
                     or (_strict and _st is None))
            }
        except Exception as e:
            logger.warning("selection visibility skipped for %s (fail-open): %s", sub, e)
    # Tools réservés au platform admin (`oto_admin_*`) : masqués aux non-admins.
    # Inutiles à un user normal (l'autz les refuse à l'appel) → ils ne font
    # qu'alourdir le contexte. Visibilité seulement ; l'autz PLATFORM_ADMIN reste
    # enforced au call-time (jamais une barrière ici).
    if not is_admin:
        to_hide |= {n for n in all_names if n.startswith("oto_admin_")}
    # Gate doux alpha (ADR 0013, barreau 4) : si le flag est posé, un compte
    # non-'active' (waitlist/blocked) ne voit que l'allowlist d'onboarding.
    # Fail-OPEN (gouvernance d'accès produit) : sur glitch DB on n'enferme pas.
    if alpha_gate_enabled():
        try:
            status = (db.get_user(sub) or {}).get("access_status")
        except Exception as e:
            logger.warning("alpha gate skipped for %s (fail-open): %s", sub, e)
            status = "active"
        if status not in (None, "active"):
            to_hide |= (all_names - ALPHA_GATE_ALLOWLIST)
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
