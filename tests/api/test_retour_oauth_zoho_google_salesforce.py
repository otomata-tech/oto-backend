"""zoho/google/salesforce — la convention unique de retour OAuth
(`?connector=<nom>&connect=connected|error|forbidden`, oto-backend#670) au
niveau de la ROUTE (callback réel, pas la fabrique seule).

- **zoho** et **google** (`api/datastore.py`) doublaient un temps l'ancienne
  forme lue par le dashboard (`?zoho=connected`, `?google=connected`) — retirée
  le 17/09/2026, sans préavis, faute de lecteur mesuré. Ces bancs vérifient
  désormais son ABSENCE, et rougissent si quelqu'un la remet.
- **salesforce** est déjà la forme cible : test de non-régression après le
  passage au fabricant partagé (`auth.flow.connector_return_url`).
"""
from __future__ import annotations

import asyncio
import urllib.parse

import pytest
from starlette.requests import Request
from starlette.responses import RedirectResponse

from oto_mcp.api import datastore as datastore_routes
from oto_mcp.api import salesforce as salesforce_routes
from oto_mcp.api import zoho as zoho_routes


def _json_response(_request, payload, status=200):
    return {"status": status, "body": payload}


def _json_error(_request, status, code, message=None):
    return {"status": status, "error": code, "detail": message}


async def _options_handler(_request):
    return None


def _get(path: str, query: str = "") -> Request:
    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    return Request({"type": "http", "method": "GET", "path": path,
                    "headers": [], "query_string": query.encode(), "path_params": {}},
                   receive=_receive)


def _endpoint(module, path: str, *, with_cors: bool = False):
    args = [None, None, _json_response, _json_error]
    if with_cors:
        args.append(lambda origin: {})
    args.append(_options_handler)
    routes = module.make_routes(*args)
    return next(r.endpoint for r in routes if r.path == path)


def _qs(url: str) -> dict:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)


def _location(response) -> str:
    return response.headers["location"]


# --- zoho : l'ancienne forme et la nouvelle, DANS LA MÊME redirection ----------

ZOHO_CALLBACK = "/api/zoho/oauth/callback"


def test_zoho_state_illisible_double_zoho_error(monkeypatch):
    monkeypatch.setattr(zoho_routes.zoho_oauth, "verify_state", lambda s: None)
    handler = _endpoint(zoho_routes, ZOHO_CALLBACK)
    resp = asyncio.run(handler(_get(ZOHO_CALLBACK, "state=bad")))
    q = _qs(_location(resp))
    assert q.get("zoho") is None, "ancienne forme réapparue — elle est retirée depuis le 17/09"
    assert q["connector"] == ["zoho"] and q["connect"] == ["error"]


def test_zoho_succes_ne_sert_que_la_forme_neuve(monkeypatch):
    """Succès sur `zohodesk` (pas `zoho`) : seule la forme neuve est servie,
    portant le connecteur RÉEL."""
    parsed = {"sub": "u1", "org": 5, "connector": "zohodesk",
             "data_center": "eu", "return_app": ""}
    monkeypatch.setattr(zoho_routes.zoho_oauth, "verify_state", lambda s: parsed)
    monkeypatch.setattr(zoho_routes.zoho_oauth, "app_fields", lambda *a, **k: {})
    monkeypatch.setattr(zoho_routes.zoho_oauth, "exchange_code",
                        lambda *a, **k: {"refresh_token": "rt"})
    monkeypatch.setattr(zoho_routes.zoho_oauth, "persist", lambda *a, **k: None)
    handler = _endpoint(zoho_routes, ZOHO_CALLBACK)
    resp = asyncio.run(handler(_get(ZOHO_CALLBACK, "code=c&state=s")))
    q = _qs(_location(resp))
    assert q.get("zohodesk") is None, "ancienne forme réapparue — elle est retirée depuis le 17/09"
    assert q["connector"] == ["zohodesk"] and q["connect"] == ["connected"]


def test_zoho_echange_echoue_ne_sert_que_la_forme_neuve(monkeypatch):
    """La forme neuve porte le connecteur réellement visé, y compris pour
    zohodesk/zohoanalytics ; plus aucune ancienne forme à côté."""
    parsed = {"sub": "u1", "org": 5, "connector": "zohoanalytics",
             "data_center": "eu", "return_app": ""}
    monkeypatch.setattr(zoho_routes.zoho_oauth, "verify_state", lambda s: parsed)
    monkeypatch.setattr(zoho_routes.zoho_oauth, "app_fields", lambda *a, **k: {})

    def _boom(*a, **k):
        raise RuntimeError("refus du fournisseur")
    monkeypatch.setattr(zoho_routes.zoho_oauth, "exchange_code", _boom)
    handler = _endpoint(zoho_routes, ZOHO_CALLBACK)
    resp = asyncio.run(handler(_get(ZOHO_CALLBACK, "code=c&state=s")))
    q = _qs(_location(resp))
    assert q.get("zoho") is None, "ancienne forme réapparue — elle est retirée depuis le 17/09"
    assert q["connector"] == ["zohoanalytics"] and q["connect"] == ["error"]


# --- google (api/datastore.py) : forme neuve seule, échec enfin redirigé -----

GOOGLE_CALLBACK = "/api/google/oauth/callback"


def test_google_succes_ne_sert_que_la_forme_neuve(monkeypatch):
    monkeypatch.setattr(datastore_routes.google_oauth, "verify_state", lambda s: ("sub-1", 7, "", "google"))
    monkeypatch.setattr(datastore_routes.google_oauth, "exchange_code",
                        lambda code, sub: {"access_token": "a", "refresh_token": "r"})
    monkeypatch.setattr(datastore_routes.google_oauth, "persist_token",
                        lambda sub, org, tok: "e@x.io")
    handler = _endpoint(datastore_routes, GOOGLE_CALLBACK, with_cors=True)
    resp = asyncio.run(handler(_get(GOOGLE_CALLBACK, "code=c&state=s")))
    q = _qs(_location(resp))
    assert q.get("google") is None, "ancienne forme réapparue — elle est retirée depuis le 17/09"
    assert q["connector"] == ["google"] and q["connect"] == ["connected"]


@pytest.mark.parametrize("brise", ["absent", "illisible", "echange"])
def test_google_echec_redirige_desormais_au_lieu_dun_json_brut(monkeypatch, brise):
    """Avant ce lot, ces trois cas rendaient un JSON brut (400/400/502) — aucune
    redirection du tout. Pur ajout : rien n'existait à doubler à cette place."""
    handler = _endpoint(datastore_routes, GOOGLE_CALLBACK, with_cors=True)
    if brise == "absent":
        req = _get(GOOGLE_CALLBACK, "")
    elif brise == "illisible":
        monkeypatch.setattr(datastore_routes.google_oauth, "verify_state", lambda s: None)
        req = _get(GOOGLE_CALLBACK, "code=c&state=bad")
    else:
        monkeypatch.setattr(datastore_routes.google_oauth, "verify_state", lambda s: ("sub-1", 7, "", "google"))

        def _boom(code, sub):
            raise RuntimeError("refus du fournisseur")
        monkeypatch.setattr(datastore_routes.google_oauth, "exchange_code", _boom)
        req = _get(GOOGLE_CALLBACK, "code=c&state=s")

    resp = asyncio.run(handler(req))
    assert isinstance(resp, RedirectResponse), (
        f"toujours un JSON brut, pas une redirection (oto-backend#670) : {resp!r}")
    q = _qs(_location(resp))
    assert q["connector"] == ["google"] and q["connect"] == ["error"]
    assert q.get("google") is None, "rien à doubler ici : l'échec ne redirigeait jamais avant"


# --- salesforce : déjà la forme cible, non-régression après le refactor -------

def test_salesforce_conserve_sa_forme_apres_le_passage_au_fabricant_partage(monkeypatch):
    monkeypatch.setattr(salesforce_routes.salesforce_oauth, "verify_state", lambda s: None)
    handler = _endpoint(salesforce_routes, "/api/salesforce/oauth/callback")
    resp = asyncio.run(handler(_get("/api/salesforce/oauth/callback", "state=bad")))
    q = _qs(_location(resp))
    assert q["connector"] == ["salesforce"] and q["connect"] == ["error"]
    assert "salesforce" not in q, "salesforce n'a jamais servi de clé `salesforce=`"
