"""Le consentement Google ramène au front qui l'a demandé — oto-backend#877, #670.

Un utilisateur venu d'un **front tiers** qui connectait Google atterrissait chez
nous après avoir consenti. Les quatre autres connecteurs OAuth le ramenaient au
bon front ; Google était le seul à ignorer la clé `app` que le front passe au
démarrage, et son callback composait son retour sur le front d'oto en dur.

Le callback arrive DEPUIS Google, sans en-tête d'authentification ni session : ce
que le state signé ne porte pas est définitivement perdu. C'est pourquoi le
correctif est au DÉPART (le state porte l'app) et pas au retour.

⚠️ Ces épreuves MONTENT le vrai handler. Sur le lot des retours OAuth, des
contrôles statiques ont laissé passer un import manquant qui faisait partir tout
retour de consentement en « échec », y compris les réussis : l'arbre ne dit rien
de l'exécution.
"""
from __future__ import annotations

import asyncio
import os
import urllib.parse

# Le state est signé : sans secret, rien ne se fabrique ni ne se relit. Posé à
# l'import, comme les autres bancs qui touchent aux states OAuth.
os.environ.setdefault("OTO_MCP_OAUTH_STATE_SECRET", "test-secret")

import pytest
from starlette.requests import Request

from oto_mcp.api import datastore as datastore_routes
from oto_mcp.auth import google as google_oauth


def _requete(query: str) -> Request:
    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request({"type": "http", "method": "GET",
                    "path": "/api/google/oauth/callback", "headers": [],
                    "query_string": query.encode(), "path_params": {}},
                   receive=_receive)


def _callback():
    def _json_response(_r, payload, status=200):
        return {"status": status, "body": payload}

    def _json_error(_r, status, code, message=None):
        return {"status": status, "error": code}

    async def _options(_r):
        return None

    routes = datastore_routes.make_routes(
        None, None, _json_response, _json_error, lambda o: {}, _options)
    return next(r.endpoint for r in routes
                if r.path == "/api/google/oauth/callback")


# --- le state porte le front, et seulement s'il est connu -------------------

def test_le_state_porte_le_front_demandeur():
    etat = google_oauth.make_state("sub-1", 42, "tulina")
    assert google_oauth.verify_state(etat) == ("sub-1", 42, "tulina")


def test_un_front_inconnu_est_reduit_a_rien():
    """Jamais de confiance à une valeur de client au-delà d'un lookup dans une
    liste fermée : signer une base arbitraire, ce serait signer une redirection
    ouverte."""
    etat = google_oauth.make_state("sub-1", 42, "https://attaquant.invalid")
    assert google_oauth.verify_state(etat) == ("sub-1", 42, "")


def test_un_state_emis_avant_ce_lot_reste_valide():
    """Ils vivent quelques minutes : il y en a en vol au déploiement. Les casser
    renverrait une erreur à quelqu'un qui vient d'autoriser correctement."""
    import base64
    import hashlib
    import hmac
    import json
    import time

    charge = json.dumps({"sub": "sub-1", "org": 42, "ts": int(time.time())},
                        separators=(",", ":")).encode()
    sig = hmac.new(google_oauth._state_secret(), charge, hashlib.sha256).digest()
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")  # noqa: E731
    assert google_oauth.verify_state(f"{b64(charge)}.{b64(sig)}") == ("sub-1", 42, "")


def test_le_flux_transmet_la_cle_cachee_du_front(monkeypatch):
    """La clé arrive par `values`, hors formulaire : le front sait qui il est,
    l'utilisateur n'a pas à la saisir. Google l'ignorait entièrement."""
    vu = {}
    monkeypatch.setattr(google_oauth, "build_auth_url",
                        lambda sub, return_app="": vu.update(app=return_app) or "https://x")

    class _Ctx:
        sub = "sub-1"

    google_oauth._start_flow(_Ctx(), {"app": "tulina"})
    assert vu["app"] == "tulina"


def test_l_url_de_consentement_filtre_le_front_avant_de_signer(monkeypatch):
    """Le point de validation est `build_auth_url`, AVANT `make_state` — pas plus
    tard. Un banc voisin garde déjà contre un `sub` venu de l'appelant (« sinon on
    signerait un état pour un tiers ») ; `app` vient de l'appelant lui aussi, et
    doit être réduit à la liste fermée au même endroit.

    Sans ce filtrage, le state signerait une base de redirection arbitraire : une
    redirection ouverte, avec notre signature dessus."""
    monkeypatch.setattr(google_oauth, "_ctx_org", lambda sub: 42)
    monkeypatch.setattr(google_oauth, "_client_id", lambda: "cid")
    monkeypatch.setattr(google_oauth, "_redirect_uri", lambda: "https://oto/cb")

    url = google_oauth.build_auth_url("sub-1", "https://attaquant.invalid")
    etat = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
    assert google_oauth.verify_state(etat)[2] == "", (
        "une valeur hors de la liste fermée ne doit jamais entrer dans le state")

    url_ok = google_oauth.build_auth_url("sub-1", "tulina")
    etat_ok = urllib.parse.parse_qs(urllib.parse.urlsplit(url_ok).query)["state"][0]
    assert google_oauth.verify_state(etat_ok)[2] == "tulina"


# --- le retour, sur le vrai handler ----------------------------------------

@pytest.fixture
def _echange_ok(monkeypatch):
    monkeypatch.setattr(google_oauth, "exchange_code", lambda code: {"access_token": "t"})
    monkeypatch.setattr(google_oauth, "persist_token", lambda *a, **k: None)


def test_le_callback_ramene_au_front_tiers(_echange_ok):
    etat = google_oauth.make_state("sub-1", 42, "tulina")
    reponse = asyncio.run(_callback()(_requete(f"code=c&state={etat}")))
    url = reponse.headers["location"]
    assert url.startswith("https://app.tulina.ai/"), url
    assert "connect=connected" in url, "la convention de retour de #670 doit tenir"


def test_le_callback_sans_front_declare_ramene_sur_une_route_QUI_EXISTE(_echange_ok):
    """Le défaut passe par le fabricant partagé, comme les quatre autres.

    Il composait `/console/connectors` à la main — un chemin absent du dashboard
    (mesuré le 04/09 : ses routes sont `/connectors`, `/my-connectors`,
    `/library/connectors`, `/org/connectors`, `/team/connectors`). Un consentement
    réussi déposait donc la personne sur un 404 : panne silencieuse, puisque
    l'autorisation avait bien eu lieu. Le test d'avant figeait ce chemin mort —
    un banc peut protéger une erreur aussi bien qu'une garantie."""
    etat = google_oauth.make_state("sub-1", 42)
    url = asyncio.run(_callback()(_requete(f"code=c&state={etat}"))).headers["location"]
    chemin = urllib.parse.urlsplit(url).path
    assert chemin == "/connectors", chemin
    assert "console" not in url and "tulina" not in url
    assert "connect=connected" in url


def test_un_echec_ramene_AUSSI_au_front_tiers(monkeypatch):
    """Le piège du retour OAuth : on soigne le succès et on oublie l'échec, donc
    la personne est renvoyée chez nous au pire moment — quand elle doit
    recommencer."""
    def _boom(code):
        raise RuntimeError("Google a refusé")

    monkeypatch.setattr(google_oauth, "exchange_code", _boom)
    etat = google_oauth.make_state("sub-1", 42, "tulina")
    url = asyncio.run(_callback()(_requete(f"code=c&state={etat}"))).headers["location"]
    assert url.startswith("https://app.tulina.ai/"), url
    assert "connect=error" in url


def test_un_state_illisible_ne_peut_que_retomber_sur_le_defaut():
    """Limite ASSUMÉE, écrite ici pour qu'on ne la prenne pas pour un oubli : si
    le state ne se relit pas, on ignore d'où vient la personne. Il n'existe aucun
    autre porteur — le callback vient de Google, sans en-tête ni session."""
    url = asyncio.run(_callback()(_requete("code=c&state=nimportequoi"))).headers["location"]
    assert urllib.parse.urlsplit(url).path == "/connectors"
    assert "connect=error" in url


def test_les_deux_fronts_ne_partagent_que_le_suffixe(_echange_ok):
    """La convention de #670 est le SUFFIXE ; la base et le chemin viennent du
    front. Vérifier les deux ensemble évite de croire qu'un seul suffit."""
    q = urllib.parse.urlsplit
    tiers = asyncio.run(_callback()(_requete(
        f"code=c&state={google_oauth.make_state('s', 42, 'tulina')}"))).headers["location"]
    nous = asyncio.run(_callback()(_requete(
        f"code=c&state={google_oauth.make_state('s', 42)}"))).headers["location"]
    assert q(tiers).netloc != q(nous).netloc, "la base doit suivre le front"
    assert "connect=connected" in tiers and "connect=connected" in nous
