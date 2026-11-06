"""The served tool surface is frozen (`tests/surface_outils.txt`, `scripts/surface_outils.py`), decided 05/11/2026.

Alexis's guard for the generic reader of descriptions: nothing breaks. No existing tool changes its name, its ops nor
its input schema; the reader's own tools are not recorded. A deliberate surface change rewrites the record
(`python scripts/surface_outils.py`) in the same commit, and is reviewed as such.
"""
from __future__ import annotations

import pathlib
import sys

from _mcp_app import static_mcp

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts import surface_outils  # noqa: E402


def test_la_surface_servie_est_celle_du_releve():
    attendu = surface_outils.RELEVE.read_text(encoding="utf-8").splitlines()
    servi = surface_outils.releve(static_mcp()).splitlines()
    assert servi == attendu, (
        f"retirés ou changés : {sorted(set(attendu) - set(servi))[:20]} ; "
        f"ajoutés ou changés : {sorted(set(servi) - set(attendu))[:20]}")


def test_l_empreinte_ne_depend_pas_de_l_ordre_d_un_enum():
    a = {"type": "object", "properties": {"op": {"enum": ["b", "a"]}}}
    b = {"properties": {"op": {"enum": ["a", "b"]}}, "type": "object"}
    assert surface_outils.empreinte(a) == surface_outils.empreinte(b)
    assert surface_outils.empreinte(a) != surface_outils.empreinte({"type": "object", "properties": {}})
