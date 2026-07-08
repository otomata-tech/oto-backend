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

from . import (access, connector_activation, connector_selection, connectors,
               credentials_store, db)
from .tool_visibility import (
    DEFAULT_HIDDEN_TOOLS,
    effective_disabled,
    is_default_hidden,
    namespace_of,
)

logger = logging.getLogger(__name__)

# Backstop FAIL-CLOSED : noms masqués-par-défaut vus lors d'un list_tools réussi.
# Si le listing échoue, on les réinjecte dans `all_names` pour qu'ils restent
# candidats au masquage (la denylist fastmcp les laisserait visibles sinon —
# défaut is_enabled=True). Cache process, alimenté au listing.
_KNOWN_DEFAULT_HIDDEN: set[str] = set()

# Gate doux alpha (ADR 0013) : un compte non-'active' ne voit QUE ces tools —
# accepter une invitation + méta de visibilité (anti-lockout). Tout le reste est
# masqué tant que le compte est en waitlist.
ALPHA_GATE_ALLOWLIST: frozenset[str] = frozenset({
    "oto_accept_invite", "oto_list_my_tools", "oto_enable_tool",
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
        _KNOWN_DEFAULT_HIDDEN.update(n for n in all_names if is_default_hidden(n))
    except Exception as e:
        logger.warning("Cannot list tools for %s: %s", sub, e)
        # repli FAIL-CLOSED : disabled explicites + masqués-par-défaut connus
        # (sinon ils resteraient visibles, denylist incomplète).
        all_names = disabled | DEFAULT_HIDDEN_TOOLS | _KNOWN_DEFAULT_HIDDEN
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
    try:
        if active_org is not None:
            restricted = db.org_restricted_connectors(active_org)
            if restricted:
                deny = restricted - db.member_allowed_connectors(sub, active_org)
                if deny:
                    to_hide |= {
                        n for n in all_names
                        if (c := connectors.connector_for_namespace(namespace_of(n))) is not None
                        and c.name in deny
                    }
    except Exception as e:
        logger.warning("org connector RBAC visibility skipped for %s (fail-open): %s", sub, e)
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
            # Un connecteur explicitement sélectionné 'active' DÉMASQUE ses outils
            # masqués-par-défaut : le choix marketplace explicite (ADR 0019) prime sur
            # l'anti-encombrement legacy `default_hidden` (ADR 0011) — redondant/
            # contradictoire en régime de sélection (`not_selected` déclutre déjà). Sans
            # ça, poser une carte sur « active » n'affiche rien pour un connecteur
            # default_hidden (pennylaneged/brevo). On respecte un toggle-off perso
            # explicite (`disabled`) et on ne retouche jamais paused/not-selected
            # (masqués au-dessus, jamais dans `_active_sel`).
            _active_sel = {
                name for name, st in _sel.items() if st == connector_selection.ACTIVE
            }
            if _active_sel:
                to_hide -= {
                    n for n in all_names
                    if is_default_hidden(n) and n not in disabled
                    and (c := connectors.connector_for_namespace(namespace_of(n))) is not None
                    and c.name in _active_sel
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
