"""Le refus du périmètre parle EN PREMIER (#632) — avant toute règle interne du connecteur.

Constat (campagne, 29/08/2026) : `serper_scrape` sur un profil personnel a été refusé par
une règle interne du client (« se lit avec les outils `unipile_*` ») — un refus qui
nomme une porte, et une porte que l'appelant n'a pas forcément. Quand le périmètre du
projet (#605) exclut l'URL, c'est LUI qui doit répondre : il dit la vraie raison
(« exclue par le périmètre du projet … motif `linkedin.com/in/` ») et n'indique aucun
autre outil.

Deux preuves, pour chaque outil d'extraction couvert :

1. **Comportement** — sous périmètre, avec une entrée qui déclencherait AUSSI une règle
   interne (validation d'un autre paramètre, validation d'hôte, taille du lot, clé
   absente, substrat non configuré, règle du client amont), c'est le message du
   périmètre qui sort, et la règle interne n'a pas été consultée.
2. **Structure** (cliquet AST) — dans chaque handler couvert, le premier geste sur
   l'entrée est le seam : rien ne précède `url_perimeter.refuse_*` sinon la docstring
   et la résolution du périmètre. Sans ce cliquet, une validation ajoutée « en tête »
   par une main suivante repasserait devant, en silence.
"""
from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from oto_mcp.mcp_errors import McpError
from oto_mcp import url_perimeter as up

PER = up.Perimeter(project_id=12, project_name="Campagne",
                   prefixes=(up.parse_prefix("linkedin.com/in/"),))
PROFILE = "https://fr.linkedin.com/in/x"
PROFILE_SANS_SCHEMA = "fr.linkedin.com/in/x"
_PKG = Path(up.__file__).resolve().parent


@pytest.fixture
def perimetre(monkeypatch):
    monkeypatch.setattr(up, "perimeter_of_call", lambda: PER)
    return PER


@pytest.fixture
def sans_cle(monkeypatch):
    """Aucun credential résolvable : une règle interne qui parlerait AVANT le périmètre."""
    def _boom(*a, **k):
        raise AssertionError("credential résolu avant le périmètre")
    monkeypatch.setattr("oto_mcp.access.resolve_api_key", _boom)


def _tool(module, name):
    from fastmcp import FastMCP
    m = FastMCP("t")
    module.register(m)
    return asyncio.run(m.get_tool(name)).fn


def _assert_perimeter_spoke(exc: McpError) -> None:
    """Le message est celui du seam : le motif et le projet, aucun outil nommé."""
    msg = str(exc.value)
    assert "linkedin.com/in/" in msg and "Campagne" in msg, msg
    assert "exclu par le périmètre du projet" in msg, msg
    # aucune porte : ni un nom d'outil (`xxx_yyy`), ni une famille (`xxx_*`)
    assert not re.search(r"(?<![`\w])[a-z]+_(?:[a-z_]+|\*)\b", msg.replace("excluded_url_prefixes", "")), msg


# ── serper ────────────────────────────────────────────────────────────────────

def test_serper_scrape_perimeter_precedes_its_own_param_validation(perimetre, monkeypatch):
    """`format` invalide ET URL exclue : le périmètre parle, pas « format invalide »."""
    from oto_mcp.tools import serper
    monkeypatch.setattr("oto.tools.serper.SerperClient", lambda **kw: MagicMock())
    monkeypatch.setattr("oto_mcp.access.resolve_api_key", lambda p, account=None: ("k", False))
    with pytest.raises(McpError) as e:
        _tool(serper, "serper_scrape")(url=PROFILE, format="pdf")
    _assert_perimeter_spoke(e)


@pytest.mark.exige_pin_oto_core
def test_serper_scrape_perimeter_precedes_the_clients_linkedin_rule(monkeypatch):
    """Avec le VRAI client : sous périmètre, la règle interne du client (pages LinkedIn)
    n'est pas même consultée ; sans périmètre, elle refuse AVANT tout réseau — c'est
    l'ordre « périmètre → règle du client → réseau », indépendant du texte de la règle
    (qui vit dans oto-core, épinglé par tag).

    ⚠️ Marqué `exige_pin_oto_core` depuis le 03/09 : il appelle le vrai
    `scrape_page`, dont la signature a gagné `timeout_s` en v1.108.0. Sur un venv
    resté en deçà, son rouge accuse ce dépôt pour un client qui n'est pas le sien."""
    from oto.tools.serper import SerperClient
    from oto_mcp.tools import serper
    monkeypatch.setattr("oto_mcp.access.resolve_api_key", lambda p, account=None: ("k", False))

    def _no_network(self, *a, **k):
        raise AssertionError("réseau atteint")
    monkeypatch.setattr(SerperClient, "_post", _no_network)
    spy = MagicMock(wraps=SerperClient._refuses_scraping)
    monkeypatch.setattr(SerperClient, "_refuses_scraping", spy)
    fn = _tool(serper, "serper_scrape")

    monkeypatch.setattr(up, "perimeter_of_call", lambda: PER)
    with pytest.raises(McpError) as e:
        fn(url=PROFILE)
    _assert_perimeter_spoke(e)
    spy.assert_not_called()

    monkeypatch.setattr(up, "perimeter_of_call", lambda: None)
    # Hors périmètre, la règle du domaine refuse — et depuis `23ba7af0` (#473) elle
    # refuse en `McpError`, plus en `RuntimeError` nu.
    #
    # ⚠️ Ce que ce banc garde n'a PAS changé : l'ordre « périmètre → règle du domaine →
    # réseau », et le fait que rien ne parte sur le réseau. Ce qui a changé est QUI
    # lève, et c'est délibéré : un `RuntimeError` nu tombe en branche « interne » de
    # `error_taxonomy` et devient « Erreur interne du serveur. » **sans écho du
    # message** — le modèle recevait « erreur interne » là où il devait lire « cherche
    # une autre source », et abandonnait au lieu de contourner (6 220 appels par
    # semaine sur cet outil).
    #
    # ⚠️ Les deux exigences ne se contredisent pas, et il faut voir pourquoi : #632
    # reproche à la règle du client de nommer une porte que l'appelant n'a pas — mais
    # SOUS périmètre, où le projet a mieux à dire. C'est exactement ce que la première
    # moitié de ce banc continue d'exiger (`spy.assert_not_called()`). Hors périmètre,
    # il n'y a rien de mieux à dire que la règle du domaine : la servir en clair vaut
    # mieux que la remplacer par « erreur interne ».
    with pytest.raises(McpError) as e2:          # la règle du domaine, hors périmètre
        fn(url=PROFILE)
    assert PROFILE in str(e2.value)
    spy.assert_called_once()


def test_serper_lens_perimeter_precedes_credential_resolution(perimetre, sans_cle):
    from oto_mcp.tools import serper
    with pytest.raises(McpError) as e:
        _tool(serper, "serper_lens")(url="https://linkedin.com/in/x/photo.jpg")
    _assert_perimeter_spoke(e)


# ── browser ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,extra", [("browser_fetch", {}), ("browser_eval", {"js": "async () => 1"})])
def test_browser_tools_perimeter_precedes_host_validation(perimetre, name, extra):
    """Une URL sans schéma est refusée par la validation d'hôte du connecteur ET par le
    périmètre (qui lit `hôte/chemin` comme https) : le périmètre parle en premier."""
    from oto_mcp.tools import browser
    fn = _tool(browser, name)
    with pytest.raises(McpError) as e:
        asyncio.run(fn(url=PROFILE_SANS_SCHEMA, **extra))
    _assert_perimeter_spoke(e)


@pytest.mark.parametrize("name,extra", [("browser_fetch", {}), ("browser_eval", {"js": "async () => 1"})])
def test_browser_tools_perimeter_precedes_substrate_check(perimetre, monkeypatch, name, extra):
    from oto_mcp.tools import browser
    monkeypatch.setattr(browser.browserbase, "is_configured", lambda: False)
    with pytest.raises(McpError) as e:
        asyncio.run(_tool(browser, name)(url=PROFILE, **extra))
    _assert_perimeter_spoke(e)


# ── tavily ────────────────────────────────────────────────────────────────────

def test_tavily_extract_perimeter_precedes_batch_size_rule(perimetre, sans_cle):
    """21 URLs dont un profil : « 20 maximum » serait une porte (scinder le lot) ; le
    périmètre dit la vraie raison."""
    from oto_mcp.tools import tavily
    with pytest.raises(McpError) as e:
        _tool(tavily, "tavily_extract")(urls=[PROFILE] + ["https://acme.fr/p"] * 20)
    _assert_perimeter_spoke(e)


@pytest.mark.parametrize("name", ["tavily_map", "tavily_crawl"])
def test_tavily_map_crawl_perimeter_precedes_limit_validation(perimetre, sans_cle, name):
    from oto_mcp.tools import tavily
    with pytest.raises(McpError) as e:
        _tool(tavily, name)(url=PROFILE, limit=0)
    _assert_perimeter_spoke(e)


# ── firecrawl ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,kw", [
    ("firecrawl_scrape", {"url": PROFILE}),
    ("firecrawl_map", {"url": PROFILE}),
    ("firecrawl_crawl", {"url": PROFILE}),
    ("firecrawl_extract", {"urls": ["https://acme.fr", PROFILE]}),
])
def test_firecrawl_tools_perimeter_precedes_credential_resolution(perimetre, sans_cle, name, kw):
    from oto_mcp.tools import firecrawl
    with pytest.raises(McpError) as e:
        _tool(firecrawl, name)(**kw)
    _assert_perimeter_spoke(e)


# ── web_read / file_source ────────────────────────────────────────────────────

class _Reg:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco(a[0]) if a and callable(a[0]) else deco


def test_web_read_perimeter_precedes_everything_else(perimetre, monkeypatch):
    from oto_mcp.tools import web as W

    def _boom(*a, **k):
        raise AssertionError("réseau atteint avant le périmètre")
    monkeypatch.setattr(W, "_fetch_http", _boom)
    monkeypatch.setattr(W, "check_url_public", _boom)
    reg = _Reg()
    W.register(reg)
    with pytest.raises(McpError) as e:
        asyncio.run(reg.tools["web_read"](url=PROFILE, max_chars=0))
    _assert_perimeter_spoke(e)


def test_file_source_url_perimeter_precedes_anti_ssrf_resolution(perimetre, monkeypatch):
    """`_assert_public_host` résout l'hôte (DNS) : sans réseau il refusait AVANT le
    périmètre (« hôte non résolu ») — l'ordre dépendait du réseau."""
    import socket

    from oto_mcp import file_source

    def _boom(*a, **k):
        raise AssertionError("DNS consulté avant le périmètre")
    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    with pytest.raises(McpError) as e:
        file_source.resolve({"kind": "url", "url": PROFILE})
    _assert_perimeter_spoke(e)


# ── cliquet structurel : le seam est le PREMIER geste du handler ─────────────

_HANDLERS = {
    "tools/serper.py": ("serper_lens", "serper_scrape"),
    "tools/web.py": ("web_read",),
    "tools/browser.py": ("browser_fetch", "browser_eval"),
    "tools/firecrawl.py": ("firecrawl_map", "firecrawl_scrape", "firecrawl_crawl",
                           "firecrawl_extract"),
    "tools/tavily.py": ("tavily_extract", "tavily_map", "tavily_crawl"),
    "file_source.py": ("_from_url",),
}
_REFUSE = {"refuse_if_excluded", "refuse_if_any_excluded"}


def _is_seam_call(node: ast.AST, names: set[str]) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "url_perimeter" and node.func.attr in names)


def _resolves_perimeter(value: ast.AST) -> bool:
    """`url_perimeter.perimeter_of_call()` ou `await asyncio.to_thread(url_perimeter.perimeter_of_call)`."""
    if isinstance(value, ast.Await):
        value = value.value
    if _is_seam_call(value, {"perimeter_of_call"}):
        return True
    return (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
            and value.func.attr == "to_thread" and value.args
            and isinstance(value.args[0], ast.Attribute)
            and isinstance(value.args[0].value, ast.Name)
            and value.args[0].value.id == "url_perimeter"
            and value.args[0].attr == "perimeter_of_call")


def _prelude_ok(stmt: ast.stmt) -> bool:
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
        return True                                             # docstring
    if isinstance(stmt, ast.Assign) and _resolves_perimeter(stmt.value):
        return True                                             # per = …
    return False


def _function(rel: str, name: str):
    tree = ast.parse((_PKG / rel).read_text(encoding="utf-8"))
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    raise AssertionError(f"{rel}: handler `{name}` introuvable")


@pytest.mark.parametrize("rel,name", [(r, n) for r, ns in sorted(_HANDLERS.items()) for n in ns])
def test_the_seam_is_the_first_thing_a_covered_handler_does(rel, name):
    fn = _function(rel, name)
    for stmt in fn.body:
        if isinstance(stmt, ast.Expr) and _is_seam_call(stmt.value, _REFUSE):
            return
        assert _prelude_ok(stmt), (
            f"`{rel}:{name}` fait autre chose avant `url_perimeter.refuse_*` (ligne "
            f"{stmt.lineno}). Le refus du périmètre parle en PREMIER : il dit la vraie "
            f"raison et n'ouvre aucune porte ; une validation qui passe devant lui rend "
            f"un refus qui en ouvre une (#632).")
    raise AssertionError(f"`{rel}:{name}` n'appelle pas `url_perimeter.refuse_*` en tête")
