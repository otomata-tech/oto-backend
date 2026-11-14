"""Un refus du service de données sort NOMMÉ, jamais en « Internal server error » (#1454).

`_raise_for` levait une `RuntimeError` nue : la taxonomie des erreurs n'y lisait aucun
statut amont et la servait au chemin (5), « Internal server error. ». Un 502 « source
amont indisponible » se lisait comme une panne d'oto, et le 400 « commune inconnue »
de `fr_aides_search` (rattrapé par un `except httpx.HTTPStatusError` que rien ne levait)
n'arrivait jamais à l'agent.
"""
import httpx
import pytest

from oto_mcp import error_taxonomy
from oto_mcp.fod import fr as fod_fr
from oto_mcp.fod import http as fod_http

_CHEMIN = "/api/fr/aides/search"


def _reponse(status: int, corps: bytes = b'{"detail": "le detail du service"}') -> httpx.Response:
    req = httpx.Request("POST", "http://10.42.0.7:8000" + _CHEMIN)
    return httpx.Response(status, request=req, content=corps)


@pytest.mark.parametrize("status", [400, 404, 429, 500, 502, 503, 504])
def test_le_refus_porte_son_statut_et_son_detail(status):
    with pytest.raises(fod_http.FodError) as e:
        fod_http._raise_for(_reponse(status))
    assert e.value.status_code == status
    assert e.value.detail == "le detail du service"
    assert isinstance(e.value, RuntimeError)        # les appelants existants tiennent


def test_un_succes_ne_leve_rien():
    assert fod_http._raise_for(_reponse(200, b"{}")) is None


@pytest.mark.parametrize("status,code", [
    (500, "upstream_5xx"), (502, "upstream_5xx"), (503, "upstream_5xx"),
    (504, "upstream_timeout"), (429, "rate_limited"), (404, "not_found"),
    (400, "upstream_4xx"),
])
def test_la_taxonomie_le_nomme_au_lieu_de_l_erreur_interne(status, code):
    with pytest.raises(fod_http.FodError) as e:
        fod_http._raise_for(_reponse(status))
    info = error_taxonomy.classify(e.value)
    assert info.code == code
    assert info.message != "Internal server error."
    assert "10.42.0" not in info.message


def test_aides_un_400_du_service_redevient_le_detail_actionnable(monkeypatch):
    """Commune inconnue du référentiel : le tool le traduit en INVALID_PARAMS."""
    def _post(path, body):
        fod_http._raise_for(_reponse(400, b'{"detail": "Commune INSEE 00000 inconnue"}'))
    monkeypatch.setattr(fod_fr, "_post", _post)
    with pytest.raises(ValueError, match="Commune INSEE 00000 inconnue"):
        fod_fr.search_aides(insee="00000")


def test_aides_une_panne_du_service_reste_un_refus_nomme(monkeypatch):
    def _post(path, body):
        fod_http._raise_for(_reponse(500, b'{"detail": "Internal Server Error"}'))
    monkeypatch.setattr(fod_fr, "_post", _post)
    with pytest.raises(fod_http.FodError) as e:
        fod_fr.search_aides(limit=10)
    assert error_taxonomy.classify(e.value).code == "upstream_5xx"
