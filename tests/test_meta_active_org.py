"""`_active_org` (meta.py) scope les toggles/presets via le seam `access.current_org`.

ADR 0038 B3 : le BRACELET de session (`oto_use_org`) est retiré — le seam résout
`jeton d'appel ?? consultation ?? maison`. Régressions couvertes : le jeton `org=`
gagne ; un bracelet résiduel (WIP en vol qui écrirait encore le store inerte) est
IGNORÉ, membre ou pas ; sans jeton → maison.
"""
import pytest

from oto_mcp import org_store, session_org
from oto_mcp.tools.meta import _active_org


@pytest.fixture(autouse=True)
def _home_is_99(monkeypatch):
    # Org maison = 99 ; aucun sous-domaine ni view-as posé par défaut.
    monkeypatch.setattr(org_store, "get_active_org", lambda sub: 99)
    yield


def test_call_token_wins_over_home():
    # Jeton d'appel `org=7` posé (déjà gardé par resolve_org_guarded) → gagne.
    tok = session_org.set_call_org(7)
    try:
        assert _active_org("u") == 7
    finally:
        session_org.reset_call_org(tok)


def test_bracelet_ignored_even_for_member(monkeypatch):
    # Bracelet résiduel vers 7 (store inerte, ADR 0038 B3) → IGNORÉ, repli maison —
    # même si le sub est membre de 7 : le bracelet n'est plus une source de scope.
    monkeypatch.setattr(session_org, "current_override", lambda: (True, 7))
    assert _active_org("u") == 99


def test_falls_back_to_home_without_token():
    # Pas de jeton d'appel → repli sur la maison (99).
    assert _active_org("u") == 99
