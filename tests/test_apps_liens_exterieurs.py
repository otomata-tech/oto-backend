"""`oto.open(url)` n'est servi dans la description d'`oto_apps` que sur une instance
qui déclare que son hôte gère les liens vers l'extérieur (`OTO_APPS_LIENS_EXTERIEURS`) :
un choix d'instance explicite, jamais un repli."""
from __future__ import annotations

import pytest

from oto_mcp import interrupteurs
from oto_mcp.capabilities import apps


def test_instance_qui_declare_l_hote_la_description_nomme_open():
    d = apps.description_app(True)
    assert "open(url)" in d and "whole address" in d
    assert "go(route) (or a link <a href=\"#/route\">) / open(url)" in d


def test_instance_qui_ne_declare_pas_la_description_tait_open():
    d = apps.description_app(False)
    assert "open(url)" not in d and "whole address" not in d
    # Sans le réglage, la description est celle d'avant, mot pour mot autour du pont.
    assert "go(route). A click in the app" in d and "#/route" not in d


@pytest.mark.parametrize("brut, attendu", [(None, False), ("", False), ("0", False),
                                           ("1", True), ("on", True)])
def test_le_reglage_se_lit_en_interrupteur(monkeypatch, brut, attendu):
    if brut is None:
        monkeypatch.delenv(apps.ENV_LIENS_EXTERIEURS, raising=False)
    else:
        monkeypatch.setenv(apps.ENV_LIENS_EXTERIEURS, brut)
    assert apps.liens_exterieurs() is attendu


def test_un_reglage_illisible_leve(monkeypatch):
    monkeypatch.setenv(apps.ENV_LIENS_EXTERIEURS, "peut-etre")
    with pytest.raises(interrupteurs.InterrupteurIllisible):
        apps.liens_exterieurs()
