"""Visibilité des tools : masquage par défaut, namespaces grant-only, règle effective.

Deux niveaux de masquage :

1. **Masqués par défaut** mais **self-activables** (`oto_enable_tool`) — simple
   découvrabilité, pas un contrôle d'accès : pour des surfaces verbeuses/spécifiques
   sans enjeu de sécurité. Deux grains : DEFAULT_HIDDEN_TOOLS (noms individuels)
   et DEFAULT_HIDDEN_NAMESPACES (namespaces entiers, DÉRIVÉ du registre — champ
   `default_hidden` des connecteurs, ex. attio).

2. **ADMIN_GRANT_ONLY_NAMESPACES** — namespaces sensibles (mission/prod-client).
   Deny-by-default : un user non-admin **ne peut PAS** s'auto-activer ces tools
   (`oto_enable_tool` refuse). Seul un **grant admin** (`user_namespace_grants`,
   posé via `oto_admin_namespace_access`) les rend visibles + appelables. Un admin
   les voit comme masqués-par-défaut (self-activables). C'est la vraie barrière
   d'autorisation, par opposition au masquage cosmétique de niveau 1.

Modèle de visibilité effective :
- grant-only : visible si (admin et self-activé) ou (non-admin et namespace granté
  et non self-désactivé). `enabled_override` ne révèle JAMAIS un grant-only à un
  non-admin — pas d'auto-escalade.
- sinon : visible sauf si désactivé ou masqué par défaut ; `enabled_override` prime
  pour rendre visible un masqué-par-défaut.
"""
from __future__ import annotations

from . import connectors

# Namespaces sensibles : accès sur grant admin explicite uniquement. DÉRIVÉ du
# registre (connecteurs `availability=platform_granted`) — gocardless + mm.
ADMIN_GRANT_ONLY_NAMESPACES = connectors.ADMIN_GRANT_ONLY_NAMESPACES

# Masqués par défaut mais self-activables (découvrabilité, pas sécurité).
# `email_send` : envoi d'email per-org. Autz DYNAMIQUE dans le handler (membre de
# l'org pour une adresse déclarée de l'org ; super_admin pour le repli marque
# oto@otomata.tech) — masqué ici pour ne pas encombrer la toolbox des orgs sans
# adresse configurée. La vraie barrière reste le check de rôle, pas ce masquage.
# `fr_egapro_declaration` : source de niche (index égalité F-H par SIREN, surtout
# utile en qualif sociale type Mūcho) — masquée pour ne pas charger la toolbox `fr`
# par défaut ; activable à la demande (oto_enable_tool fr_egapro_declaration).
DEFAULT_HIDDEN_TOOLS: frozenset[str] = frozenset({"email_send", "fr_egapro_declaration"})
DEFAULT_HIDDEN_NAMESPACES = connectors.DEFAULT_HIDDEN_NAMESPACES

# Méta-tools TOUJOURS visibles (anti-lockout) : sans eux l'utilisateur ne peut
# plus se déverrouiller (lister/activer/appliquer un preset) — plus l'accueil
# `oto_onboarding` et l'identité `oto_whoami`, qui doivent rester atteignables au
# démarrage d'un compte même sous un preset/baseline restrictif. Une baseline (org
# ADR 0015 ou groupe ADR 0012) ne doit JAMAIS les masquer. SOURCE UNIQUE : meta.py
# et api_routes en dérivent.
PROTECTED_TOOLS: frozenset[str] = frozenset(
    {"oto_list_my_tools", "oto_enable_tool", "oto_apply_preset", "oto_onboarding",
     "oto_whoami"})


def namespace_of(name: str) -> str:
    """Namespace d'un tool = préfixe avant le premier `_` (ex. `mm_company` → `mm`)."""
    return name.split("_", 1)[0]


def is_default_hidden(name: str) -> bool:
    return name in DEFAULT_HIDDEN_TOOLS or namespace_of(name) in DEFAULT_HIDDEN_NAMESPACES


def is_tool_visible(
    name: str,
    disabled: set[str],
    enabled_override: set[str],
    group_baseline: "frozenset[str] | None" = None,
) -> bool:
    """Règle de visibilité effective pour un tool donné.

    `group_baseline` (ADR 0012) = le preset de toolset que le chef du groupe ACTIF
    a posé pour son équipe. None = pas de baseline (visibilité par défaut). Quand
    une baseline existe, elle décide la visibilité par défaut des tools NORMAUX
    (dans la baseline → visible, même un masqué-par-défaut ; hors baseline →
    masqué) — mais les overrides perso priment."""
    if name in PROTECTED_TOOLS:
        return True  # anti-lockout : jamais masqué (ni baseline, ni default-hidden)
    if name in enabled_override:
        return True
    if name in disabled:
        return False
    if group_baseline is not None:
        # La baseline du groupe gouverne la visibilité par défaut de l'équipe.
        return name in group_baseline
    if is_default_hidden(name):
        return False
    return True


def effective_disabled(
    all_names: set[str],
    disabled: set[str],
    enabled_override: set[str],
    group_baseline: "frozenset[str] | None" = None,
) -> set[str]:
    """Ensemble des tools à masquer pour cet user, parmi `all_names`."""
    return {
        n
        for n in all_names
        if not is_tool_visible(n, disabled, enabled_override, group_baseline)
    }
