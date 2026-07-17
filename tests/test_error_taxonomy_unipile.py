"""Erreurs de connecteur amont d'input sans statut HTTP — drop Sentry + message
agent (oto-backend#90).

Une `UnipileError` (oto-core) d'INPUT/config n'a pas de `status_code` (facette
LinkedIn introuvable, compte non connecté…) : elle fuyait vers Sentry (ni 4xx ni
McpError) et l'agent recevait « Erreur interne ». Elle est désormais reconnue comme
gérée (droppée) et classée `invalid_input` avec son vrai message. Les 4xx (déjà
couverts par `.status_code`) et les erreurs RÉSEAU (transitoires) sont inchangés.
"""
from __future__ import annotations

from oto_mcp.error_taxonomy import (
    _is_expected_error, _is_upstream_managed_error, classify,
)


class UnipileError(RuntimeError):
    """Réplique du type oto-core (reconnu par nom de classe) : message + status_code."""
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def test_input_error_is_managed_and_dropped():
    e = UnipileError("Facette COMPANY introuvable pour : 'Acme'")
    assert _is_upstream_managed_error(e) is True
    assert _is_expected_error(e) is True   # → before_send droppe (pas un bug backend)


def test_not_connected_is_managed():
    e = UnipileError("Aucun compte LinkedIn connecté sur Unipile")
    assert _is_upstream_managed_error(e) is True


def test_network_error_is_NOT_dropped():
    # réseau = transitoire (panne potentielle) → reste reporté à Sentry
    e = UnipileError("Unipile: erreur réseau (ConnectionError).")
    assert _is_upstream_managed_error(e) is False
    assert _is_expected_error(e) is False


def test_4xx_unipile_unaffected():
    # un 4xx porte déjà status_code → couvert par _is_managed_connector_error,
    # pas par le nouveau prédicat (qui ne vise QUE status_code None)
    e = UnipileError("Unipile 422: recipient invalide", status_code=422)
    assert _is_upstream_managed_error(e) is False
    assert _is_expected_error(e) is True   # via le chemin 4xx existant


def test_classify_echoes_agent_message():
    e = UnipileError("Facette COMPANY introuvable pour : 'Acme'")
    info = classify(e)
    assert info.code == "invalid_input"
    assert info.retryable is False
    assert "introuvable" in info.message   # message agent-utile, pas « Erreur interne »


def test_unrelated_exception_still_internal():
    # un vrai bug backend (pas UnipileError) reste « internal », jamais droppé
    e = KeyError("something")
    assert _is_upstream_managed_error(e) is False
    assert _is_expected_error(e) is False
    assert classify(e).code == "internal"
