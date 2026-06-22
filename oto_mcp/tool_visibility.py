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
   posé via `oto_admin_grant_namespace`) les rend visibles + appelables. Un admin
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
# `email_send` : outil d'envoi sous l'identité de marque, gaté super_admin dans son
# handler — masqué ici pour ne pas encombrer la toolbox des comptes normaux (la
# vraie barrière reste le check de rôle, pas ce masquage cosmétique).
DEFAULT_HIDDEN_TOOLS: frozenset[str] = frozenset({"email_send"})
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


# Namespaces grant-only découverts au RUNTIME (sans entrée registre) : les
# connecteurs remote data-driven (ADR 0003/0011), remplis au boot par
# tools/remote.py. Complète ADMIN_GRANT_ONLY_NAMESPACES (registre).
_RUNTIME_GRANT_ONLY: set[str] = set()


def register_runtime_grant_only(namespaces) -> None:
    """Marque des namespaces grant-only au boot (deny-by-default) sans entrée
    registre. Idempotent. Utilisé par les connecteurs remote découverts de la donnée."""
    _RUNTIME_GRANT_ONLY.update(namespaces)


def is_grant_only(name: str) -> bool:
    ns = namespace_of(name)
    return ns in ADMIN_GRANT_ONLY_NAMESPACES or ns in _RUNTIME_GRANT_ONLY


def is_entitled(
    name: str,
    granted_namespaces: frozenset[str] = frozenset(),
    is_admin: bool = False,
) -> bool:
    """L'user a-t-il le DROIT de voir ce tool (hors préférence d'affichage) ?

    Un grant-only exige admin ou grant de namespace ; tout le reste est de droit.
    Utilisé pour empêcher un preset de révéler un grant-only non autorisé.
    """
    if is_grant_only(name):
        return is_admin or namespace_of(name) in granted_namespaces
    return True


def is_tool_visible(
    name: str,
    disabled: set[str],
    enabled_override: set[str],
    granted_namespaces: frozenset[str] = frozenset(),
    is_admin: bool = False,
    group_baseline: "frozenset[str] | None" = None,
) -> bool:
    """Règle de visibilité effective pour un tool donné.

    `group_baseline` (ADR 0012) = le preset de toolset que le chef du groupe ACTIF
    a posé pour son équipe. None = pas de baseline (visibilité par défaut). Quand
    une baseline existe, elle décide la visibilité par défaut des tools NORMAUX
    (dans la baseline → visible, même un masqué-par-défaut ; hors baseline →
    masqué) — mais les overrides perso priment, et elle ne touche JAMAIS les
    grant-only (barrière de sécurité distincte, anti-escalade)."""
    if name in PROTECTED_TOOLS:
        return True  # anti-lockout : jamais masqué (ni baseline, ni default-hidden)
    if is_grant_only(name):
        # L'admin HÉRITE de la visibilité des namespaces entitled de son org
        # active (comme un user normal entitled). Sinon l'admin qui veut SE
        # SERVIR d'un connecteur grant-only (ex. memento fédéré, 51 outils)
        # devrait les activer un par un. Les namespaces NON entitled restent
        # masqués + opt-in manuel pour l'admin (anti-encombrement de sa vue sur
        # TOUS les connecteurs clients de la plateforme).
        if namespace_of(name) in granted_namespaces:
            return name not in disabled
        if is_admin:
            return name in enabled_override
        # Non-admin sans grant : invisible, pas d'auto-activation.
        return False
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
    granted_namespaces: frozenset[str] = frozenset(),
    is_admin: bool = False,
    group_baseline: "frozenset[str] | None" = None,
) -> set[str]:
    """Ensemble des tools à masquer pour cet user, parmi `all_names`."""
    return {
        n
        for n in all_names
        if not is_tool_visible(
            n, disabled, enabled_override, granted_namespaces, is_admin, group_baseline)
    }
