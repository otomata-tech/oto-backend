"""Le mode `session` du connecteur `api`, de bout en bout : les VRAIS outils, le VRAI
client d'oto-core, le VRAI coffre (base jetable), contre deux faux serveurs locaux.

Le service fictif : son API (`api.service.test`) prend le login en JSON et rend l'URL
d'un serveur de connexion (`connexion.service.test`, un autre hôte du même domaine),
qui redirige vers un code ; ce code pose le cookie de session, avec lequel l'API se
lit. Les noms d'hôte sont résolus vers la boucle locale pour la connexion, et vers une
adresse publique pour la garde de sortie, qui voit chaque saut. Aucun service réel.
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
from oto_mcp.tools import api_calls

MOT_DE_PASSE = "mot-de-passe-de-test"
COMPTE = "espace"
LIGNE = ("org", "1", COMPTE)
API, CONNEXION, AILLEURS = "api.service.test", "connexion.service.test", "ailleurs.test"
_KEY = base64.b64encode(b"\x5a" * 32).decode()


class Etat:
    def __init__(self):
        self.logins: list[dict] = []
        self.connexions: list[str] = []
        self.lectures = 0
        self.jetons: set[str] = set()
        self.emis = 0
        self.refuse_toujours = False
        self.auth_url = ""
        self.cible = ""


def _api(etat: Etat):
    class Gestion(http.server.BaseHTTPRequestHandler):
        def _json(self, code, corps, entetes=()):
            data = json.dumps(corps).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in entetes:
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_POST(self):
            corps = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path != "/api/login":
                return self._json(405, {"error": "post refused"})
            etat.logins.append(corps)
            if corps.get("password") != MOT_DE_PASSE:
                return self._json(401, {"message": "Invalid credentials."})
            self._json(200, {"auth_url": etat.auth_url, "refresh_token": "r"})

        def do_GET(self):
            p = urlsplit(self.path)
            if p.path == "/code":
                if parse_qs(p.query).get("code") != ["c0de"]:
                    return self._json(403, {"error": "bad code"})
                etat.emis += 1
                jeton = f"jwt-{etat.emis}"
                etat.jetons.add(jeton)
                self.send_response(302)
                self.send_header("Location", "/app/")
                self.send_header("Content-Length", "0")
                self.send_header("Set-Cookie", f"SESSION={jeton}; Path=/; HttpOnly")
                return self.end_headers()
            if p.path == "/app/":
                return self._json(200, {"page": "app"})
            if p.path == "/api/items":
                etat.lectures += 1
                cookie = self.headers.get("Cookie") or ""
                jeton = cookie.split("SESSION=", 1)[1].split(";")[0] if "SESSION=" in cookie else ""
                if jeton in etat.jetons and not etat.refuse_toujours:
                    return self._json(200, {"items": [1, 2]})
                return self._json(401, {"message": "Expired token"})
            self._json(404, {})

        do_HEAD = do_GET

        def log_message(self, *a):
            pass

    return Gestion


def _connexion(etat: Etat):
    class Gestion(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            etat.connexions.append(self.path)
            p = urlsplit(self.path)
            ok = p.path == "/connect" and parse_qs(p.query).get("step") == ["1"]
            self.send_response(302 if ok else 400)
            if ok:
                self.send_header("Location", etat.cible)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    return Gestion


def _serveur(gestion):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), gestion)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


ETAPES = {"login": {"path": "/api/login", "constants": {"remember_me": True}},
          "follow": {"field": "auth_url", "query": {"step": "1"}},
          "domain": "service.test", "login_pages": ["/login"]}


@pytest.fixture
def banc(live, monkeypatch):
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", _KEY)
    monkeypatch.delenv(egress.ALLOW_VAR, raising=False)
    # Les noms du service fictif mènent à la boucle locale ; la garde de sortie les
    # voit publics (une adresse interne, elle, est refusée — cf. le test du saut).
    internes: set[str] = set()
    monkeypatch.setattr(egress, "resolved_addresses",
                        lambda hote, port: {"10.0.0.7"} if hote in internes
                        else {"93.184.216.34"})
    vrai = socket.getaddrinfo

    def resoudre(hote, *a, **k):
        if isinstance(hote, str) and hote in (API, CONNEXION, AILLEURS):
            hote = "127.0.0.1"
        return vrai(hote, *a, **k)
    monkeypatch.setattr(socket, "getaddrinfo", resoudre)

    etat = Etat()
    api, port_api = _serveur(_api(etat))
    cx, port_cx = _serveur(_connexion(etat))
    base = f"http://{API}:{port_api}"
    etat.auth_url = f"http://{CONNEXION}:{port_cx}/connect?ticket=t1"
    etat.cible = f"{base}/code?code=c0de"

    def poser(mot_de_passe=MOT_DE_PASSE):
        champs = {"base_url": base, "auth_mode": "session", "username": "compte@example.org",
                  "password": mot_de_passe, "session": json.dumps(ETAPES)}
        credentials_store.set_credential(*LIGNE[:2], "api",
                                         credentials_store.pack_secret("api", champs),
                                         account=COMPTE)
        return champs

    carte = {"champs": poser()}

    def rc():
        return SimpleNamespace(fields=dict(carte["champs"]), entity_type=LIGNE[0],
                               entity_id=LIGNE[1], account=COMPTE, provider="api")

    monkeypatch.setattr(H, "current_user_sub_from_token", lambda: "u-1")
    monkeypatch.setattr(H, "_resolve", rc)
    monkeypatch.setattr(access, "resolve_credential", lambda *a, **k: rc())
    m = FastMCP("t")
    H.register(m)
    outil = lambda nom: asyncio.run(m.get_tool(nom)).fn  # noqa: E731

    def reposer(mot_de_passe):
        carte["champs"] = poser(mot_de_passe)

    yield SimpleNamespace(etat=etat, outil=outil, port_cx=port_cx, internes=internes,
                          reposer=reposer, carte=carte)
    api.shutdown()
    cx.shutdown()
    credentials_store.clear_credential(*LIGNE[:2], "api", account=COMPTE)


def _meta() -> dict:
    from oto_mcp.db import _connect
    with _connect() as c:
        return c.execute(
            "SELECT meta FROM connector_credentials WHERE entity_type=%s AND entity_id=%s "
            "AND connector='api' AND account=%s", LIGNE).fetchone()["meta"]


def test_login_suivi_vers_un_autre_hote_du_domaine_puis_lecture_au_cookie(banc):
    out = banc.outil("api_get")(path="/api/items")

    assert out == {"items": [1, 2]}
    assert banc.etat.logins == [{"username": "compte@example.org",
                                 "password": MOT_DE_PASSE, "remember_me": True}]
    assert banc.etat.connexions == ["/connect?ticket=t1&step=1"]
    # Le jar ne revient jamais à l'agent.
    assert "jwt-" not in json.dumps(out) and "SESSION" not in json.dumps(out)


def test_le_jar_est_range_chiffre_puis_relu_sans_nouveau_login(banc):
    banc.outil("api_get")(path="/api/items")
    meta = _meta()

    assert set(meta) >= {"session_jar_enc"}
    brut = json.dumps(meta)
    assert "jwt-1" not in brut and "SESSION" not in brut and MOT_DE_PASSE not in brut
    # Retiré des surfaces de statut, comme un bearer.
    assert "session_jar_enc" not in credentials_store.public_meta(meta)
    clair = credentials_store.unseal_satellite(*LIGNE[:2], "api", COMPTE, "session_jar")
    assert "jwt-1" in json.loads(clair)["jar"]

    # Un deuxième appel : un client neuf, le jar relu au coffre, aucun login.
    assert banc.outil("api_get")(path="/api/items") == {"items": [1, 2]}
    assert len(banc.etat.logins) == 1


def test_un_401_relance_un_login_et_un_seul_rejeu(banc):
    banc.outil("api_get")(path="/api/items")
    banc.etat.jetons.clear()                         # la session a expiré

    assert banc.outil("api_get")(path="/api/items") == {"items": [1, 2]}
    assert len(banc.etat.logins) == 2
    clair = credentials_store.unseal_satellite(*LIGNE[:2], "api", COMPTE, "session_jar")
    assert "jwt-2" in json.loads(clair)["jar"]


def test_refuse_encore_apres_le_rejeu_marque_la_cle_refusee(banc):
    banc.outil("api_get")(path="/api/items")
    banc.etat.refuse_toujours = True

    with pytest.raises(McpError) as pris:
        banc.outil("api_get")(path="/api/items")
    assert error_taxonomy.credential_rejected_in_chain(pris.value)
    assert pris.value.error.data["code"] == "credential_rejected"
    assert len(banc.etat.logins) == 2


def test_un_mot_de_passe_refuse_marque_la_cle_et_ne_fuit_pas(banc):
    banc.reposer("faux-mot-de-passe")
    with pytest.raises(McpError) as pris:
        banc.outil("api_get")(path="/api/items")
    assert error_taxonomy.credential_rejected_in_chain(pris.value)
    assert "faux-mot-de-passe" not in str(pris.value)
    assert "compte@example.org" not in str(pris.value)
    assert banc.etat.connexions == []


def test_un_jar_scelle_pour_d_autres_champs_n_est_pas_servi(banc):
    """Le jar d'un appel en cours quand la carte change n'est pas resservi à la
    nouvelle : la carte vue par l'appel (ici, un mot de passe changé sans repose) ne
    reprend pas le cookie scellé pour l'ancienne — elle se reconnecte, et c'est le
    service qui juge."""
    banc.outil("api_get")(path="/api/items")
    banc.carte["champs"] = {**banc.carte["champs"], "password": "autre"}
    with pytest.raises(McpError):
        banc.outil("api_get")(path="/api/items")
    assert len(banc.etat.logins) == 2


def test_reposer_la_carte_efface_le_jar(banc):
    banc.outil("api_get")(path="/api/items")
    banc.reposer(MOT_DE_PASSE)
    assert "session_jar_enc" not in _meta()
    assert banc.outil("api_get")(path="/api/items") == {"items": [1, 2]}
    assert len(banc.etat.logins) == 2


def test_un_saut_vers_un_hote_etranger_est_refuse_et_nomme(banc):
    banc.etat.auth_url = f"http://{AILLEURS}:{banc.port_cx}/connect?ticket=t1"
    with pytest.raises(McpError) as pris:
        banc.outil("api_get")(path="/api/items")
    assert "api_session_hop_refused" in str(pris.value) and AILLEURS in str(pris.value)
    assert pris.value.error.data["code"] == "api_session_hop_refused"
    assert banc.etat.connexions == []
    assert not error_taxonomy.credential_rejected_in_chain(pris.value)


def test_la_garde_de_sortie_voit_chaque_saut(banc):
    """Un hôte du domaine déclaré qui résout vers une adresse interne : refusé avant
    l'envoi, alors que le login, lui, est parti."""
    banc.internes.add(CONNEXION)
    with pytest.raises(McpError) as pris:
        banc.outil("api_get")(path="/api/items")
    assert "session" in str(pris.value)
    assert len(banc.etat.logins) == 1 and banc.etat.connexions == []


def test_lecture_seule_api_post_refuse_avant_tout_envoi(banc):
    with pytest.raises(McpError) as pris:
        banc.outil("api_post")(path="/api/items", body={"x": 1})
    assert pris.value.error.data["code"] == "api_session_read_only"
    assert banc.etat.logins == [] and banc.etat.lectures == 0


@pytest.mark.parametrize("methode", ["POST", "PUT", "PATCH", "DELETE"])
def test_lecture_seule_api_act_request_refuse_meme_a_blanc(banc, methode):
    with pytest.raises(McpError) as pris:
        api_calls._request(methode, "/api/items", None, {"x": 1}, dry_run=True)
    assert pris.value.error.data["code"] == "api_session_read_only"
    assert banc.etat.logins == []
