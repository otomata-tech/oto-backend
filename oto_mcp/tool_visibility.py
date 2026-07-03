"""Visibilité des tools : masquage par défaut + règle de visibilité effective.

**Masqués par défaut** mais **self-activables** (`oto_enable_tool`) — simple
découvrabilité, pas un contrôle d'accès : pour des surfaces verbeuses/spécifiques
sans enjeu de sécurité. Deux grains : DEFAULT_HIDDEN_TOOLS (noms individuels) et
DEFAULT_HIDDEN_NAMESPACES (namespaces entiers, DÉRIVÉ du registre — champ
`default_hidden` des connecteurs, ex. attio).

Modèle de visibilité effective : un tool est visible sauf s'il est désactivé
(toggle perso) ou masqué par défaut ; `enabled_override` prime pour rendre visible
un masqué-par-défaut. La gouvernance d'accès (activation org, RBAC connecteur
ADR 0025, credential) est appliquée AILLEURS — la visibilité n'est PAS une barrière
de sécurité (ADR 0031).
"""
from __future__ import annotations

from . import connectors

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
# plus se déverrouiller (lister/activer un tool) — plus l'identité `oto_whoami`
# et la fiche `oto_profile`, qui doivent rester atteignables au démarrage d'un
# compte même sous une visibilité restrictive. SOURCE UNIQUE : meta.py et
# api_routes en dérivent.
PROTECTED_TOOLS: frozenset[str] = frozenset(
    {"oto_list_my_tools", "oto_enable_tool", "oto_profile",
     "oto_whoami",
     # Dispatch universel (ADR 0036) : `oto_call` matérialise à la demande un outil
     # NON listé (FOD, connecteur non activé…) sans l'exposer durablement — il DOIT
     # rester atteignable même sous visibilité restrictive, sinon le catalogue latent
     # est inaccessible. `oto_tool_schema` = son handoff de schéma (même raison).
     "oto_call", "oto_tool_schema",
     # Échappatoires de CONTEXTE — jamais masquables (ni toggle perso ni
     # default-hidden). Un user dont `oto_use_org` est caché ne peut plus changer
     # d'org → lock-out, son client rappelle le tool en boucle → "Unknown tool".
     # Vécu Sentry 2026-06-30 (x50 sur 1 user après l'abolition du perso).
     "oto_use_org", "oto_clear_org", "oto_list_orgs",
     "oto_use_group", "oto_clear_group",
     # Boucle d'usage (ADR 0017) — les instructions plateforme MANDATENT leur
     # emploi systématique (signaler un gap, encadrer un run) : un toggle qui les
     # masque rend la doctrine inapplicable et le gap invisible. Jamais
     # désactivables ni masquables.
     "feedback", "run_start", "run_finish",
     # Famille projet (ADR 0032) — même raison : le bloc C injecte « Projets
     # récents » et les instructions mandatent « travaille dans un projet »
     # (oto_use_project).
     "oto_project", "oto_use_project", "oto_clear_project"})


# Testables depuis le dashboard (bouton « tester » de la fiche connecteur) :
# l'exécution est RÉELLE, déclenchée par un humain via REST → bornée aux
# connecteurs open-data en LECTURE SEULE (aucun effet de bord, aucune mutation,
# pas de credential BYO requis). Un « test » ne doit JAMAIS envoyer un email,
# écrire une donnée ou poster un message. FOD (données publiques France) est le
# cœur de cible. Étendre = ajouter un namespace read-only ici (source unique).
TESTABLE_NAMESPACES: frozenset[str] = frozenset(
    {"fr", "foncier", "urba", "sante", "frenchtech", "culture", "infosec"})


def namespace_of(name: str) -> str:
    """Namespace d'un tool = préfixe avant le premier `_` (ex. `mm_company` → `mm`)."""
    return name.split("_", 1)[0]


def is_testable(name: str) -> bool:
    """Un tool est testable depuis le dashboard s'il appartient à un namespace
    open-data en lecture seule (cf. TESTABLE_NAMESPACES). Les variantes `*_app`
    (MCP Apps SEP-1865) renvoient un composant d'UI, pas du JSON → non testables."""
    if name.endswith("_app"):
        return False
    return namespace_of(name) in TESTABLE_NAMESPACES


def is_default_hidden(name: str) -> bool:
    return name in DEFAULT_HIDDEN_TOOLS or namespace_of(name) in DEFAULT_HIDDEN_NAMESPACES


def is_tool_visible(
    name: str,
    disabled: set[str],
    enabled_override: set[str],
) -> bool:
    """Règle de visibilité effective pour un tool donné.

    Override positif perso prime > désactivé perso > masqué-par-défaut > visible.
    Les méta-tools protégés ne sont jamais masqués (anti-lockout)."""
    if name in PROTECTED_TOOLS:
        return True  # anti-lockout : jamais masqué (ni toggle perso, ni default-hidden)
    if name in enabled_override:
        return True
    if name in disabled:
        return False
    if is_default_hidden(name):
        return False
    return True


def effective_disabled(
    all_names: set[str],
    disabled: set[str],
    enabled_override: set[str],
) -> set[str]:
    """Ensemble des tools à masquer pour cet user, parmi `all_names`."""
    return {
        n
        for n in all_names
        if not is_tool_visible(n, disabled, enabled_override)
    }
