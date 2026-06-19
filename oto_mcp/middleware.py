"""Middlewares FastMCP — application des préférences user au boot de session."""
from __future__ import annotations

import logging
import os

from fastmcp.server.middleware import Middleware
from fastmcp.server.transforms.visibility import disable_components

from . import access, connector_activation, connector_selection, connectors, db, group_store, org_store
from .auth_hooks import current_user_sub_from_token
from .tool_visibility import (
    DEFAULT_HIDDEN_TOOLS,
    effective_disabled,
    is_default_hidden,
    is_grant_only,
    namespace_of,
)

logger = logging.getLogger(__name__)

# Noms de tools grant-only vus lors d'un list_tools réussi. Sert de repli
# FAIL-CLOSED : si list_tools échoue au handshake, on réinjecte ces noms dans
# `all_names` pour qu'ils restent candidats au masquage (sinon la denylist
# fastmcp les laisserait visibles — défaut is_enabled=True). Backstop de
# listing ; l'autorisation d'APPEL est garantie indépendamment par
# access.require_namespace dans les tools à credential serveur (cf. mm).
_KNOWN_GRANT_ONLY: set[str] = set()

# Même repli pour les masqués-par-défaut par NAMESPACE (ex. attio) : leurs noms
# ne sont pas connaissables statiquement, on mémorise ceux vus au listing.
_KNOWN_DEFAULT_HIDDEN: set[str] = set()

# Gate doux alpha (ADR 0013) : un compte non-'active' ne voit QUE ces tools —
# accepter une invitation (réclamer son accès) + méta de visibilité (anti-lockout
# du middleware). Tout le reste est masqué tant que le compte est en waitlist.
ALPHA_GATE_ALLOWLIST: frozenset[str] = frozenset({
    "oto_accept_invite", "oto_list_my_tools", "oto_enable_tool", "oto_apply_preset",
})


def alpha_gate_enabled() -> bool:
    """Cran d'enforcement du gate doux (ADR 0013, barreau 4). Off par défaut :
    tant que le flag n'est pas posé, l'état d'accès n'a aucun effet de visibilité."""
    return os.environ.get("OTO_ALPHA_GATE_ENABLED", "").strip().lower() in ("1", "true", "yes")


class UserDisabledToolsMiddleware(Middleware):
    """Applique la visibilité des tools du user à sa session MCP.

    Au handshake `initialize`, pour le `sub` JWT courant, on calcule
    l'ensemble effectif des tools à masquer = `user_disabled_tools` ∪
    (tools masqués par défaut non explicitement activés). On pose une
    visibility rule session-scopée via `disable_components`. Le reste —
    filtrage `tools/list`, blocage `tools/call`, émission de
    `tools/list_changed` — est géré nativement par fastmcp.

    Pas de sub identifiable (stdio local, discovery non-authentifié) → on ne
    filtre rien : la machine du dev a accès complet, le masquage par défaut
    ne concerne que la surface multi-user authentifiée.
    """

    async def on_initialize(self, context, call_next):
        result = await call_next(context)
        try:
            sub = current_user_sub_from_token()
        except Exception:
            sub = None
        if not sub:
            return result
        ctx = context.fastmcp_context
        if ctx is None:
            logger.warning("fastmcp_context is None at on_initialize for sub=%s", sub)
            return result
        try:
            # Profil de visibilité = (sub, org active) ; 0 = perso/global (ADR 0015).
            # Les toggles/presets sont scopés par org → on lit ceux de l'org active.
            active_org = org_store.get_active_org(sub)
            prof_org = active_org or 0
            disabled = set(db.list_user_disabled_tools(sub, prof_org))
            enabled_override = set(db.list_user_enabled_tools(sub, prof_org))
            # Union grants per-user + entitlements de l'org active (source unique).
            granted = access.granted_namespaces_for(sub)
            is_admin = access.is_super_admin(sub)
            # Baseline de toolset (preset de visibilité). Cascade ADR 0015/0012 :
            # le GROUPE actif raffine l'ORG active. Le chef d'équipe a priorité ; à
            # défaut, la baseline curée par l'org_admin pour ses membres (le toolset
            # épuré qu'ils voient quand cette org est active). None = pas de baseline
            # (visibilité par défaut, ex. profil perso sans org active).
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
        # l'org de la session — à chaud, per-org (plus de gate au chargement).
        # Fail-OPEN (gouvernance d'exposition, pas une barrière de sécurité ; le
        # grant-only reste fail-closed ci-dessus). Les tools plateforme (scout/oto/
        # data/doctrine) n'ont pas de connecteur au registre → jamais gatés.
        try:
            exposed = connector_activation.exposed_connectors(org_store.get_active_org(sub))
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
        # B6). Derrière flag `OTO_CONNECTOR_SELECTION_ENABLED`, fail-OPEN (gouvernance
        # d'exposition ; grant-only/PROTECTED jamais touchés — ils n'ont pas de
        # connecteur au registre, donc connector_for_namespace renvoie None).
        if os.environ.get("OTO_CONNECTOR_SELECTION_ENABLED"):
            try:
                _sel = connector_selection.list_selection(sub, prof_org)
                _paused = {nm for nm, st in _sel.items() if st == connector_selection.PAUSED}
                if _paused:
                    to_hide |= {
                        n for n in all_names
                        if (c := connectors.connector_for_namespace(namespace_of(n))) is not None
                        and c.name in _paused
                    }
            except Exception as e:
                logger.warning("selection visibility skipped for %s (fail-open): %s", sub, e)
        # Tools réservés au platform admin (`oto_admin_*`) : masqués aux non-admins.
        # Inutiles à un user normal (l'autz les refuse à l'appel) → ils ne font
        # qu'alourdir le contexte de TOUT LE MONDE. Visibilité seulement ; l'autz
        # PLATFORM_ADMIN reste enforced au call-time (jamais une barrière ici).
        if not is_admin:
            to_hide |= {n for n in all_names if n.startswith("oto_admin_")}
        # Gate doux alpha (ADR 0013, barreau 4) : si le flag est posé, un compte
        # non-'active' (waitlist/blocked) ne voit que l'allowlist d'onboarding.
        # Fail-OPEN (gouvernance d'accès produit, pas une barrière de sécurité) :
        # sur glitch DB on n'enferme pas. Sans le flag, no-op total.
        if alpha_gate_enabled():
            try:
                status = (db.get_user(sub) or {}).get("access_status")
            except Exception as e:
                logger.warning("alpha gate skipped for %s (fail-open): %s", sub, e)
                status = "active"
            if status not in (None, "active"):
                to_hide |= (all_names - ALPHA_GATE_ALLOWLIST)
        if not to_hide:
            return result
        try:
            await disable_components(ctx, names=to_hide, components={"tool"})
        except Exception as e:
            logger.warning("Failed to apply tool visibility for %s: %s", sub, e)
        return result
