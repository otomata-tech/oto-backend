"""fr_avis_sirene (#188) : URL du PDF officiel « Avis de situation SIRENE » d'un
établissement, endpoint public INSEE (sans clé). Valide le SIRET (14 chiffres,
espaces/points ignorés) et vérifie la disponibilité par un HEAD (réseau stubé)."""
from __future__ import annotations

import pytest
from mcp.shared.exceptions import McpError


class _Reg:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        if a and callable(a[0]):
            return deco(a[0])
        return deco


class _Noop:
    def __init__(self, *a, **k): ...


class _Resp:
    def __init__(self, status, ctype="application/pdf"):
        self.status_code = status
        self.headers = {"Content-Type": ctype}


@pytest.fixture()
def avis(monkeypatch):
    monkeypatch.setattr("oto.tools.sirene.EntreprisesClient", _Noop)
    monkeypatch.setattr("oto.tools.sirene.SireneClient", _Noop)
    monkeypatch.setattr("oto.tools.inpi.InpiClient", _Noop)
    monkeypatch.setattr("oto.tools.bodacc.BodaccClient", _Noop)
    monkeypatch.setattr("france_opendata.EgaproClient", _Noop)
    from oto_mcp.tools import fr
    reg = _Reg()
    fr.register(reg)
    return reg.tools["fr_avis_sirene"]


def _head(status, ctype="application/pdf"):
    def _f(url, timeout=None):
        _f.url = url
        return _Resp(status, ctype)
    return _f


def test_valid_siret_returns_url(avis, monkeypatch):
    h = _head(200)
    monkeypatch.setattr("requests.head", h)
    out = avis("81760723700028")
    assert out == {"siret": "81760723700028", "format": "pdf",
                   "url": "https://api-avis-situation-sirene.insee.fr/identification/pdf/81760723700028"}
    assert h.url.endswith("/81760723700028")


def test_spaces_and_dots_normalized(avis, monkeypatch):
    monkeypatch.setattr("requests.head", _head(200))
    out = avis("817 607 237 00028")
    assert out["siret"] == "81760723700028"


def test_bad_length_raises(avis):
    with pytest.raises(McpError):
        avis("12345")


def test_404_raises_actionable(avis, monkeypatch):
    monkeypatch.setattr("requests.head", _head(404, "application/json"))
    with pytest.raises(McpError):
        avis("00000000000000")


def test_200_non_pdf_raises(avis, monkeypatch):
    monkeypatch.setattr("requests.head", _head(200, "text/html"))
    with pytest.raises(McpError):
        avis("81760723700028")
