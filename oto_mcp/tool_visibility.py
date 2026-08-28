"""Visibilité des tools : masquage par défaut + règle de visibilité effective.

**Masqués par défaut** mais **self-activables** (`oto_enable_tool`) — simple
découvrabilité, pas un contrôle d'accès : pour des surfaces verbeuses/spécifiques
sans enjeu de sécurité. Un seul grain : DEFAULT_HIDDEN_TOOLS (noms individuels).
Le grain CONNECTEUR (ex-`default_hidden` du registre) a été retiré (ADR 0050) —
un connecteur hors du socle `default_active` est simplement non-installé
(sélection 0019, régime strict) et se réinstalle depuis la library.

Modèle de visibilité effective : un tool est visible sauf s'il est désactivé
(toggle perso) ou masqué par défaut ; `enabled_override` prime pour rendre visible
un masqué-par-défaut. La gouvernance d'accès (activation org, RBAC connecteur
ADR 0025, credential) est appliquée AILLEURS — la visibilité n'est PAS une barrière
de sécurité (ADR 0031).
"""
from __future__ import annotations

# Masqués par défaut mais self-activables (découvrabilité, pas sécurité).
# `email_send` : envoi d'email per-org. Autz DYNAMIQUE dans le handler (membre de
# l'org pour une adresse déclarée de l'org ; super_admin pour le repli marque
# oto@otomata.tech) — masqué ici pour ne pas encombrer la toolbox des orgs sans
# adresse configurée. La vraie barrière reste le check de rôle, pas ce masquage.
# `fr_egapro_declaration` : source de niche (index égalité F-H par SIREN, surtout
# utile en qualif sociale type Mūcho) — masquée pour ne pas charger la toolbox `fr`
# par défaut ; activable à la demande (oto_enable_tool fr_egapro_declaration).
# `browser_eval` : exécute du JS ARBITRAIRE dans une session loguée (oto-private#79).
# Borné à un connecteur écrit en dur (pennylaneged), ce pouvoir est contenu ; sur le
# connecteur GÉNÉRIQUE `browser` il devient pointable n'importe où → le geste courant
# (`browser_fetch`) est exposé, l'échappatoire demande une activation explicite. C'est
# de la découvrabilité graduée, pas une barrière (le connecteur reste le vrai gate).
# `lemlist_launch_lead` : pousse un lead dans une séquence d'envoi RÉELLE (sort son
# état "en attente de review"). `lemlist_create_lead`/`lemlist_add_lead_variables`
# restent visibles (créer/annoter un lead n'envoie rien tant qu'il n'est pas lancé) —
# seul le geste qui déclenche l'envoi est masqué, même logique graduée que ci-dessus.
DEFAULT_HIDDEN_TOOLS: frozenset[str] = frozenset(
    {"email_send", "fr_egapro_declaration", "browser_eval", "lemlist_launch_lead"})

# Méta-tools TOUJOURS visibles (anti-lockout) : sans eux l'utilisateur ne peut
# plus se déverrouiller (lister/activer un tool) — plus l'identité `oto_whoami`
# et la fiche `oto_profile`, qui doivent rester atteignables au démarrage d'un
# compte même sous une visibilité restrictive. SOURCE UNIQUE : meta.py et
# api_routes en dérivent.
PROTECTED_TOOLS: frozenset[str] = frozenset(
    {"oto_list_my_tools", "oto_enable_tool", "oto_profile",
     "oto_whoami",
     # Guides d'usage plateforme (oto-backend#111) — read-only, toujours atteignable :
     # l'agent doit pouvoir charger le how-to (ex. bulk-load) en toute session.
     "oto_guide",
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
     # (oto_use_project). `oto_doc`/`oto_doc_app` = pendant DOCUMENTS du projet
     # (pages markdown, KB) : même rôle spine de gestion → jamais évinçable, sinon
     # l'agent ne peut plus écrire les pages d'un projet en pleine tâche (signal #213).
     "oto_project", "oto_use_project", "oto_clear_project",
     "oto_doc", "oto_doc_app"})


# Namespaces SPINE plateforme, TOUJOURS montés (hors gate connecteur) : datastore
# (`data_*` — substrat PG natif, cf. providers/google.py « PAS un connecteur »), boucle
# d'usage (`run_*`, `feedback`). Trop de noms `data_*` pour les lister → on protège
# le namespace entier. Le namespace `oto` n'y est PAS (il porte les `oto_admin_*`
# gatés par rôle) → ses tools spine sont listés par NOM dans PROTECTED_TOOLS.
# Miroir de meta._NON_DISPATCHABLE / middleware.field_redaction._SPINE_SERVICES.
PROTECTED_NAMESPACES: frozenset[str] = frozenset({"data", "run", "feedback"})


# Testables depuis le dashboard (bouton « tester » de la fiche connecteur) :
# l'exécution est RÉELLE, déclenchée par un humain via REST → bornée aux
# connecteurs open-data en LECTURE SEULE (aucun effet de bord, aucune mutation,
# pas de credential BYO requis). Un « test » ne doit JAMAIS envoyer un email,
# écrire une donnée ou poster un message. FOD (données publiques France) est le
# cœur de cible. Étendre = ajouter un namespace read-only ici (source unique).
TESTABLE_NAMESPACES: frozenset[str] = frozenset(
    {"fr", "foncier", "urba", "sante", "frenchtech", "culture", "infosec"})


def namespace_of(name: str) -> str:
    """Namespace d'un tool = le plus long préfixe **déclaré au registre**, aligné sur
    les `_` ; à défaut, le premier token (ex. `mm_company` → `mm`).

    Pourquoi pas « le 1er token » tout court (règle d'avant le 2026-08-10) : le
    namespace résout LE CONNECTEUR qui gouverne le tool (`connector_for_namespace` →
    gates d'appel, visibilité de session, sélection, projets, slots). Sous un schéma
    de nommage `capacité_fournisseur` (ADR 0010 §Amendement), le 1er token est la
    CAPACITÉ : deux connecteurs distincts tomberaient sur le même namespace, et le
    gate en désignerait un au hasard — mauvais credential, mauvaise activation,
    mauvaise sélection.

    **Le porteur vivant, aujourd'hui : `linkedin_aiark`.** Deux surfaces commencent
    par `linkedin_` et n'ont rien à voir — `linkedin_*` est la SESSION qu'on opère
    (hébergée), `linkedin_aiark_*` est de la DONNÉE ACHETÉE au crédit chez AI Ark
    (email vérifié, mobile, reverse-lookup ; LinkedIn n'y est qu'une des sources).
    Sans la résolution au plus long préfixe, `linkedin_aiark_person` tomberait sous
    le connecteur `linkedin` : mauvaise clé (celle du compte hébergé, pas celle
    d'AI Ark), mauvaise activation, mauvaise sélection. C'est exactement le cas pour
    lequel la règle existe, et il est verrouillé par `tests/test_linkedin.py`.

    **Strictement additif** : un tool dont aucun préfixe multi-token n'est déclaré
    garde son 1er token — comportement identique à l'implémentation d'avant.

    Un tool dont aucun préfixe n'est déclaré garde son 1er token (le gate reste
    fail-open sur un namespace inconnu, inchangé).
    """
    if "_" not in name:
        return name
    from . import providers
    parts = name.split("_")
    for i in range(len(parts) - 1, 1, -1):   # du plus long au plus court, 1er token exclu
        candidate = "_".join(parts[:i])
        if providers.connector_for_namespace(candidate) is not None:
            return candidate
    return parts[0]


def is_testable(name: str) -> bool:
    """Un tool est testable depuis le dashboard s'il appartient à un namespace
    open-data en lecture seule (cf. TESTABLE_NAMESPACES). Les variantes `*_app`
    (MCP Apps SEP-1865) renvoient un composant d'UI, pas du JSON → non testables."""
    if name.endswith("_app"):
        return False
    return namespace_of(name) in TESTABLE_NAMESPACES


def is_default_hidden(name: str) -> bool:
    return name in DEFAULT_HIDDEN_TOOLS


def is_protected(name: str) -> bool:
    """True si `name` est un tool SPINE anti-lockout : jamais masquable par AUCUN
    mécanisme (toggle perso, default-hidden, gating connecteur/RBAC/sélection/admin).
    Source UNIQUE consommée par `is_tool_visible` (couche toggle) ET le garde final
    de `compute_hidden_tools` (couches de gating) — sans ce garde, un tool protégé
    n'était sauvé des blocs de gating que parce que son namespace ne résolvait aucun
    connecteur (effet de bord fragile, signal d’usage #213)."""
    return name in PROTECTED_TOOLS or namespace_of(name) in PROTECTED_NAMESPACES


def is_tool_visible(
    name: str,
    disabled: set[str],
    enabled_override: set[str],
    admin_hidden: frozenset[str] = frozenset(),
) -> bool:
    """Règle de visibilité effective pour un tool donné.

    Override positif perso prime > désactivé perso > masqué par un admin
    (denylist org/équipe, `access.org_admin_hidden_tools`/`group_admin_hidden_tools`)
    > masqué-par-défaut plateforme > visible. Les méta-tools protégés ne sont
    jamais masqués (anti-lockout). Le masquage admin reste de la GOUVERNANCE
    (ADR 0031, pas une barrière de sécurité) : un override perso positif le lève
    toujours, même échappatoire qu'un masqué-par-défaut plateforme — un cran
    plus spécifique, rien de plus."""
    if is_protected(name):
        return True  # anti-lockout : jamais masqué (ni toggle perso, ni default-hidden/admin)
    if name in enabled_override:
        return True
    if name in disabled:
        return False
    if name in admin_hidden:
        return False
    if is_default_hidden(name):
        return False
    return True


def effective_disabled(
    all_names: set[str],
    disabled: set[str],
    enabled_override: set[str],
    admin_hidden: frozenset[str] = frozenset(),
) -> set[str]:
    """Ensemble des tools à masquer pour cet user, parmi `all_names`."""
    return {
        n
        for n in all_names
        if not is_tool_visible(n, disabled, enabled_override, admin_hidden)
    }
