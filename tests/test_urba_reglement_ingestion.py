"""`urba_reglement` : un règlement absent du cache se demande au service (`ingest=true`),
son état se relit, et chaque impasse a son refus nommé.

Le service FOD ingère à la demande (`POST|GET /api/reglements/{idurba}/ingestion`) :
une ingestion à la fois (503 + `Retry-After`), 80 Mo par pièce, 600 s. Ce que ces tests
tiennent :
- **par défaut, rien n'est lancé** : un cache-miss relit l'état (GET), jamais un POST ;
- `ingest=true` lance (POST) et rend `en_cours` avec le délai de relecture ;
- prêt entre-temps : les extraits, pas l'état ;
- les refus `reglement_service_occupe`, `reglement_trop_volumineux`,
  `reglement_ingestion_echec`, `reglement_introuvable` ;
- le client parle aux bonnes routes, sans reprise sur le 503 d'état, et aucun échec ne
  sert l'adresse interne du service.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import oto_mcp.fod.reglement  # noqa: F401 — cible du monkeypatch (import à l'appel)
from oto_mcp.mcp_errors import McpError

_ID = "80021_PLU_20251211"
_EXTRAITS = {"idurba": _ID, "cached": True, "recherche": "UA", "extraits": ["…"]}
_ABSENT = {"idurba": _ID, "cached": False, "statut": "absent", "message": "POST …"}


def _tool():
    from fastmcp import FastMCP
    from oto_mcp.tools import urba as U

    m = FastMCP("t")
    U.register(m)
    return asyncio.run(m.get_tool("urba_reglement")).fn


@pytest.fixture
def service(monkeypatch):
    """Le service simulé : `cache` successifs pour les extraits, `etat` pour l'ingestion."""
    vu = {"extraits": 0, "ingestion": [], "cache": [_ABSENT], "etat": (200, {}, None)}

    def extraits(idurba, **kw):
        vu["extraits"] += 1
        return vu["cache"].pop(0) if len(vu["cache"]) > 1 else vu["cache"][0]

    def ingestion(idurba, *, lancer):
        vu["ingestion"].append(lancer)
        return vu["etat"]

    monkeypatch.setattr("oto_mcp.fod.reglement.extraits", extraits)
    monkeypatch.setattr("oto_mcp.fod.reglement.ingestion", ingestion)
    return vu


def test_en_cache_les_extraits_sans_toucher_a_l_ingestion(service):
    service["cache"] = [_EXTRAITS]
    assert _tool()(idurba=_ID, zone="UA", ingest=True) == _EXTRAITS
    assert service["ingestion"] == []


def test_absent_sans_ingest_relit_l_etat_et_dit_comment_lancer(service):
    service["etat"] = (200, {"idurba": _ID, "statut": "absent"}, None)
    r = _tool()(idurba=_ID)
    assert service["ingestion"] == [False]
    assert r["statut"] == "absent" and r["cached"] is False
    assert "ingest=true" in r["message"] and "POST" not in r["message"]


def test_ingest_lance_et_rend_en_cours_avec_le_delai(service):
    service["etat"] = (202, {"idurba": _ID, "statut": "en_cours",
                             "depuis": "2026-11-13T10:00:00+00:00"}, 20)
    r = _tool()(idurba=_ID, ingest=True)
    assert service["ingestion"] == [True]
    assert r == {"idurba": _ID, "cached": False, "statut": "en_cours",
                 "depuis": "2026-11-13T10:00:00+00:00", "retry_after_seconds": 20,
                 "message": r["message"]}
    assert "same arguments" in r["message"]


@pytest.mark.parametrize("statut", ["pret", "scanne"])
def test_pret_entre_temps_rend_les_extraits(service, statut):
    service["cache"] = [_ABSENT, _EXTRAITS]
    service["etat"] = (200, {"idurba": _ID, "statut": statut}, None)
    assert _tool()(idurba=_ID, ingest=True) == _EXTRAITS
    assert service["extraits"] == 2


def _refus(service, **kw):
    with pytest.raises(McpError) as e:
        _tool()(idurba=_ID, **kw)
    return e.value.error


def test_service_occupe_est_nomme_et_rejouable(service):
    service["etat"] = (503, {"detail": "1 ingestions déjà en cours"}, 60)
    err = _refus(service, ingest=True)
    assert err.data == {"code": "reglement_service_occupe", "retryable": True,
                        "retry_after_seconds": 60}


def test_trop_volumineux_est_nomme(service):
    erreur = "ReglementTropVolumineux: 112 Mo > 80 Mo"
    service["etat"] = (200, {"idurba": _ID, "statut": "echec", "erreur": erreur,
                             "relance_possible_apres": "2026-11-13T10:10:00+00:00"}, None)
    err = _refus(service, ingest=True)
    assert err.data == {"code": "reglement_trop_volumineux", "retryable": False,
                        "erreur": erreur}
    assert "batch" in err.message


def test_echec_d_ingestion_est_nomme_avec_sa_cause(service):
    service["etat"] = (200, {"idurba": _ID, "statut": "echec", "erreur": "DelaiDepasse: délai",
                             "relance_possible_apres": "2026-11-13T10:10:00+00:00"}, None)
    err = _refus(service)          # même sans ingest : l'état relu est un échec
    assert err.data == {"code": "reglement_ingestion_echec", "retryable": False,
                        "erreur": "DelaiDepasse: délai",
                        "relance_possible_apres": "2026-11-13T10:10:00+00:00"}
    assert "DelaiDepasse" in err.message


def test_document_introuvable_nomme_les_autres(service):
    service["etat"] = (404, {"idurba": _ID, "statut": "introuvable", "detail": "remplacé",
                             "autres_documents": ["80021_PLU_20260101"]}, None)
    err = _refus(service, ingest=True)
    assert err.data == {"code": "reglement_introuvable", "retryable": False,
                        "autres_documents": ["80021_PLU_20260101"]}


def test_un_etat_inconnu_leve(service):
    service["etat"] = (200, {"idurba": _ID, "statut": "nouveau"}, None)
    with pytest.raises(RuntimeError, match="nouveau"):
        _tool()(idurba=_ID)


def test_le_parametre_est_au_schema_et_faux_par_defaut():
    from fastmcp import FastMCP
    from oto_mcp.tools import urba as U

    m = FastMCP("t")
    U.register(m)
    prop = asyncio.run(m.get_tool("urba_reglement")).parameters["properties"]["ingest"]
    assert prop["type"] == "boolean" and prop["default"] is False


def test_ingest_se_declare_en_ecriture():
    from oto_mcp.tools import effets
    assert effets.effet("urba_reglement", {"idurba": _ID})[0] == "lire"
    assert effets.effet("urba_reglement", {"idurba": _ID, "ingest": True})[0] == "ecrire"


# ── le client ──────────────────────────────────────────────────────────────

_HOTE = "http://10.42.0.7:8000"


@pytest.fixture
def transport(monkeypatch):
    from oto_mcp.fod import http as fod_http
    vu = {"appels": [], "reponse": (202, {"statut": "en_cours"}, {"Retry-After": "20"})}

    def handler(request):
        vu["appels"].append((request.method, request.url.path))
        code, corps, entetes = vu["reponse"]
        return httpx.Response(code, json=corps, headers=entetes)

    monkeypatch.setattr(fod_http, "_client",
                        httpx.Client(base_url=_HOTE, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(fod_http, "_BASE", _HOTE)
    monkeypatch.setattr(fod_http, "_TOKEN", "t")
    return vu


def test_le_client_lance_par_post_et_relit_par_get(transport):
    from oto_mcp.fod import reglement
    assert reglement.ingestion(_ID, lancer=True) == (202, {"statut": "en_cours"}, 20)
    reglement.ingestion(_ID, lancer=False)
    chemin = f"/api/reglements/{_ID}/ingestion"
    assert transport["appels"] == [("POST", chemin), ("GET", chemin)]


def test_le_503_d_etat_n_est_pas_repris(transport):
    from oto_mcp.fod import reglement
    transport["reponse"] = (503, {"detail": "occupé"}, {"Retry-After": "60"})
    assert reglement.ingestion(_ID, lancer=True) == (503, {"detail": "occupé"}, 60)
    assert len(transport["appels"]) == 1


def test_une_panne_ne_sert_pas_l_adresse_interne(transport):
    from oto_mcp.fod import reglement
    transport["reponse"] = (500, {"detail": f"boom {_HOTE}/x"}, {})
    with pytest.raises(RuntimeError) as e:
        reglement.ingestion(_ID, lancer=True)
    assert "10.42.0" not in str(e.value)


def test_les_extraits_passent_par_la_plomberie_commune(transport):
    from oto_mcp.fod import reglement
    transport["reponse"] = (500, {"detail": "x"}, {})
    with pytest.raises(RuntimeError) as e:
        reglement.extraits(_ID, zone="UA")
    assert "10.42.0" not in str(e.value) and "/api/reglement" in str(e.value)
