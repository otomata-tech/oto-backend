"""Le journal d'ACCÈS d'uvicorn n'écrit jamais en clair un jeton porté par le chemin.

#558 a fermé la fuite dans `tool_calls` ; le journal d'accès d'uvicorn (journald,
200 Mo de rétention) recopiait pourtant toujours la cible brute de la requête —
`/api/receivers/apollo/phones/<jeton>`, `/api/upload/<jeton>`… Ces tests gardent la
même propriété sur ce canal : un segment lié à un paramètre de route secret devient
son masque, pour toute route présente ou future qui le déclare.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from oto_mcp import journal_secrets as js

JETON = "k3Yq9-J8vTzL0mW2xRpA7bC4dE6fG1hI5jK8lM0nO2p"   # 43 car., la forme émise

# La ligne telle qu'uvicorn la formate (`uvicorn.logging.AccessFormatter`).
FORMAT = '%s - "%s %s HTTP/%s" %d'


@pytest.fixture(autouse=True)
def _table_de_routes_reelle():
    """Déclare la VRAIE table de routes servie — pas une liste fabriquée ici."""
    from oto_mcp.api import routes as api_routes
    api_routes.make_routes(object(), mcp_instance=None)
    yield


@pytest.fixture
def acces(caplog):
    """Un logger monté comme `uvicorn.access` en prod : le filtre unique de `server.py`."""
    lg = logging.getLogger("test.acces.chemin")
    lg.filters.clear()
    lg.addFilter(js.MasqueCheminAcces())

    def ecrire(methode: str, cible: str, statut: int = 200) -> str:
        with caplog.at_level("INFO", logger=lg.name):
            caplog.clear()
            lg.info(FORMAT, "1.2.3.4:5", methode, cible, "1.1", statut)
        return caplog.records[-1].getMessage()
    return ecrire


@pytest.mark.parametrize("gabarit", [
    "/api/receivers/apollo/phones/{}",
    "/api/upload/{}",
    "/api/invitations/{}",
    "/api/public/docs/{}",
    "/p/d/{}",
    "/o/u/{}",
    "/o/d/{}",
])
def test_un_jeton_du_chemin_ne_part_pas_en_clair(acces, gabarit):
    ligne = acces("POST", gabarit.format(JETON))
    assert JETON not in ligne
    assert js.mask(JETON) in ligne, "le masque corrélable remplace le jeton"


ADRESSE_PRIVEE = "h_Zq3xV9mK2pL7wR4tY8uN1bC6"   # la forme `h_…` servie en `hook_url`


@pytest.mark.parametrize("adresse", [ADRESSE_PRIVEE, "4242"])
def test_l_adresse_d_un_webhook_d_agent_ne_part_pas_en_clair(acces, adresse):
    """`/api/hooks/{trigger_id}` : l'adresse privée est ce qui rend l'agent introuvable ;
    l'id numérique d'un agent sans adresse privée passe par le même segment, que la
    route ne distingue qu'en lisant la base — masqué pareil."""
    ligne = acces("POST", f"/api/hooks/{adresse}", 202)
    assert f"/api/hooks/{adresse}" not in ligne
    assert js.mask(adresse) in ligne


def test_l_adresse_d_un_webhook_ne_part_pas_en_clair_dans_tool_calls():
    """L'autre canal : la ligne `tool_calls` d'un POST de webhook (`route_and_secrets`,
    lu par le journal REST) ne porte l'adresse ni dans `tool` ni en clair dans `args`."""
    route, masques = js.route_and_secrets(f"/api/hooks/{ADRESSE_PRIVEE}")
    assert route == "/api/hooks/:trigger_id"
    assert masques == {"trigger_id": js.mask(ADRESSE_PRIVEE)}


def test_le_secret_d_une_route_ne_devient_pas_un_nom_secret_partout():
    """`trigger_id` est secret sur `/api/hooks/…` PAR DÉCLARATION DE LA ROUTE, pas par
    son nom : les capacités de flotte le portent en clair, et le chemin du webhook
    garde le nom que les contrats des fronts épinglent (`/api/hooks/{trigger_id}`)."""
    assert "trigger_id" not in js.SECRET_PARAM_NAMES
    assert "trigger_id" not in js.secret_arg_names("oto_trigger")


def test_la_requete_et_la_queue_restent_lisibles(acces):
    ligne = acces("GET", f"/api/upload/{JETON}/x?format=csv", 404)
    assert JETON not in ligne and "/x?format=csv" in ligne


def test_une_route_ordinaire_est_recopiee_telle_quelle(acces):
    assert "/api/datastores/204/rows?limit=5" in acces("GET", "/api/datastores/204/rows?limit=5")


def test_les_cles_secretes_de_la_query_sont_masquees(acces):
    ligne = acces("GET", "/api/x?code=abc&state=xyz&foo=1", 302)
    assert '"GET /api/x?code=***&state=***&foo=1 HTTP/1.1" 302' in ligne


@pytest.mark.parametrize("route", [
    "/oauth/callback",                       # le relais d'autorisation
    "/api/microsoft/oauth/callback",
    "/api/google/oauth/callback",
    "/api/salesforce/oauth/callback",
    "/api/zoho/oauth/callback",
    "/api/meta_ads/oauth/callback",
    "/api/instagram_meta/oauth/callback",
    "/api/mcp/auth_callback",                # atlassian, folk… (serveurs MCP distants)
    "/api/une_route_future/oauth/callback",  # aucune liste de routes
])
def test_le_code_d_un_retour_oauth_ne_part_pas_en_clair(acces, route):
    ligne = acces("GET", f"{route}?code=1.AR8ASECRET&state=SCEAU&session_state=SESS"
                         "&iss=https%3A%2F%2Fa", 302)
    for secret in ("1.AR8ASECRET", "SCEAU", "SESS"):
        assert secret not in ligne
    assert f"{route}?code=***&state=***&session_state=***&iss=https%3A%2F%2Fa" in ligne


@pytest.mark.parametrize("cle", [
    "id_token", "access_token", "refresh_token", "client_secret", "token", "api_token",
    "password", "app_secret", "X-Token", "client%5Fsecret", "Code", "key", "apikey",
    "api_key", "access_key",
])
def test_toute_cle_secrete_par_son_nom(cle):
    assert js.chemin_pour_journal_acces(f"/api/x?a=1&{cle}=V4L3UR&b=2") \
        == f"/api/x?a=1&{cle}=***&b=2"


@pytest.mark.parametrize("requete", ["limit=5&offset=10", "q=code", "codex=1", "flag",
                                     "state", ""])
def test_une_cle_ordinaire_reste_lisible(requete):
    assert js.chemin_pour_journal_acces(f"/api/x?{requete}") == f"/api/x?{requete}"


def test_une_ligne_hors_forme_passe_sans_lever(caplog):
    lg = logging.getLogger("test.acces.hors_forme")
    lg.addFilter(js.MasqueCheminAcces())
    with caplog.at_level("INFO", logger=lg.name):
        lg.info("sans arguments")
        lg.info("%s", 42)
    assert [r.getMessage() for r in caplog.records] == ["sans arguments", "42"]


def test_le_serveur_monte_le_filtre_sur_uvicorn_access():
    """Cliquet : sans ce branchement, tout ce qui précède est inerte EN SILENCE."""
    source = (Path(__file__).resolve().parent.parent / "oto_mcp" / "server.py").read_text()
    assert "journal_secrets.installer_masques_du_journal()" in source
    assert source.index("journal_secrets.installer_masques_du_journal()") \
        < source.index("uvicorn.run(")


@pytest.fixture
def journal_uvicorn():
    """Le VRAI logger `uvicorn.access`, rendu dans l'état où il était."""
    lg = logging.getLogger(js.JOURNAL_ACCES)
    avant = (list(lg.filters), list(lg.handlers), lg.level, lg.propagate)
    lg.filters.clear()
    yield lg
    lg.filters[:], lg.handlers[:] = avant[0], avant[1]
    lg.setLevel(avant[2])
    lg.propagate = avant[3]


def test_le_filtre_est_attache_au_logger_et_survit_au_demarrage_d_uvicorn(journal_uvicorn):
    """Garde : le filtre est sur le logger NOMMÉ `uvicorn.access`, une seule fois, et il y
    est ENCORE après la configuration de journal qu'`uvicorn.run` applique au démarrage
    (le chemin réel de prod : `oto-mcp` → `server.main` → installation → `uvicorn.run`)."""
    import uvicorn

    js.installer_masques_du_journal()
    js.installer_masques_du_journal()
    uvicorn.Config(app=lambda *a: None, log_level="info").configure_logging()

    masques = [f for f in journal_uvicorn.filters if isinstance(f, js.MasqueCheminAcces)]
    assert len(masques) == 1, "le filtre du journal d'accès n'est plus attaché à uvicorn.access"
    # La configuration d'uvicorn ferme les handlers existants (caplog compris) : on lit
    # la ligne sur un handler posé APRÈS elle, comme le sien.
    lignes: list[str] = []
    capteur = logging.Handler()
    capteur.emit = lambda r: lignes.append(r.getMessage())
    journal_uvicorn.addHandler(capteur)
    journal_uvicorn.info(FORMAT, "1.2.3.4:5", "GET", "/api/x?code=abc&state=xyz&foo=1",
                         "1.1", 302)
    assert lignes == ['1.2.3.4:5 - "GET /api/x?code=***&state=***&foo=1 HTTP/1.1" 302']



# ── Les requêtes SORTANTES (httpx, httpcore, urllib3) : la même règle ─────────────

@pytest.fixture
def journaux_sortants():
    """Les VRAIS loggers des clients HTTP, rendus dans l'état où ils étaient."""
    avant = {n: list(logging.getLogger(n).filters) for n in js.JOURNAUX_SORTANTS}
    for n in avant:
        logging.getLogger(n).filters.clear()
    yield
    for n, filtres in avant.items():
        logging.getLogger(n).filters[:] = filtres


def _ecrire_sur(nom: str, msg: str, *args) -> str:
    lignes: list[str] = []
    capteur = logging.Handler()
    capteur.emit = lambda r: lignes.append(r.getMessage())
    lg = logging.getLogger(nom)
    niveau = lg.level
    lg.addHandler(capteur)
    lg.setLevel(logging.DEBUG)
    try:
        lg.info(msg, *args)
    finally:
        lg.removeHandler(capteur)
        lg.setLevel(niveau)
    return lignes[0]


def test_la_ligne_httpx_masque_la_cle_en_query(journaux_sortants):
    import httpx

    js.installer_masques_du_journal()
    ligne = _ecrire_sur("httpx", 'HTTP Request: %s %s "%s %d %s"', "POST",
                        httpx.URL("https://x/y?key=abc&q=1"), "HTTP/1.1", 200, "OK")
    assert ligne == 'HTTP Request: POST https://x/y?key=***&q=1 "HTTP/1.1 200 OK"'


def test_urllib3_et_httpcore_masquent_pareil(journaux_sortants):
    js.installer_masques_du_journal()
    ligne = _ecrire_sur("urllib3.connectionpool", '%s://%s:%s "%s %s %s" %s %s', "https",
                        "x", 443, "GET", "/v1?api_key=abc&q=1", "HTTP/1.1", 200, 12)
    assert ligne == 'https://x:443 "GET /v1?api_key=***&q=1 HTTP/1.1" 200 12'
    ligne = _ecrire_sur("httpcore.http11", "send_request_headers.started "
                        "url=https://x/y?access_token=abc&q=1")
    assert "access_token=***&q=1" in ligne and "abc" not in ligne


def test_une_ligne_sortante_sans_secret_est_recopiee(journaux_sortants):
    js.installer_masques_du_journal()
    assert _ecrire_sur("httpx", "HTTP Request: %s %s %d", "GET", "https://x/y?q=1", 200) \
        == "HTTP Request: GET https://x/y?q=1 200"


def test_les_filtres_sortants_sont_attaches_et_survivent_au_demarrage(journaux_sortants):
    """Garde : chaque logger sortant porte le filtre, une seule fois, après la
    configuration de journal d'`uvicorn.run`."""
    import uvicorn

    js.installer_masques_du_journal()
    js.installer_masques_du_journal()
    uvicorn.Config(app=lambda *a: None, log_level="info").configure_logging()
    for nom in ("httpx", "httpcore", "httpcore.http11", "urllib3.connectionpool"):
        poses = [f for f in logging.getLogger(nom).filters
                 if isinstance(f, js.MasqueRequeteSortante)]
        assert len(poses) == 1, f"le filtre n'est plus attaché à {nom}"
