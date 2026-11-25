"""web_read (#348) — les transitions d'escalade se PROUVENT, et la garde SSRF mord.

Ce qui se teste ici : chaque cran tenté est TRACÉ (`tentatives`), le chemin
gagnant est DIT (`chemin`), un cran indisponible est sauté-et-dit, le ③ exige
l'opt-in, le cap mord PENDANT la lecture — et le fetch ne peut JAMAIS
atteindre le réseau privé (IP directe, domaine qui résout privé, redirection
publique→privée : les trois voies du SSRF, chacune son test).
"""
from __future__ import annotations

import asyncio

import pytest
from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import web as W


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


def _web_read(monkeypatch, *, fetch=None, serper="absent", browserbase_page=None,
              repli_www_reel=False):
    """Le tool monté avec les crans scriptés. `serper` : 'absent' (pas de clé),
    dict (réponse), Exception (panne).

    Le repli `www.` (oto#262) résout le DNS avant le cran ① : neutralisé par défaut,
    sinon chaque banc de ce fichier dépendrait du résolveur de la machine qui le
    joue. Un banc qui l'exerce passe `repli_www_reel=True` et simule le DNS."""
    reg = _Reg()
    if not repli_www_reel:
        monkeypatch.setattr(W, "_cible_avec_repli_www", lambda url: (url, False))
    if fetch is not None:
        monkeypatch.setattr(W, "_fetch_http", lambda url: fetch)
    if serper == "absent":
        monkeypatch.setattr(W.access, "resolve_api_key",
                            lambda p: (_ for _ in ()).throw(RuntimeError("no key")))
    else:
        monkeypatch.setattr(W.access, "resolve_api_key", lambda p: ("k", False))

        class _FauxSerper:
            def __init__(self, api_key):
                pass

            def scrape_page(self, url, include_markdown=True, timeout_s=None):
                if isinstance(serper, Exception):
                    raise serper
                return serper
        import oto.tools.serper as _s
        monkeypatch.setattr(_s, "SerperClient", _FauxSerper)
    if browserbase_page is not None:
        async def _faux(url, as_html=False):
            return browserbase_page
        monkeypatch.setattr(W.browserbase, "fetch_page_ephemeral", _faux)
        monkeypatch.setattr(W.browserbase, "is_configured", lambda: True)
    W.register(reg)
    fn = reg.tools["web_read"]
    return lambda **kw: asyncio.run(fn(**kw))


_HTML_OK = ("<html><head><title>ACME</title><style>x{}</style></head><body>"
            + "<p>Contact : contact@acme.fr — 01 02 03 04 05.</p>" * 20
            + "<script>alert(1)</script></body></html>")


# ── ① suffit : pas d'escalade ────────────────────────────────────────────────

def test_le_fetch_qui_lit_ne_paie_rien(monkeypatch):
    lire = _web_read(monkeypatch,
                     fetch={"ok": True, "status": 200, "html": _HTML_OK,
                            "final_url": "https://acme.fr", "verdict": "lu"})
    out = lire(url="https://acme.fr")
    assert out["chemin"] == "http"
    assert "contact@acme.fr" in out["content"]
    assert "alert(1)" not in out["content"], "script/style ne sont pas du texte"
    assert out["title"] == "ACME"
    assert out["cout"] == {"serper_credits": 0, "browser_session": False}
    assert out["tentatives"] == [{"cran": "http", "verdict": "read"}]


# ── ① échoue → ② ────────────────────────────────────────────────────────────

def test_un_403_escalade_vers_serper(monkeypatch):
    lire = _web_read(monkeypatch,
                     fetch={"ok": False, "verdict": "HTTP 403", "status": 403},
                     serper={"markdown": "# ACME\n\nContact : x@acme.fr\n" * 20})
    out = lire(url="https://acme.fr")
    assert out["chemin"] == "serper"
    assert out["cout"]["serper_credits"] == 1
    assert out["tentatives"][0] == {"cran": "http", "verdict": "HTTP 403"}
    assert out["tentatives"][1]["cran"] == "serper"


def _quantite_tracee(lire, **kw):
    """La `quantity` que le relevé d'appel porte après une lecture — l'axe que
    le facturier de l'instance lit (absente = 1, d'où le zéro TRACÉ)."""
    from oto_mcp import session_org
    releve: dict = {}
    jeton = session_org.set_call_trace(releve)
    try:
        lire(**kw)
    finally:
        session_org.reset_call_trace(jeton)
    return releve.get("quantity")


def test_la_quantite_facturee_est_celle_des_credits_serper(monkeypatch):
    lire = _web_read(monkeypatch,
                     fetch={"ok": False, "verdict": "HTTP 403", "status": 403},
                     serper={"markdown": "# ACME\n\nContact : x@acme.fr\n" * 20,
                             "credits": 2})
    assert _quantite_tracee(lire, url="https://acme.fr") == 2


def test_le_fetch_gratuit_trace_un_zero(monkeypatch):
    lire = _web_read(monkeypatch,
                     fetch={"ok": True, "status": 200, "html": _HTML_OK,
                            "final_url": "https://acme.fr", "verdict": "lu"})
    assert _quantite_tracee(lire, url="https://acme.fr") == 0


def test_une_coquille_vide_escalade_aussi(monkeypatch):
    lire = _web_read(monkeypatch,
                     fetch={"ok": True, "status": 200, "final_url": "https://x.fr",
                            "html": "<html><body><div id=root></div></body></html>",
                            "verdict": "lu"},
                     serper={"markdown": "du vrai contenu rendu par le JS " * 20})
    out = lire(url="https://x.fr")
    assert out["chemin"] == "serper"
    assert "empty shell" in out["tentatives"][0]["verdict"]


def test_sans_cle_serper_le_cran_est_saute_et_dit(monkeypatch):
    lire = _web_read(monkeypatch,
                     fetch={"ok": False, "verdict": "timeout"}, serper="absent")
    with pytest.raises(McpError) as e:
        lire(url="https://lent.fr")
    assert "browser=true" in str(e.value), "le refus dit le geste suivant"


# ── ② échoue → ③ opt-in STRICT ──────────────────────────────────────────────

def test_sans_opt_in_jamais_de_navigateur(monkeypatch):
    lire = _web_read(monkeypatch,
                     fetch={"ok": False, "verdict": "HTTP 403", "status": 403},
                     serper={"markdown": ""},
                     browserbase_page={"content": "JAMAIS SERVI"})
    with pytest.raises(McpError) as e:
        lire(url="https://acme.fr")
    assert "browser=true" in str(e.value)
    assert "JAMAIS SERVI" not in str(e.value)


def test_avec_opt_in_le_navigateur_lit_et_le_cout_est_dit(monkeypatch):
    lire = _web_read(monkeypatch,
                     fetch={"ok": False, "verdict": "HTTP 403", "status": 403},
                     serper={"markdown": ""},
                     browserbase_page={"content": "Contact : x@acme.fr",
                                       "title": "ACME", "final_url": "https://acme.fr/c",
                                       "status": 200})
    out = lire(url="https://acme.fr", browser=True)
    assert out["chemin"] == "browser"
    assert out["cout"]["browser_session"] is True
    assert [t["cran"] for t in out["tentatives"]] == ["http", "serper", "browser"]


# ── la garde SSRF : les trois voies, chacune son test ────────────────────────

# Depuis le 12/09/2026 (oto#180) la garde de `web_read` EST celle d'egress
# (`oto_mcp/egress.py`) : la résolution DNS se remplace là, et le refus nomme la
# plage (« réseau privé », « boucle locale »…), plus seulement « non publique ».
def _resolution(**table):
    def faux(hote, port):  # noqa: ARG001
        return set(table[hote])
    return faux


def test_une_ip_privee_directe_est_refusee():
    with pytest.raises(McpError) as e:
        W.check_url_public("http://172.16.16.4:8000/api/sirene/info")
    assert "adresse interne" in str(e.value) and "réseau privé" in str(e.value)


def test_un_domaine_qui_resout_prive_est_refuse(monkeypatch):
    from oto_mcp import egress
    monkeypatch.setattr(egress, "resolved_addresses",
                        _resolution(**{"piege.example.com": ["10.0.0.7"]}))
    with pytest.raises(McpError) as e:
        W.check_url_public("https://piege.example.com/")
    assert "adresse interne" in str(e.value)
    assert "piege.example.com" in str(e.value) and "10.0.0.7" in str(e.value)


def test_une_redirection_vers_le_prive_est_refusee(monkeypatch):
    """Le 302 public→privé : la garde repasse à CHAQUE saut."""
    sauts = {"n": 0}

    class _Resp:
        def __init__(self, redirect):
            self.is_redirect = redirect
            self.is_permanent_redirect = False
            self.status_code = 302 if redirect else 200
            self.headers = {"Location": "http://169.254.169.254/latest/meta-data/"}

        def close(self):
            pass

    def _faux_get(url, **kw):
        sauts["n"] += 1
        return _Resp(redirect=True)

    from oto_mcp import egress
    monkeypatch.setattr(egress, "resolved_addresses",
                        lambda h, p: {"93.184.216.34"} if "example.com" in h
                        else {"169.254.169.254"})
    monkeypatch.setattr(W.requests, "get", _faux_get)
    with pytest.raises(McpError) as e:
        W._fetch_http("https://example.com/page")
    assert "adresse interne" in str(e.value) and "lien-local" in str(e.value)
    assert sauts["n"] == 1, "le saut privé est refusé AVANT toute connexion"


def test_un_schema_non_http_est_refuse():
    with pytest.raises(McpError):
        W.check_url_public("file:///etc/passwd")


def test_un_host_mixte_public_prive_est_refuse(monkeypatch):
    """Fail-closed sur l'ENSEMBLE : public + privé dans la même résolution =
    le montage type du contournement."""
    from oto_mcp import egress
    monkeypatch.setattr(egress, "resolved_addresses",
                        _resolution(**{"mixte.example.com": ["93.184.216.34",
                                                             "192.168.1.10"]}))
    with pytest.raises(McpError):
        W.check_url_public("https://mixte.example.com/")


# ── le cap mord PENDANT la lecture ───────────────────────────────────────────

def test_le_cap_arrete_la_lecture_en_cours(monkeypatch):
    servis = {"n": 0}

    class _Resp:
        is_redirect = False
        is_permanent_redirect = False
        status_code = 200
        encoding = "utf-8"
        headers: dict = {}

        def iter_content(self, chunk_size, decode_unicode=False):
            while True:
                servis["n"] += 1
                yield b"x" * chunk_size

        def close(self):
            pass

    monkeypatch.setattr(W, "check_url_public", lambda u: None)
    monkeypatch.setattr(W.requests, "get", lambda *a, **k: _Resp())
    out = W._fetch_http("https://enorme.fr/")
    assert out["ok"] and len(out["html"]) <= W._MAX_FETCH_BYTES + 65536
    assert servis["n"] <= (W._MAX_FETCH_BYTES // 65536) + 1, \
        "la lecture s'arrête PENDANT — jamais accumuler-puis-tronquer"


# ── #491 : ce que la réponse DIT de l'hôte réellement servi ──────────────────
#
# Signal #491 (17/08, `wrong_result`) : « web_read rend par intermittence le
# contenu d'un AUTRE domaine que celui demandé (cross-contamination between
# concurrent calls) ». L'enquête du 28/08 a réfuté la contamination et daté ce
# qui reste :
#
#   1. le journal de prod du 17/08 porte 371 `web_read`, **tous du MÊME `sub`**
#      (aucune minute avec deux appelants distincts) — il n'y a jamais eu deux
#      utilisateurs à contaminer l'un par l'autre ;
#   2. aucun état n'est partagé entre deux appels : `requests.get` ouvre sa
#      propre connexion, `SerperClient` est instancié à chaque appel, une
#      session Browserbase est ouverte puis relâchée par appel ; ⚠️ vrai au
#      28/08 — depuis oto#115, le client Serper est UNE instance par clé
#      (`serper.client_for`), pour que son limiteur de débit compte : elle ne
#      porte que ce limiteur et la session HTTP, aucune donnée de réponse ;
#   3. le cas cité (un site de marque → la boutique d'un autre domaine) est une
#      **redirection 301 légitime**, revérifiée en direct le 28/08 — elle n'est
#      d'ailleurs visible qu'avec notre User-Agent, curl nu reçoit un 403 ;
#   4. le second cas cité (un domaine associatif → un autre) n'existe
#      pas au journal : le seul appel (`#357616`) a ÉCHOUÉ sur
#      `Serper scrape 500`, et aucun appel de la plateforme n'a jamais porté
#      ce second domaine dans ses arguments.
#
# Reste un vrai défaut, et c'est celui qui a rendu l'accusation crédible : le
# tool AFFIRMAIT une `final_url` qu'il ne connaissait pas. Sur le cran ②, serper
# suit les redirections en silence et ne rend AUCUNE URL finale — `_sortie`
# recopiait alors l'URL DEMANDÉE. La parade du rapporteur (« comparer
# `final_url` à l'hôte demandé ») était donc structurellement aveugle sur ce
# cran, et l'écart d'hôte du cran ① restait à sa charge.
#
# D'où la règle que ces tests figent : **`final_url` est OBSERVÉE ou elle est
# `None`**, et un écart d'hôte est ANNONCÉ par le tool.

def test_le_cran_serper_n_invente_pas_l_url_finale(monkeypatch):
    """Serper ne dit pas où il a atterri ⟹ on ne le sait pas, et on le DIT."""
    lire = _web_read(monkeypatch,
                     fetch={"ok": False, "verdict": "HTTP 403", "status": 403},
                     serper={"markdown": "du contenu venu d'on ne sait où. " * 20})
    out = lire(url="https://acme.fr")
    assert out["chemin"] == "serper"
    assert out["final_url"] is None, \
        "recopier l'URL demandée serait AFFIRMER une chose inconnue (#491)"
    assert out["hote"] == {"demande": "acme.fr", "servi": None, "conforme": None}
    assert "avertissement" in out


def test_une_redirection_hors_domaine_est_annoncee(monkeypatch):
    """Le cas `ancien-site.example` → `boutique.nouvelle-marque.example` : redirection légitime,
    mais l'appelant doit l'apprendre DU TOOL, pas d'une comparaison qu'il pense
    à faire lui-même (#491)."""
    lire = _web_read(monkeypatch,
                     fetch={"ok": True, "status": 200, "html": _HTML_OK,
                            "final_url": "https://boutique.nouvelle-marque.example/fr/",
                            "verdict": "lu"})
    out = lire(url="https://www.ancien-site.example/")
    assert out["final_url"] == "https://boutique.nouvelle-marque.example/fr/"
    assert out["hote"] == {"demande": "www.ancien-site.example",
                           "servi": "boutique.nouvelle-marque.example",
                           "conforme": False}
    assert "boutique.nouvelle-marque.example" in out["avertissement"]
    assert "ancien-site.example" in out["avertissement"]


def test_un_saut_vers_www_ou_un_sous_domaine_n_est_pas_un_ecart(monkeypatch):
    """`acme.fr` → `www.acme.fr` (ou `shop.acme.fr`) est le MÊME site : crier au
    loup à chaque redirection canonique rendrait l'avertissement inaudible."""
    for servie in ("https://www.acme.fr/", "https://shop.acme.fr/x"):
        lire = _web_read(monkeypatch,
                         fetch={"ok": True, "status": 200, "html": _HTML_OK,
                                "final_url": servie, "verdict": "lu"})
        out = lire(url="https://acme.fr/")
        assert out["hote"]["conforme"] is True, servie
        assert "avertissement" not in out, servie


def test_deux_lectures_successives_ne_se_contaminent_pas(monkeypatch):
    """L'accusation de #491 prise au mot : deux lectures d'affilée rendent
    CHACUNE son domaine. Aucun état partagé entre appels — ce test le fige pour
    que l'absence de cache reste un choix, pas un hasard."""
    pages = {
        "https://un.fr": "<html><head><title>UN</title></head><body>"
                         + "<p>le contenu de UN.</p>" * 30 + "</body></html>",
        "https://deux.fr": "<html><head><title>DEUX</title></head><body>"
                           + "<p>le contenu de DEUX.</p>" * 30 + "</body></html>",
    }
    monkeypatch.setattr(W, "_fetch_http",
                        lambda url: {"ok": True, "status": 200, "html": pages[url],
                                     "final_url": url, "verdict": "lu"})
    lire = _web_read(monkeypatch, serper="absent")
    a = lire(url="https://un.fr")
    b = lire(url="https://deux.fr")
    c = lire(url="https://un.fr")
    assert (a["title"], b["title"], c["title"]) == ("UN", "DEUX", "UN")
    assert "DEUX" not in a["content"] and "UN." not in b["content"]
    assert [o["final_url"] for o in (a, b, c)] == \
        ["https://un.fr", "https://deux.fr", "https://un.fr"]


# ── #491 (2ᵉ défaut) : un appel qui pend gèle TOUT le monde ──────────────────
#
# Le même journal du 17/08 donne 11 lectures **au-delà de 30 s**, une à **57,5 s**.
# `web_read` est `async def` (il `await` le cran ③), donc FastMCP l'exécute DANS
# la boucle — mais ses crans ① et ② sont du I/O SYNCHRONE (`requests`,
# `SerperClient`). 57 s de boucle tenue, pour tous les utilisateurs à la fois :
# c'est le mode de gel n°1 de `docs/event-loop-perf.md`.
#
# Le garde-fou AST `test_no_blocking_async_handlers` ne peut PAS le voir : son
# critère est « ce handler `await`-t-il quelque chose dans son propre scope ? »,
# et celui-ci `await` bien — au cran ③, tout en bas. D'où un test qui n'analyse
# pas le source mais OBSERVE le thread, comme le garde des middlewares.

def test_les_crans_bloquants_ne_tournent_pas_dans_la_boucle(monkeypatch):
    """Le fetch et le scraper sont synchrones : ils doivent sortir de la boucle."""
    import threading

    vus: list = []

    def _faux_fetch(url):
        vus.append(threading.current_thread())
        return {"ok": True, "status": 200, "html": _HTML_OK,
                "final_url": url, "verdict": "lu"}

    monkeypatch.setattr(W, "_fetch_http", _faux_fetch)
    monkeypatch.setattr(W.access, "resolve_api_key",
                        lambda p: (_ for _ in ()).throw(RuntimeError("no key")))
    reg = _Reg()
    W.register(reg)
    fn = reg.tools["web_read"]

    boucle: list = []

    async def _scenario():
        boucle.append(threading.current_thread())
        return await fn(url="https://acme.fr")

    asyncio.run(_scenario())
    assert vus and boucle, "la garde serait inerte si le fetch n'était pas atteint"
    assert vus[0] is not boucle[0], \
        "le fetch synchrone doit sortir de la boucle (asyncio.to_thread)"


def test_la_lecture_a_un_budget_et_dit_ce_qu_elle_a_tente(monkeypatch):
    """`_TIMEOUT` borne CHAQUE socket, jamais la lecture entière : six sauts de
    redirection valent six fois le budget, et le streaming n'est borné par rien.
    Il faut un délai GLOBAL — et son verdict doit dire ce qu'il a tenté (#491)."""
    horloge = {"t": 0.0}
    monkeypatch.setattr(W, "_now", lambda: horloge["t"])

    class _FauxRedirect:
        is_redirect = True
        is_permanent_redirect = False
        status_code = 302
        headers = {"Location": "https://acme.fr/encore"}

        def close(self):
            pass

    def _get(url, **kw):
        horloge["t"] += 20.0        # chaque saut brûle 20 s
        return _FauxRedirect()

    monkeypatch.setattr(W.requests, "get", _get)
    monkeypatch.setattr(W, "check_url_public", lambda u: None)

    res = W._fetch_http("https://acme.fr/")
    assert res["ok"] is False
    assert "timeout" in res["verdict"], f"verdict opaque : {res['verdict']!r}"
    assert "redirect" in res["verdict"], \
        "le verdict doit dire ce qu'il a tenté, pas seulement qu'il a renoncé"
    assert horloge["t"] <= W._DEADLINE_S + 20.0, \
        "le budget doit couper AVANT d'épuiser les 5 redirections"


# ── ② : ce qu'on annonce et ce qu'on débite, même lecture ────────────────────

def _cran_deux(monkeypatch, reponse, *, platform=True):
    """Le cran ② atteint, avec la clé PLATEFORME et un espion sur le débit de quota."""
    debits = []
    lire = _web_read(monkeypatch,
                     fetch={"ok": False, "verdict": "HTTP 403", "status": 403},
                     serper=reponse)
    monkeypatch.setattr(W.access, "resolve_api_key", lambda p: ("k", platform))
    from _releve import capter_units
    capter_units(monkeypatch, "serper", lambda provider, calls: debits.append((provider, calls)))
    return lire, debits


def test_le_cran_serper_debite_ce_qu_il_a_reellement_coute(monkeypatch):
    """`web_read` est la SECONDE bouche serper du backend, et elle débitait UNE unité de
    quota là où un scrape en coûte de deux à dix — la description du tool annonçait
    « ~2 credits », et `cout.serper_credits` les rendait déjà à l'appelant. La moitié de
    la dépense de ce chemin n'était comptée nulle part.

    Ce que le tool ANNONCE et ce qu'il DÉBITE sont désormais la même lecture de la même
    réponse (`serper.credits_consumed`), pas deux règles qui dérivent."""
    lire, debits = _cran_deux(monkeypatch,
                              {"markdown": "# ACME\ndu contenu\n" * 40, "credits": 2})
    out = lire(url="https://acme.fr")

    assert out["chemin"] == "serper"
    assert debits == [("serper", 2)], "le quota interne sous-comptait le cran ② de moitié"
    assert out["cout"]["serper_credits"] == 2


def test_un_scrape_difficile_coute_ce_que_serper_dit(monkeypatch):
    """La contre-épreuve du 2 en dur : le montant vient de la RÉPONSE. Une page difficile
    est facturée davantage, et le quota doit le voir."""
    lire, debits = _cran_deux(monkeypatch,
                              {"markdown": "# ACME\ndu contenu\n" * 40, "credits": 10})
    out = lire(url="https://acme.fr")

    assert debits == [("serper", 10)]
    assert out["cout"]["serper_credits"] == 10


def test_un_amont_muet_debite_au_moins_un(monkeypatch):
    """Repli honnête : une réponse réussie coûte au moins un crédit, jamais zéro."""
    lire, debits = _cran_deux(monkeypatch, {"markdown": "# ACME\ndu contenu\n" * 40})
    out = lire(url="https://acme.fr")

    assert debits == [("serper", 1)]
    assert out["cout"]["serper_credits"] == 1


def test_sur_une_cle_du_client_le_quota_de_la_plateforme_ne_bouge_pas(monkeypatch):
    """Le QUOTA interne d'oto ne compte que NOTRE clé — inchangé par ce lot."""
    lire, debits = _cran_deux(monkeypatch,
                              {"markdown": "# ACME\ndu contenu\n" * 40, "credits": 2},
                              platform=False)
    out = lire(url="https://acme.fr")

    assert debits == []
    assert out["cout"]["serper_credits"] == 2, "le coût reste ANNONCÉ, clé du client ou pas"


# ── #1336 : un échec du cran ② s'écrit dans `tentatives`, la lecture continue ──

def _erreur_serper(statut):
    from oto.tools.common.errors import UpstreamHTTPError
    return UpstreamHTTPError(statut, "boom", service="serper")


@pytest.mark.parametrize("panne, verdict", [
    (_erreur_serper(502), "error HTTP 502"),
    (_erreur_serper(404), "error HTTP 404"),
    ("timeout", "timeout after"),
    ("reseau", "network: ConnectionError"),
])
def test_un_echec_du_cran_serper_est_trace_et_la_lecture_continue(monkeypatch, panne,
                                                                    verdict):
    import requests
    if panne == "timeout":
        panne = requests.Timeout("lent")
    elif panne == "reseau":
        panne = requests.ConnectionError("coupé")
    lire = _web_read(monkeypatch,
                     fetch={"ok": False, "verdict": "network: SSLError"}, serper=panne)
    with pytest.raises(McpError) as e:
        lire(url="https://acme.fr")
    msg = str(e.value)
    assert "Page unreadable by fetch and scraper" in msg
    assert "http: network: SSLError" in msg and "serper: " + verdict in msg


def test_un_echec_du_cran_serper_laisse_le_navigateur_lire(monkeypatch):
    lire = _web_read(monkeypatch,
                     fetch={"ok": False, "verdict": "HTTP 403", "status": 403},
                     serper=_erreur_serper(503),
                     browserbase_page={"content": "Contact : x@acme.fr", "title": "ACME",
                                       "final_url": "https://acme.fr", "status": 200})
    out = lire(url="https://acme.fr", browser=True)
    assert out["chemin"] == "browser"
    assert out["tentatives"][1] == {"cran": "serper", "verdict": "error HTTP 503"}


def test_un_refus_serper_qui_n_est_pas_la_page_leve_toujours(monkeypatch):
    """Une clé refusée (401) est NOTRE panne : elle ne se déguise pas en page illisible."""
    from oto.tools.common.errors import UpstreamHTTPError
    lire = _web_read(monkeypatch, fetch={"ok": False, "verdict": "HTTP 403", "status": 403},
                     serper=_erreur_serper(401))
    with pytest.raises(UpstreamHTTPError):
        lire(url="https://acme.fr")


# ── les liens de la page, sur demande (#1947) ────────────────────────────────

_HTML_LIENS = ("<html><head><title>Clients</title></head><body>"
               + "<p>Nos clients racontent leur histoire, carte après carte.</p>" * 10
               + '<a href="/fr/customers-story/alpha#top"><h3>Alpha</h3> Logistique</a>'
               + '<a href="/fr/customers-story/alpha">Alpha</a>'
               + '<a href="https://www.acme.fr/fr/customers-story/beta">Beta</a>'
               + '<a href="/fr/pricing">Tarifs</a><a href="mailto:x@acme.fr">x</a>'
               + '<a href="javascript:void(0)">menu</a></body></html>')


def test_sans_demande_aucun_lien_n_est_servi(monkeypatch):
    lire = _web_read(monkeypatch, fetch={"ok": True, "status": 200, "html": _HTML_LIENS,
                                         "final_url": "https://www.acme.fr/fr/clients"})
    out = lire(url="https://www.acme.fr/fr/clients")
    assert "links" not in out


def test_links_rend_les_liens_absolus_dedoublonnes(monkeypatch):
    lire = _web_read(monkeypatch, fetch={"ok": True, "status": 200, "html": _HTML_LIENS,
                                         "final_url": "https://www.acme.fr/fr/clients"})
    out = lire(url="https://www.acme.fr/fr/clients", links=True)
    hrefs = [l["href"] for l in out["links"]]
    assert hrefs == ["https://www.acme.fr/fr/customers-story/alpha",
                     "https://www.acme.fr/fr/customers-story/beta",
                     "https://www.acme.fr/fr/pricing"]
    assert out["links"][0]["text"] == "Alpha Logistique"
    assert out["links_total"] == 3 and out["links_truncated"] is False
    assert "customers-story" not in out["content"], "le texte reste du texte"


def test_link_contains_filtre_et_vaut_links(monkeypatch):
    lire = _web_read(monkeypatch, fetch={"ok": True, "status": 200, "html": _HTML_LIENS,
                                         "final_url": "https://www.acme.fr/fr/clients"})
    out = lire(url="https://www.acme.fr/fr/clients", link_contains="/customers-story/")
    assert [l["text"] for l in out["links"]] == ["Alpha Logistique", "Beta"]


def test_le_scraper_rend_les_liens_de_son_markdown(monkeypatch):
    md = "# Clients\n\n" + "Texte. " * 30 + "\n[Alpha](https://acme.fr/customers-story/a) [Tarifs](https://acme.fr/p)"
    lire = _web_read(monkeypatch, fetch={"ok": False, "verdict": "HTTP 403", "status": 403},
                     serper={"markdown": md})
    out = lire(url="https://acme.fr/clients", link_contains="customers-story")
    assert out["chemin"] == "serper"
    assert out["links"] == [{"href": "https://acme.fr/customers-story/a", "text": "Alpha"}]


def test_le_navigateur_lit_le_dom_quand_on_veut_les_liens(monkeypatch):
    vu = {}

    async def _faux(url, as_html=False):
        vu["as_html"] = as_html
        return {"content": _HTML_LIENS, "title": "Clients",
                "final_url": "https://www.acme.fr/fr/clients"}
    lire = _web_read(monkeypatch, fetch={"ok": False, "verdict": "HTTP 403", "status": 403},
                     serper={"markdown": ""})
    monkeypatch.setattr(W.browserbase, "fetch_page_ephemeral", _faux)
    monkeypatch.setattr(W.browserbase, "is_configured", lambda: True)
    out = lire(url="https://www.acme.fr/fr/clients", browser=True, links=True)
    assert vu["as_html"] is True
    assert "<a" not in out["content"] and "Nos clients" in out["content"]
    assert len(out["links"]) == 3
