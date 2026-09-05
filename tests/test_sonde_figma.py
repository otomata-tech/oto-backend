"""La sonde de connexion Figma — otomata-tech/oto#69. Couvre `auth` SEUL.

`GET /v1/me` exige le scope `current_user:read`, SÉPARÉ des scopes réels du
connecteur (fichiers/design) — quatrième règle d'oto#69 : un 403 sur CET appel
précis ne dit rien de la clé pour l'usage réel, et ne doit JAMAIS lever
`NonAutorise` (ça pousserait à révoquer une clé qui marche). Seul le 401
(jeton mort) est un vrai refus d'authentification.
"""
from __future__ import annotations

import pytest
import requests

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import figma as F


def _fields(secret: str) -> dict:
    """Champs EXACTEMENT comme la capacité verify les produit — coupler le test
    au vrai unpack empêche le drift sonde↔schéma (régression 05/09 sur
    folk/hunter/pennylane, `tests/connectors/test_verify_sondes_champs_reels.py`)."""
    return credentials_store.unpack_secret("figma", secret)


class _FauxClient:
    def __init__(self, reponse=None, boom=None):
        self._reponse, self._boom = reponse, boom

    def _request(self, method, endpoint, **kw):
        if self._boom:
            raise self._boom
        return self._reponse


def _brancher(monkeypatch, client):
    import oto.tools.figma.client as fc
    monkeypatch.setattr(fc, "FigmaClient", lambda **kw: client)
    return client


def _http_error(status: int) -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(response=resp)


def test_un_utilisateur_identifie_passe(monkeypatch):
    _brancher(monkeypatch, _FauxClient({"id": "u1", "handle": "alice", "email": "a@b.c"}))
    F._verify(_fields("k"))  # ne lève pas


def test_un_401_est_un_vrai_refus_d_auth(monkeypatch):
    _brancher(monkeypatch, _FauxClient(boom=_http_error(401)))
    with pytest.raises(cv.NonAutorise):
        F._verify(_fields("k"))


def test_LE_POINT_un_403_ne_leve_PAS_non_autorise(monkeypatch):
    """Le cœur de la quatrième règle : le scope de CET appel n'est pas celui du
    connecteur — un 403 ici ne doit jamais dire « clé invalide »."""
    _brancher(monkeypatch, _FauxClient(boom=_http_error(403)))
    with pytest.raises(RuntimeError) as e:
        F._verify(_fields("k"))
    assert not isinstance(e.value, cv.SondeRefusee), (
        "un 403 sur /v1/me ne doit JAMAIS lever NonAutorise — ça pousserait à "
        "révoquer une clé qui marche pour l'usage réel du connecteur")
    assert cv.classer(e.value) == cv.UNKNOWN, (
        "doit tomber sur 'unknown' (je ne sais pas), pas 'unauthorized'")


def test_une_reponse_200_SANS_identite_est_un_echec(monkeypatch):
    _brancher(monkeypatch, _FauxClient({}))
    with pytest.raises(RuntimeError, match="sans identifier"):
        F._verify(_fields("k"))


def test_la_sonde_est_enregistree_avec_la_couverture_auth():
    from fastmcp import FastMCP

    F.register(FastMCP("t"))
    assert cv.supports("figma")
    assert cv.probe_for("figma") is F._verify
    assert cv.couverture("figma") == cv.AUTH
