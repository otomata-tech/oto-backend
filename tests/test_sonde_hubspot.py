"""La sonde de connexion HubSpot — otomata-tech/oto#69. Couvre `auth+scopes`.

`GET /account-info/v3/details` identifie le compte (`portalId`) : c'est le VERDICT,
un refus y lève. Puis les scopes, objet par objet — une MESURE rendue à côté du
verdict, jamais le verdict : un jeton sans le scope tickets marche pour le reste
(troisième règle d'oto#69), donc `ok` reste vrai et le manque se lit dans `scopes`.
Le détail des scopes est couvert par `test_hubspot_scopes_pipelines.py`.
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


class _Rep:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.content = b"x"
        self.text = str(body)

    def json(self):
        return self._body


class _Session:
    """Introspection du jeton : rend tous les scopes."""
    def __init__(self):
        self.appels = []

    def request(self, method, url, **kw):
        self.appels.append((method, url))
        return _Rep(200, {"scopes": ["crm.objects.contacts.read"]})


class _FauxClient:
    BASE_URL = "https://api.hubapi.com"

    def __init__(self, reponse=None, boom=None):
        self._reponse, self._boom = reponse, boom
        self.appels = []
        self.session = _Session()

    def _request(self, method, path, **kw):
        self.appels.append((method, path))
        if self._boom:
            raise self._boom
        return self._reponse


def _brancher(monkeypatch, client):
    import oto.tools.hubspot.client as hc
    monkeypatch.setattr(hc, "HubSpotClient", lambda **kw: client)
    return client


def test_un_compte_identifie_passe_et_mesure_ses_scopes(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient(
        {"portalId": 123456, "accountType": "STANDARD", "timeZone": "Europe/Paris"}))
    rendu = H._verify(_fields("k"))
    assert cli.appels == [("GET", "/account-info/v3/details")], "le verdict : un seul appel"
    assert rendu["scopes"]["method"] == "token_info"
    assert [m for m, _ in cli.session.appels] == ["POST"], "les scopes : une lecture"


def test_une_cle_refusee_leve(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient(boom=RuntimeError("HTTP 401: invalid token")))
    with pytest.raises(RuntimeError, match="401"):
        H._verify(_fields("k"))
    assert cli.session.appels == [], "une clé refusée ne mesure aucun scope"


def test_une_reponse_200_SANS_identite_est_un_echec(monkeypatch):
    _brancher(monkeypatch, _FauxClient({}))
    with pytest.raises(RuntimeError, match="without identifying"):
        H._verify(_fields("k"))


def test_la_sonde_est_enregistree_avec_la_couverture_auth_scopes():
    from fastmcp import FastMCP

    H.register(FastMCP("t"))
    assert cv.supports("hubspot")
    assert cv.probe_for("hubspot") is H._verify
    assert cv.couverture("hubspot") == cv.AUTH_SCOPES


def test_la_mesure_des_scopes_passe_le_filtre_des_mesures():
    """`executer` ne garde que les clés connues : sans `scopes` dans la liste, la
    mesure mourait en silence entre la sonde et la réponse."""
    assert cv._mesures({"scopes": {"families": {}}, "bruit": 1}) == {
        "scopes": {"families": {}}}
