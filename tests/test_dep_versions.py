"""Le détecteur de dépendance flottante en retard (`_dep_versions.trop_vieux`).

Même défaut que le pin oto-core, sur deux paquets PyPI à intervalle plutôt qu'à
tag exact : un venv partagé qui ne se réinstalle jamais pendant que
`pyproject.toml`/PyPI avancent produit des rouges qui décrivent le venv, pas le
dépôt. Ce fichier éprouve le détecteur dans les DEUX sens — un plancher qui
s'affiche toujours, ou jamais, ne prouve rien de plus qu'une absence de garde.
"""
from __future__ import annotations

import os

import pytest

from _dep_versions import _PLANCHERS, trop_vieux


def test_en_dessous_du_plancher_nomme_les_deux_versions(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr("_dep_versions._version", lambda pkg: "3.4.2")
    motif = trop_vieux("fastmcp")
    assert motif is not None
    assert "3.4.2" in motif and "3.4.7" in motif


def test_a_jour_ou_au_dessus_ne_dit_rien(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr("_dep_versions._version", lambda pkg: "3.4.7")
    assert trop_vieux("fastmcp") is None
    monkeypatch.setattr("_dep_versions._version", lambda pkg: "9.9.9")
    assert trop_vieux("fastmcp") is None


def test_paquet_absent_ne_dit_rien():
    """Absent = un autre problème (le connecteur lèverait ailleurs, plus tôt) —
    pas celui que ce détecteur nomme."""
    assert trop_vieux("un-paquet-qui-n-existe-pas-du-tout") is None


def test_en_ci_le_detecteur_se_tait_meme_en_dessous_du_plancher(monkeypatch):
    """Même politique que `_oto_core_pin.skips_autorises()` : la CI installe
    toujours des versions fraîches, et un skip là-bas serait une garde qui
    s'éteint pile où elle protège."""
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr("_dep_versions._version", lambda pkg: "0.0.1")
    assert trop_vieux("fastmcp") is None


def test_les_deux_paquets_suivis_ont_un_plancher():
    """Les trois tests qui ont motivé ce détecteur (github/leexi/productlane,
    fod_coverage, fr_avis_sirene) dépendent de ces deux entrées précises — un
    plancher retiré sans le dire ferait revenir un rouge muet chez eux."""
    assert set(_PLANCHERS) == {"fastmcp", "france-opendata"}
