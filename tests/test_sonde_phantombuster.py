"""La sonde de connexion Phantombuster — otomata-tech/oto#69. Couvre `auth` SEUL.

`GET /containers` sans `agent_id` (déjà dans le client — `list_containers`),
`limit=1`. Une liste VIDE est un état normal (aucune exécution), jamais un
refus — seul le fait que l'appel n'ait pas levé compte.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import phantombuster as P


def _fields(secret: str) -> dict:
    """Champs EXACTEMENT comme la capacité verify les produit — coupler le test
    au vrai unpack empêche le drift sonde↔schéma (régression 05/09 sur
    folk/hunter/pennylane, `tests/connectors/test_verify_sondes_champs_reels.py`)."""
    return credentials_store.unpack_secret("phantombuster", secret)


class _FauxClient:
    def __init__(self, boom=None):
        self._boom = boom
        self.appels = []

    def list_containers(self, agent_id=None, limit=10):
        self.appels.append((agent_id, limit))
        if self._boom:
            raise self._boom
        return []


def _brancher(monkeypatch, client):
    import oto.tools.phantombuster.client as pc
    monkeypatch.setattr(pc, "PhantombusterClient", lambda **kw: client)
    return client


def test_une_cle_valide_passe(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient())
    P._verify(_fields("k"))
    assert cli.appels == [(None, 1)]


def test_une_cle_refusee_leve(monkeypatch):
    _brancher(monkeypatch, _FauxClient(boom=RuntimeError("401 Client Error")))
    with pytest.raises(RuntimeError, match="401"):
        P._verify(_fields("k"))


def test_la_sonde_est_enregistree_avec_la_couverture_auth():
    from fastmcp import FastMCP

    P.register(FastMCP("t"))
    assert cv.supports("phantombuster")
    assert cv.probe_for("phantombuster") is P._verify
    assert cv.couverture("phantombuster") == cv.AUTH
