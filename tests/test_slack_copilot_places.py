"""Les places des tours Slack de l'agent intégré (`oto_mcp/slack_copilot_places.py`)."""
from __future__ import annotations

import pytest

from oto_mcp import slack_copilot_places as places


@pytest.fixture(autouse=True)
def _neuf(monkeypatch):
    monkeypatch.setattr(places, "_par_compte", {})
    monkeypatch.setattr(places, "_total", 0)
    monkeypatch.delenv(places.ENV_MAX, raising=False)
    places.plafond.cache_clear()
    yield
    places.plafond.cache_clear()


def test_le_defaut_est_huit():
    assert places.plafond() == places.MAX_DEFAUT == 8


@pytest.mark.parametrize("brut", ["0", "-2", "huit", "1.5"])
def test_un_reglage_illisible_refuse_de_demarrer(monkeypatch, brut):
    monkeypatch.setenv(places.ENV_MAX, brut)
    with pytest.raises(places.ReglageInvalide):
        places.verifier_configuration()


def test_un_compte_n_a_qu_une_place_les_autres_gardent_la_leur():
    with places.place("a"):
        with pytest.raises(places.Occupe):
            with places.place("a"):
                pass
        with places.place("b"):
            assert places.en_cours() == 2
    assert places.en_cours() == 0


def test_au_plafond_le_suivant_est_refuse_tout_de_suite_puis_passe_apres(monkeypatch):
    monkeypatch.setenv(places.ENV_MAX, "2")
    with places.place("a"), places.place("b"):
        with pytest.raises(places.Occupe):
            with places.place("c"):
                pass
    with places.place("c"):
        assert places.en_cours() == 1


def test_la_place_est_rendue_quand_le_tour_leve():
    with pytest.raises(ValueError):
        with places.place("a"):
            raise ValueError
    assert places.en_cours() == 0 and places._par_compte == {}
