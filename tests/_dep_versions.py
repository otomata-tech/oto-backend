"""Deux dépendances FLOTTANTES ont fait dériver le venv partagé, exactement
comme le pin git d'oto-core que `_oto_core_pin.py` détecte déjà — mais sans
coordonnée EXACTE à comparer (un intervalle `>=X,<Y`/`>=X`, pas un tag).

Découvert le 05/09/2026 en creusant un « socle connu » de 9 tests rouges/erreurs
qu'on croyait propres à ce dépôt : dans un venv JETÉ avec des dépendances
fraîchement résolues, les 9 passent. Dans le venv partagé :

- **`fastmcp`** (plancher+plafond `>=3.4.2,<3.5`) : à 3.4.2 (le PLANCHER, installé
  ici depuis longtemps), `pydantic_core.ValidationError` n'est pas encore
  enveloppée en `fastmcp.exceptions.ValidationError` — 3 tests de
  `test_github_leexi_productlane.py` qui l'attendent lèvent le type brut.
  Confirmé absent en 3.4.2, présent en 3.4.7.
- **`france_opendata`** (tiré transitivement par oto-core, `>=0.7.0`, sans
  plafond) : à 0.11.0 (installé ici), des classes/modules entiers ont disparu
  du paquet (`EgaproClient`, et 13 modules data listés par
  `tests/fod/test_fod_coverage.py`) — présents en 0.42.0.

⚠️ **Les seuils ci-dessous sont le PLUS HAUT confirmé CASSÉ, pas le minimum réel
qui marche** — personne n'a bissecté les versions intermédiaires. Un seuil qui
s'avère trop haut ne fait QUE sauter un peu plus large que nécessaire ; trop bas
laisserait revenir un rouge qui ne prouve rien. Resserrer si quelqu'un bissecte.
"""
from __future__ import annotations

import os
from importlib import metadata

try:
    from packaging.version import Version
except ImportError:  # transitif quasi garanti (pip/setuptools la tirent déjà)
    Version = None

#: Même politique que `_oto_core_pin.skips_autorises()` : CI installe toujours
#: des versions fraîches (aucune raison de skipper là-bas), et un skip en CI
#: serait une garde qui s'éteint pile où elle protège.
ENV_CI = "CI"

_PLANCHERS = {
    "fastmcp": "3.4.7",
    "france-opendata": "0.42.0",
}


def _version(pkg: str) -> "str | None":
    try:
        return metadata.version(pkg)
    except metadata.PackageNotFoundError:
        return None


def trop_vieux(pkg: str) -> "str | None":
    """Motif NOMMÉ si `pkg` est installé EN DESSOUS de son plancher constaté,
    sinon `None` (absent, introuvable, ou à jour — dans les trois cas ce n'est
    pas CE problème-ci)."""
    if os.environ.get(ENV_CI):
        return None
    plancher = _PLANCHERS.get(pkg)
    v = _version(pkg)
    if plancher is None or v is None or Version is None:
        return None
    if Version(v) < Version(plancher):
        return (f"{pkg} {v} installé < {plancher} constaté nécessaire ici — "
                f"venv partagé en retard sur une dépendance flottante (jamais "
                f"réinstallée), pas un défaut de ce test. cf. tests/_dep_versions.py")
    return None
