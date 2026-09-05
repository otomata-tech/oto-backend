"""La sonde de connexion HubSpot — otomata-tech/oto#69. Couvre `auth` SEUL.

`GET /account-info/v3/details` : identifie le compte (`portalId`), ne révèle
aucun scope. HubSpot accorde ses scopes OBJET PAR OBJET (contacts, tickets…) —
un manque local n'est pas un état « connecteur mort » et ne se mesure pas ici
(troisième règle d'oto#69), il est déjà traduit à l'appel réel par
`_scope_refusal`.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import hubspot as H


def _fields(secret: str) -> dict:
    """Champs EXACTEMENT comme la capacité verify les produit — coupler le test
    au vrai unpack empêche le drift sonde↔schéma (régression 05/09 sur
    folk/hunter/pennylane, `tests/connectors/test_verify_sondes_champs_reels.py`)."""
    return credentials_store.unpack_secret("hubspot", secret)


class _FauxClient:
    def __init__(self, reponse=None, boom=None):
        self._reponse, self._boom = reponse, boom
        self.appels = []

    def _request(self, method, path, **kw):
        self.appels.append((method, path))
        if self._boom:
            raise self._boom
        return self._reponse


def _brancher(monkeypatch, client):
    import oto.tools.hubspot.client as hc
    monkeypatch.setattr(hc, "HubSpotClient", lambda **kw: client)
    return client


def test_un_compte_identifie_passe(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient(
        {"portalId": 123456, "accountType": "STANDARD", "timeZone": "Europe/Paris"}))
    H._verify(_fields("k"))
    assert cli.appels == [("GET", "/account-info/v3/details")], "un seul appel, en lecture"


def test_une_cle_refusee_leve(monkeypatch):
    _brancher(monkeypatch, _FauxClient(boom=RuntimeError("HTTP 401: invalid token")))
    with pytest.raises(RuntimeError, match="401"):
        H._verify(_fields("k"))


def test_une_reponse_200_SANS_identite_est_un_echec(monkeypatch):
    _brancher(monkeypatch, _FauxClient({}))
    with pytest.raises(RuntimeError, match="sans identifier"):
        H._verify(_fields("k"))


def test_la_sonde_est_enregistree_avec_la_couverture_auth():
    from fastmcp import FastMCP

    H.register(FastMCP("t"))
    assert cv.supports("hubspot")
    assert cv.probe_for("hubspot") is H._verify
    assert cv.couverture("hubspot") == cv.AUTH
