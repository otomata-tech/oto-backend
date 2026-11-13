"""`fr_stock_enrich(sirets=[…])` : un lot d'ÉTABLISSEMENTS en un scan, avec leur tranche
d'effectif, son année et leur caractère employeur, une erreur par SIRET.

`fr_stock_enrich` ne prenait que des SIREN et ne rendait que le siège ; `fr_stock_siret`
ne prend qu'un SIRET par appel. Le service de données sert le lot
(`POST /api/sirene/sirets`) ; ce banc joue le VRAI outil, client FOD stubé à la forme de
cette route.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp.mcp_errors import McpError

_SIRET = "11111111100015"
_ROUTE = {
    "items": {_SIRET: {"siren": "111111111", "siret": _SIRET, "is_headquarters": False,
                       "street": "1 RUE A", "postal_code": "75008", "city": "PARIS",
                       "naf": "70.10Z", "denomination": None, "status": "active",
                       "tranche_effectifs": "12", "annee_effectifs": "2023",
                       "caractere_employeur": "O"}},
    "erreurs": [{"siret": "123", "code": "siret_invalide"},
                {"siret": "99999999999999", "code": "introuvable"}],
    "count": 1,
}


@pytest.fixture
def outil(monkeypatch):
    from fastmcp import FastMCP
    from oto_mcp.fod import client
    from oto_mcp.tools import fr_stock

    vu = {"sirets": [], "sirens": []}

    def lookup_sirets(sirets):
        vu["sirets"].append(list(sirets))
        return {"items": {k: dict(v) for k, v in _ROUTE["items"].items()},
                "erreurs": list(_ROUTE["erreurs"]), "count": 1}

    monkeypatch.setattr(client, "lookup_sirets", lookup_sirets)
    monkeypatch.setattr(client, "headquarters_addresses",
                        lambda sirens: vu["sirens"].append(list(sirens)) or {})
    monkeypatch.setattr(client, "lookup_unites_legales",
                        lambda sirens: {"111111111": {"denomination": "EXEMPLE SA",
                                                      "categorie_entreprise": "PME"}})
    m = FastMCP("t")
    fr_stock.register(m)
    t = asyncio.run(m.get_tool("fr_stock_enrich"))
    return t, vu


def test_un_lot_de_sirets_rend_effectif_employeur_et_erreurs(outil):
    t, vu = outil
    res = t.fn(sirets=[f" {_SIRET} ", "123", "99999999999999"])
    assert vu["sirets"] == [[_SIRET, "123", "99999999999999"]] and vu["sirens"] == []
    etab = res["items"][_SIRET]
    assert (etab["tranche_effectifs"], etab["annee_effectifs"], etab["caractere_employeur"]) \
        == ("12", "2023", "O")
    assert etab["raison_sociale"] == "EXEMPLE SA" and etab["categorie_entreprise"] == "PME"
    assert res["erreurs"] == _ROUTE["erreurs"] and res["count"] == 1


def test_les_sirens_gardent_leur_chemin(outil):
    t, vu = outil
    assert t.fn(sirens=["111111111"]) == {}
    assert vu["sirens"] == [["111111111"]] and vu["sirets"] == []


@pytest.mark.parametrize("kw", [{}, {"sirens": ["111111111"], "sirets": [_SIRET]}])
def test_exactement_un_des_deux(outil, kw):
    t, _ = outil
    with pytest.raises(McpError) as e:
        t.fn(**kw)
    assert e.value.error.data == {"code": "sirens_ou_sirets", "retryable": False}


def test_le_lot_est_borne_avant_l_appel(outil, monkeypatch):
    from oto_mcp.tools import fr_stock
    t, vu = outil
    monkeypatch.setattr(fr_stock, "SIRETS_MAX", 2)
    with pytest.raises(McpError) as e:
        t.fn(sirets=["11111111100015", "22222222200011", "33333333300017"])
    assert e.value.error.data == {"code": "sirets_lot_trop_grand", "retryable": False,
                                  "limit": 2, "count": 3}
    assert vu["sirets"] == []
    # Les doublons ne comptent qu'une fois.
    t.fn(sirets=["11111111100015", "11111111100015 ", "22222222200011"])
    assert len(vu["sirets"]) == 1


def test_le_schema_rend_les_deux_listes_optionnelles(outil):
    t, _ = outil
    props = t.parameters["properties"]
    assert {"sirens", "sirets"} <= set(props) and not t.parameters.get("required")


def test_le_client_appelle_la_route_du_service(monkeypatch):
    from oto_mcp.fod import client
    vu = {}
    monkeypatch.setattr(client, "_post", lambda path, body: vu.update(path=path, body=body) or {})
    client.lookup_sirets([_SIRET])
    assert vu == {"path": "/api/sirene/sirets", "body": {"sirets": [_SIRET]}}
