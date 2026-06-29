"""`_active_org` (meta.py) scope les toggles/presets sur l'org de SESSION.

Régression ADR 0030 §6 barreau 1 : il lit le seam unique `access.current_org`
(org de session, posée par `oto_use_org`), **pas** `org_store.get_active_org`
(org maison) — sinon les toggles/presets restent ceux de la maison après une
bascule `oto_use_org`, désync UX silencieuse.
"""
import pytest

from oto_mcp import org_store, session_org
from oto_mcp.tools.meta import _active_org


@pytest.fixture(autouse=True)
def _home_is_99(monkeypatch):
    # Org maison = 99 ; aucun sous-domaine ni view-as posé par défaut.
    monkeypatch.setattr(org_store, "get_active_org", lambda sub: 99)
    yield


def test_session_override_wins_over_home(monkeypatch):
    # oto_use_org a posé une org de session 7 → les toggles se scopent sur 7.
    monkeypatch.setattr(session_org, "current_override", lambda: (True, 7))
    assert _active_org("u") == 7


def test_falls_back_to_home_without_override():
    # Pas d'override de session → repli sur la maison (99).
    assert _active_org("u") == 99


def test_perso_override_maps_to_zero(monkeypatch):
    # oto_use_org vers le perso → override (True, None) → 0 (sentinelle perso).
    monkeypatch.setattr(session_org, "current_override", lambda: (True, None))
    assert _active_org("u") == 0
