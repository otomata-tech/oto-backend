"""La sonde de connexion Webflow — otomata-tech/oto#69. Couvre `auth` SEUL.

`GET /v2/token/authorized_by` exige le scope `authorized_user:read`, SÉPARÉ
des scopes réels du connecteur (`cms:read`/`sites:read`) — quatrième règle
d'oto#69 : un 403 sur CET appel précis ne dit rien de la clé pour l'usage réel,
et ne doit JAMAIS lever `NonAutorise` (ça pousserait à révoquer une clé qui
marche). Seul le 401 (jeton mort) est un vrai refus d'authentification.
"""
from __future__ import annotations

import pytest
from oto.tools.common.errors import UpstreamHTTPError

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import webflow as W


def _fields(secret: str) -> dict:
    """Champs EXACTEMENT comme la capacité verify les produit — coupler le test
    au vrai unpack empêche le drift sonde↔schéma (régression 05/09 sur
    folk/hunter/pennylane, `tests/connectors/test_verify_sondes_champs_reels.py`)."""
    return credentials_store.unpack_secret("webflow", secret)


class _FauxClient:
    def __init__(self, reponse=None, boom=None):
        self._reponse, self._boom = reponse, boom

    def _request(self, method, endpoint, **kw):
        if self._boom:
            raise self._boom
        return self._reponse


def _brancher(monkeypatch, client):
    import oto.tools.webflow.client as wc
    monkeypatch.setattr(wc, "WebflowClient", lambda **kw: client)
    return client


def test_un_utilisateur_identifie_passe(monkeypatch):
    _brancher(monkeypatch, _FauxClient(
        {"id": "u1", "email": "a@b.c", "firstName": "A", "lastName": "B"}))
    W._verify(_fields("k"))  # ne lève pas


def test_un_401_est_un_vrai_refus_d_auth(monkeypatch):
    _brancher(monkeypatch, _FauxClient(
        boom=UpstreamHTTPError(401, "invalid token", service="webflow")))
    with pytest.raises(cv.NonAutorise):
        W._verify(_fields("k"))


def test_LE_POINT_un_403_ne_leve_PAS_non_autorise(monkeypatch):
    """Le cœur de la quatrième règle : le scope de CET appel n'est pas celui du
    connecteur — un 403 ici ne doit jamais dire « clé invalide »."""
    _brancher(monkeypatch, _FauxClient(
        boom=UpstreamHTTPError(403, "missing scope authorized_user:read", service="webflow")))
    with pytest.raises(RuntimeError) as e:
        W._verify(_fields("k"))
    assert not isinstance(e.value, cv.SondeRefusee), (
        "un 403 sur token/authorized_by ne doit JAMAIS lever NonAutorise — ça "
        "pousserait à révoquer une clé qui marche pour l'usage réel (CMS)")
    assert cv.classer(e.value) == cv.UNKNOWN, (
        "doit tomber sur 'unknown' (je ne sais pas), pas 'unauthorized'")


def test_une_reponse_200_SANS_identite_est_un_echec(monkeypatch):
    _brancher(monkeypatch, _FauxClient({}))
    with pytest.raises(RuntimeError, match="sans identifier"):
        W._verify(_fields("k"))


def test_la_sonde_est_enregistree_avec_la_couverture_auth():
    from fastmcp import FastMCP

    W.register(FastMCP("t"))
    assert cv.supports("webflow")
    assert cv.probe_for("webflow") is W._verify
    assert cv.couverture("webflow") == cv.AUTH
