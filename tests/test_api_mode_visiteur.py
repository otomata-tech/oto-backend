"""Le mode `visitor` du connecteur `api`, de bout en bout : les VRAIS outils, le VRAI
client d'oto-core, le VRAI coffre (base jetable), contre un faux site local.

Le site fictif (`site.example.test`) n'a pas de compte : sa page d'entrée pose un
premier cookie et redirige vers l'accueil, qui en pose un second ; ses données ne se
lisent qu'avec les deux (401 sinon). Un autre hôte du même domaine
(`relais.example.test`) sert à éprouver la garde de sortie à chaque saut. Les noms
d'hôte mènent à la boucle locale pour la connexion, et à une adresse publique pour la
garde. Aucun site réel.
"""
from __future__ import annotations

import asyncio
import base64
import http.server
import json
import socket
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastmcp import FastMCP

from oto_mcp import access, credentials_store, egress, error_taxonomy
from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import api as H
from oto_mcp.tools import effets

COMPTE = "site"
LIGNE = ("org", "1", COMPTE)
SITE, RELAIS, AILLEURS = "site.example.test", "relais.example.test", "ailleurs.test"
_KEY = base64.b64encode(b"\x5b" * 32).decode()


class Etat:
    def __init__(self):
        self.visites: list[str] = []
        self.lectures = 0
        self.envois: list[dict] = []
        self.sessions: set[str] = set()
        self.emises = 0
        self.sans_cookie = False
        self.refuse_toujours = False
        self.accueil = "/accueil"


def _cookies(entetes) -> dict:
    out = {}
    for part in (entetes.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k:
            out[k] = v
    return out


def _site(etat: Etat):
    class Gestion(http.server.BaseHTTPRequestHandler):
        def _json(self, code, corps, entetes=()):
            data = json.dumps(corps).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in entetes:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _ouverte(self) -> bool:
            c = _cookies(self.headers)
            return (not etat.refuse_toujours and c.get("VISITE") in etat.sessions
                    and c.get("ACCUEIL") == "oui")

        def do_GET(self):
            p = urlsplit(self.path)
            if p.path == "/entree":
                etat.visites.append(self.path)
                if parse_qs(p.query).get("lang") != ["fr"]:
                    return self._json(400, {"error": "lang"})
                self.send_response(302)
                self.send_header("Location", etat.accueil)
                self.send_header("Content-Length", "0")
                if not etat.sans_cookie:
                    etat.emises += 1
                    jeton = f"v-{etat.emises}"
                    etat.sessions.add(jeton)
                    self.send_header("Set-Cookie", f"VISITE={jeton}; Path=/; HttpOnly")
                return self.end_headers()
            if p.path == "/accueil":
                entetes = () if etat.sans_cookie else (
                    ("Set-Cookie", "ACCUEIL=oui; Path=/"),)
                return self._json(200, {"page": "accueil"}, entetes)
            if p.path == "/api/prix":
                etat.lectures += 1
                if self._ouverte():
                    return self._json(200, {"prix": [12, 34]})
                return self._json(401, {"message": "session required"})
            if p.path == "/api/basique":
                return self._json(200, {"autorisation": self.headers.get("Authorization")})
            self._json(404, {})

        def do_POST(self):
            corps = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path != "/api/recherche":
                return self._json(404, {})
            if not self._ouverte():
                return self._json(401, {"message": "session required"})
            etat.envois.append(corps)
            self._json(200, {"trouve": corps})

        def log_message(self, *a):
            pass

    return Gestion


def _serveur(gestion):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), gestion)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


ETAPES = {"login": {"path": "/entree", "method": "GET", "query": {"lang": "fr"}},
          "domain": "example.test"}


@pytest.fixture
def banc(live, monkeypatch):
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", _KEY)
    monkeypatch.delenv(egress.ALLOW_VAR, raising=False)
    internes: set[str] = set()
    monkeypatch.setattr(egress, "resolved_addresses",
                        lambda hote, port: {"10.0.0.7"} if hote in internes
                        else {"93.184.216.34"})
    vrai = socket.getaddrinfo

    def resoudre(hote, *a, **k):
        if isinstance(hote, str) and hote in (SITE, RELAIS, AILLEURS):
            hote = "127.0.0.1"
        return vrai(hote, *a, **k)
    monkeypatch.setattr(socket, "getaddrinfo", resoudre)

    etat = Etat()
    srv, port = _serveur(_site(etat))
    base = f"http://{SITE}:{port}"

    def poser(champs):
        credentials_store.set_credential(*LIGNE[:2], "api",
                                         credentials_store.pack_secret("api", champs),
                                         account=COMPTE)
        carte["champs"] = champs

    carte: dict = {}
    poser({"base_url": base, "auth_mode": "visitor", "session": json.dumps(ETAPES)})

    def rc():
        return SimpleNamespace(fields=dict(carte["champs"]), entity_type=LIGNE[0],
                               entity_id=LIGNE[1], account=COMPTE, provider="api")

    monkeypatch.setattr(H, "current_user_sub_from_token", lambda: "u-1")
    monkeypatch.setattr(H, "_resolve", rc)
    monkeypatch.setattr(access, "resolve_credential", lambda *a, **k: rc())
    m = FastMCP("t")
    H.register(m)
    outil = lambda nom: asyncio.run(m.get_tool(nom)).fn  # noqa: E731

    yield SimpleNamespace(etat=etat, outil=outil, port=port, base=base,
                          internes=internes, poser=poser, carte=carte)
    srv.shutdown()
    credentials_store.clear_credential(*LIGNE[:2], "api", account=COMPTE)


def _jar() -> str:
    clair = credentials_store.unseal_satellite(*LIGNE[:2], "api", COMPTE, "session_jar")
    return json.loads(clair)["jar"]


# --- la visite, le jar ---------------------------------------------------------------

def test_la_visite_pose_les_cookies_puis_la_lecture_les_porte(banc):
    out = banc.outil("api_get")(path="/api/prix")

    assert out == {"prix": [12, 34]}
    assert banc.etat.visites == ["/entree?lang=fr"]
    assert "v-1" not in json.dumps(out) and "VISITE" not in json.dumps(out)


def test_le_jar_est_scelle_puis_relu_sans_nouvelle_visite(banc):
    banc.outil("api_get")(path="/api/prix")
    from oto_mcp.db import _connect
    with _connect() as c:
        meta = c.execute(
            "SELECT meta FROM connector_credentials WHERE entity_type=%s AND entity_id=%s "
            "AND connector='api' AND account=%s", LIGNE).fetchone()["meta"]
    assert "session_jar_enc" in meta and "v-1" not in json.dumps(meta)
    assert "v-1" in _jar() and "ACCUEIL" in _jar()

    assert banc.outil("api_get")(path="/api/prix") == {"prix": [12, 34]}
    assert len(banc.etat.visites) == 1


def test_un_401_relance_une_visite_et_un_seul_rejeu(banc):
    banc.outil("api_get")(path="/api/prix")
    banc.etat.sessions.clear()                       # le site a oublié le visiteur

    assert banc.outil("api_get")(path="/api/prix") == {"prix": [12, 34]}
    assert len(banc.etat.visites) == 2
    assert "v-2" in _jar()


def test_un_jar_d_une_autre_carte_n_est_pas_servi(banc):
    """L'empreinte couvre les étapes : la carte vue par l'appel (ici, une page d'entrée
    changée sans repose, comme un appel en cours pendant la repose) ne reprend pas le
    jar scellé pour l'ancienne — elle refait une visite."""
    banc.outil("api_get")(path="/api/prix")
    autres = {**ETAPES, "login": {**ETAPES["login"], "query": {"lang": "fr", "v": "2"}}}
    banc.carte["champs"] = {**_carte(banc), "session": json.dumps(autres)}
    assert banc.outil("api_get")(path="/api/prix") == {"prix": [12, 34]}
    assert banc.etat.visites == ["/entree?lang=fr", "/entree?lang=fr&v=2"]


def _carte(banc) -> dict:
    return {"base_url": banc.base, "auth_mode": "visitor", "session": json.dumps(ETAPES)}


# --- les refus nommés ----------------------------------------------------------------

def test_refuse_encore_apres_une_visite_neuve_est_un_refus_nomme_pas_une_cle(banc):
    banc.outil("api_get")(path="/api/prix")
    banc.etat.refuse_toujours = True

    with pytest.raises(McpError) as pris:
        banc.outil("api_get")(path="/api/prix")
    assert pris.value.error.data == {"code": "api_visitor_refused", "retryable": False}
    assert "api_visitor_refused" in pris.value.error.message
    assert not error_taxonomy.credential_rejected_in_chain(pris.value)
    assert len(banc.etat.visites) == 2


def test_une_visite_sans_cookie_est_refusee_par_nom(banc):
    banc.etat.sans_cookie = True
    with pytest.raises(McpError) as pris:
        banc.outil("api_get")(path="/api/prix")
    assert pris.value.error.data["code"] == "api_visitor_refused"
    assert not error_taxonomy.credential_rejected_in_chain(pris.value)
    assert banc.etat.lectures == 0


def test_un_saut_vers_un_hote_etranger_est_refuse_avant_l_envoi(banc):
    banc.etat.accueil = f"http://{AILLEURS}:{banc.port}/accueil"
    with pytest.raises(McpError) as pris:
        banc.outil("api_get")(path="/api/prix")
    assert pris.value.error.data["code"] == "api_session_hop_refused"
    assert AILLEURS in str(pris.value)
    assert banc.etat.lectures == 0


def test_la_garde_de_sortie_voit_chaque_saut_de_la_visite(banc):
    """Un hôte du domaine déclaré qui résout vers une adresse interne : refusé avant
    l'envoi, alors que la page d'entrée, elle, a été visitée."""
    banc.etat.accueil = f"http://{RELAIS}:{banc.port}/accueil"
    banc.internes.add(RELAIS)
    with pytest.raises(McpError) as pris:
        banc.outil("api_get")(path="/api/prix")
    assert "session" in str(pris.value)
    assert banc.etat.visites == ["/entree?lang=fr"] and banc.etat.lectures == 0


# --- la carte ------------------------------------------------------------------------

@pytest.mark.parametrize("etapes, attendu", [
    ({"login": {"path": "/entree"}}, "session.login.method"),
    ({"login": {"path": "/entree", "method": "POST"}}, "session.login.method"),
    ({"login": {"path": "/entree", "method": "GET"}, "follow": {"field": "u"}}, "follow"),
    ({"login": {"path": "/entree", "method": "GET", "user_field": "u"}}, "user_field"),
])
def test_des_etapes_invalides_sont_refusees_avant_tout_envoi(banc, etapes, attendu):
    banc.poser({**_carte(banc), "session": json.dumps(etapes)})
    with pytest.raises(McpError) as pris:
        banc.outil("api_get")(path="/api/prix")
    assert attendu in str(pris.value)
    assert banc.etat.visites == [] and banc.etat.lectures == 0


# --- l'écriture : le même juge qu'aujourd'hui ----------------------------------------

def test_un_post_en_mode_visiteur_part_et_reste_juge_envoyer(banc):
    """Pas de lecture seule pour le visiteur (aucun mot de passe en jeu) — mais aucune
    exception non plus : le juge des effets ne connaît pas le mode, un POST envoie."""
    out = banc.outil("api_post")(path="/api/recherche", body={"depart": "A"})
    assert out == {"trouve": {"depart": "A"}}
    assert banc.etat.envois == [{"depart": "A"}]

    assert effets.effet("api_post", {"path": "/api/recherche"})[0] == "envoyer"
    assert effets.effet("api_act", {"op": "request", "method": "POST",
                                    "path": "/api/recherche"})[0] == "envoyer"
    assert effets.effet("api_act", {"op": "request", "method": "GET",
                                    "path": "/api/prix"})[0] == "lire"


# --- non-régression : un mode sans jar ne touche pas au coffre ------------------------

def test_le_mode_basic_ne_lit_ni_n_ecrit_de_jar(banc):
    banc.poser({"base_url": banc.base, "auth_mode": "basic", "username": "u",
                "password": "p"})
    out = banc.outil("api_get")(path="/api/basique")
    assert out["autorisation"] == "Basic " + base64.b64encode(b"u:p").decode()
    assert credentials_store.unseal_satellite(*LIGNE[:2], "api", COMPTE,
                                              "session_jar") is None
    assert banc.etat.visites == []
