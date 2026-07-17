"""Registre de « pending actions » par connecteur (lot 2, seam générique).

Certains connecteurs ont une connexion en DEUX temps : la clé/l'autorisation
résout, mais il manque encore une étape côté user pour être opérationnel
(unipile : lier un canal ; session navigateur : ré-authentifier une session
morte). Plutôt que de faire remonter ces notions spécifiques dans le modèle
générique (`ProviderStatus`), chaque connecteur ENREGISTRE ici un hook qui
répond « quelle étape manque ? » — le front reste agnostique : il affiche le
libellé tel quel comme verdict + CTA.

Patron identique à `connector_verify.py` : registre passif, enregistrement à
l'import du module connecteur, fail-open (un hook qui casse ne casse jamais
/api/me).
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# fn(sub, org, group, entry) -> libellé de l'étape manquante, ou None si rien.
# `entry` = l'entrée ProviderStatus déjà construite (mode, flags par niveau…).
_HOOKS: dict[str, Callable[[str, Optional[int], Optional[int], dict], Optional[str]]] = {}


def register(connector: str, fn: Callable[[str, Optional[int], Optional[int], dict], Optional[str]]) -> None:
    _HOOKS[connector] = fn


def has_hook(connector: str) -> bool:
    return connector in _HOOKS


def pending_action(connector: str, sub: str, org: Optional[int],
                   group: Optional[int], entry: dict) -> Optional[str]:
    """Étape manquante pour ce (sub, connecteur), ou None. Fail-open."""
    fn = _HOOKS.get(connector)
    if fn is None:
        return None
    try:
        return fn(sub, org, group, entry)
    except Exception:
        logger.warning("status_hints: hook %s en échec (fail-open)", connector,
                       exc_info=True)
        return None
