"""La sonde de connexion Supabase — otomata-tech/oto#69. Couvre `auth` SEUL.

`GET /v1/projects` (déjà dans le client — `list_projects`). Une liste VIDE est
un état normal (organisation sans projet), jamais un refus — seul le fait que
l'appel n'ait pas levé compte.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import supabase as S


def _fields(secret: str) -> dict:
    """Champs EXACTEMENT comme la capacité verify les produit — coupler le test
    au vrai unpack empêche le drift sonde↔schéma (régression 05/09 sur
    folk/hunter/pennylane, `tests/connectors/test_verify_sondes_champs_reels.py`)."""
    return credentials_store.unpack_secret("supabase", secret)


def test_une_liste_de_projets_meme_vide_passe(monkeypatch):
    import oto.tools.supabase as pkg
    vu = {}
    monkeypatch.setattr(pkg.client, "list_projects",
                        lambda token=None: vu.setdefault("token", token) or [])
    S._verify(_fields("k"))
    assert vu["token"] == "k"


def test_une_cle_refusee_leve(monkeypatch):
    import oto.tools.supabase as pkg

    def _boom(token=None):
        raise RuntimeError("HTTP 401: Invalid API key")
    monkeypatch.setattr(pkg.client, "list_projects", _boom)
    with pytest.raises(RuntimeError, match="401"):
        S._verify(_fields("k"))


def test_la_sonde_est_enregistree_avec_la_couverture_auth():
    from fastmcp import FastMCP

    S.register(FastMCP("t"))
    assert cv.supports("supabase")
    assert cv.probe_for("supabase") is S._verify
    assert cv.couverture("supabase") == cv.AUTH
