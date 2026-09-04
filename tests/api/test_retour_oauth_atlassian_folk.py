"""atlassian/folk — le retour OAuth ne perd plus le statut, le repli n'est plus
une chaîne littérale (oto-backend#670).

Avant ce lot, `_retour("connected", …)` et `_retour("error", …)` rendaient la
MÊME URL : un consentement réussi ne se distinguait pas d'un échec. Et le repli
(quand le tenant n'a pas de patron `connector_return`) était une f-string à
ACCOLADES DOUBLÉES — `f"{{_app_url()}}/?atlassian={{statut}}"` — une chaîne
LITTÉRALE, jamais une URL.
"""
from __future__ import annotations

import asyncio
import urllib.parse

import pytest
from starlette.requests import Request

from oto_mcp import links as links_module
from oto_mcp.api import atlassian as atlassian_routes
from oto_mcp.api import folk as folk_routes


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


def _endpoint(module, path: str):
    routes = module.make_routes(None, None, _json_response, _json_error, _options_handler)
    return next(r.endpoint for r in routes if r.path == path)


def _qs(url: str) -> dict:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)


def _location(response) -> str:
    return response.headers["location"]


def _oauth_module(module):
    return module.atlassian_oauth if module is atlassian_routes else module.folk_oauth


@pytest.mark.parametrize("module,path,connecteur", [
    (atlassian_routes, "/api/atlassian/oauth/callback", "atlassian"),
    (folk_routes, "/api/folkmcp/oauth/callback", "folk"),
])
def test_connecte_et_echec_produisent_desormais_des_urls_differentes(
        monkeypatch, module, path, connecteur):
    """LE défaut d'origine : `_retour("connected", …)` et `_retour("error", …)`
    rendaient la MÊME URL — un consentement réussi ne se distinguait pas d'un
    échec (oto-backend#670)."""
    monkeypatch.setattr(links_module, "link_for",
                        lambda *a, **k: f"https://app.oto.ninja/connectors?connector={connecteur}")
    oauth = _oauth_module(module)
    monkeypatch.setattr(oauth, "verify_state", lambda s: ("sub-1", "verifier"))
    monkeypatch.setattr(oauth, "persist_token", lambda *a, **k: None)
    handler = _endpoint(module, path)

    monkeypatch.setattr(oauth, "exchange_code", lambda *a, **k: {"access_token": "t"})
    ok = asyncio.run(handler(_get(path, "code=c&state=s")))

    def _boom(*a, **k):
        raise RuntimeError("refus du fournisseur")
    monkeypatch.setattr(oauth, "exchange_code", _boom)
    ko = asyncio.run(handler(_get(path, "code=c&state=s")))

    assert _location(ok) != _location(ko), "connecté et échec rendent encore la même URL"
    assert _qs(_location(ok))["connect"] == ["connected"]
    assert _qs(_location(ko))["connect"] == ["error"]
    # `connector=` était déjà servi : pur ajout, il ne bouge pas.
    assert _qs(_location(ok))["connector"] == [connecteur]


@pytest.mark.parametrize("module,path", [
    (atlassian_routes, "/api/atlassian/oauth/callback"),
    (folk_routes, "/api/folkmcp/oauth/callback"),
])
def test_le_repli_nest_plus_une_chaine_litterale(monkeypatch, module, path):
    """Avant ce lot : `f"{{_app_url()}}/?atlassian={{statut}}"` — une chaîne
    LITTÉRALE (accolades doublées), jamais une URL. Se déclenche quand le tenant
    n'a pas de patron `connector_return` (`links.link_for` rend `None`) — ici sur
    un state illisible, `verify_state` stubbé pour ne pas dépendre du secret HMAC
    réel (`OTO_MCP_OAUTH_STATE_SECRET`, hors de ce banc)."""
    monkeypatch.setattr(links_module, "link_for", lambda *a, **k: None)
    oauth = _oauth_module(module)
    monkeypatch.setattr(oauth, "verify_state", lambda s: None)
    handler = _endpoint(module, path)
    resp = asyncio.run(handler(_get(path, "state=bad")))
    url = _location(resp)
    assert "{" not in url and "}" not in url, f"encore une f-string cassée : {url!r}"
    parsed = urllib.parse.urlsplit(url)
    assert parsed.scheme and parsed.netloc, f"pas une URL valide : {url!r}"
    assert _qs(url)["connect"] == ["error"]
