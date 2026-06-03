"""Visibilité des tools : ensemble masqué par défaut + règle effective.

Modèle :
- un tool est visible SAUF s'il est désactivé (`user_disabled_tools`) ou
  masqué par défaut (`DEFAULT_HIDDEN_TOOLS`) ;
- un tool masqué par défaut redevient visible si l'user l'a activé
  explicitement (`user_enabled_tools`), qui prime sur tout.

Les tools masqués par défaut sont des surfaces spécifiques/sensibles (ex.
connecteur compta GoCardless d'une mission) qu'on ne veut pas voir polluer
la liste de tous les users. Ils restent activables à la demande via
`oto_enable_tool` ou un preset qui les liste.
"""
from __future__ import annotations

# ⚠️ À tenir à jour si on ajoute des tools à un namespace masqué par défaut.
DEFAULT_HIDDEN_TOOLS = frozenset({
    "gocardless_creditors",
    "gocardless_payments",
    "gocardless_payment",
    "gocardless_events",
    "gocardless_payment_party",
    "gocardless_failure_reason",
})


def is_tool_visible(name: str, disabled: set[str], enabled_override: set[str]) -> bool:
    """Règle de visibilité effective pour un tool donné."""
    if name in enabled_override:
        return True
    if name in disabled:
        return False
    if name in DEFAULT_HIDDEN_TOOLS:
        return False
    return True


def effective_disabled(all_names: set[str], disabled: set[str],
                       enabled_override: set[str]) -> set[str]:
    """Ensemble des tools à masquer pour cet user, parmi `all_names`."""
    return {n for n in all_names if not is_tool_visible(n, disabled, enabled_override)}
