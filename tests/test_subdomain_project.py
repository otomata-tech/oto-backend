"""Endpoint MCP par projet (`<slug>.mcp.oto.cx`, ADR 0032) — parsing, contexte
anonyme, résolution de credential sans sub, visibilité allowlist, dispatch par Host.
"""
import pytest

from oto_mcp import access, anon_visibility as av, connectors, db, org_store
from oto_mcp import subdomain_project as sp


# ── Parsing du Host ──────────────────────────────────────────────────────────
def test_slug_parsing():
    assert sp._slug_from_host("french-tech-marseille.mcp.oto.cx") == "french-tech-marseille"
    assert sp._slug_from_host("FOO.mcp.oto.cx:443") == "foo"
    assert sp._slug_from_host("mcp.oto.ninja") is None          # host canonique
    assert sp._slug_from_host("mcp.oto.cx") is None             # pas de label de slug
    assert sp._slug_from_host("a.b.mcp.oto.cx") is None         # multi-label ≠ slug
    assert sp._slug_from_host("") is None


def test_slug_parsing_share_domain():
    # `.share.oto.cx` (partage navigable) partage le MÊME dispatch que `.mcp.oto.cx`.
    assert sp._slug_from_host("mon-projet.share.oto.cx") == "mon-projet"
    assert sp._slug_from_host("FOO.share.oto.cx:443") == "foo"
    assert sp._slug_from_host("share.oto.cx") is None          # pas de label de slug
    assert sp._slug_from_host("a.b.share.oto.cx") is None      # multi-label ≠ slug


def test_project_domain_env_driven(monkeypatch):
    # PREPROD (cutover ADR 0040) : OTO_PROJECT_DOMAIN=oto.ninja → routing + audience suivent,
    # le domaine prod (.oto.cx) ne matche plus. C'est le fix du suffixe figé.
    monkeypatch.setenv("OTO_PROJECT_DOMAIN", "oto.ninja")
    assert sp._slug_from_host("mon-projet.share.oto.ninja") == "mon-projet"
    assert sp._slug_from_host("ft.mcp.oto.ninja") == "ft"
    assert sp._slug_from_host("ft.mcp.oto.cx") is None            # plus le domaine courant
    assert sp._is_share_host("x.share.oto.ninja") is True
    assert sp._is_share_host("x.share.oto.cx") is False
    monkeypatch.setattr(db, "get_project_by_mcp_slug",
                        lambda s: {"id": 8, "mcp_access": "org"} if s == "mm" else None)
    assert sp.valid_org_audience("https://mm.mcp.oto.ninja/mcp") is True   # audience sur le domaine courant
    assert sp.valid_org_audience("https://mm.mcp.oto.cx/mcp") is False     # domaine prod rejeté en preprod


def test_connect_url_is_path_aware():
    # `.share.oto.cx` = path `/mcp` explicite (racine = UI) ; `.mcp.oto.cx` = URL nue.
    assert sp._is_share_host("x.share.oto.cx") is True
    assert sp._is_share_host("x.mcp.oto.cx") is False
    assert sp._connect_url("mon-projet.share.oto.cx") == "https://mon-projet.share.oto.cx/mcp"
    assert sp._connect_url("ft.mcp.oto.cx") == "https://ft.mcp.oto.cx"


def test_resolve_project(monkeypatch):
    monkeypatch.setattr(db, "get_project_by_mcp_slug",
                        lambda s: {"id": 7, "mcp_access": "anonymous"} if s == "ft" else None)
    assert sp.resolve_project("ft.mcp.oto.cx") == {"id": 7, "mcp_access": "anonymous"}
    assert sp.resolve_project("nope.mcp.oto.cx") is None
    assert sp.resolve_project("mcp.oto.ninja") is None


# ── Contexte anonyme + seam current_org ──────────────────────────────────────
def test_anon_context_and_current_org_seam():
    ctx = sp.AnonContext(42, 99, frozenset({"frenchtech_search_annuaire"}))
    tok = sp._CTX.set(ctx)
    try:
        assert sp.current_anon_org() == 99
        assert sp.current_allowlist() == {"frenchtech_search_annuaire"}
        assert access.current_org(None) == 99          # seam branché sur l'anon
    finally:
        sp._CTX.reset(tok)
    assert sp.current_anon_org() is None
    assert access.current_org(None) is None            # hors contexte → None


# ── Couplage visibilité ↔ datastore exposé (#193) ────────────────────────────
def test_allowlist_couples_datastore_when_exposed():
    # NON exposé → allowlist = preset nu (le flag découplé n'ajoutait rien : #193).
    tok = sp._CTX.set(sp.AnonContext(1, 99, frozenset({"fr_search"})))
    try:
        assert sp.current_allowlist() == {"fr_search"}
    finally:
        sp._CTX.reset(tok)
    # exposé LECTURE → data_list_namespaces + data_rows ajoutés, PAS l'écriture.
    tok = sp._CTX.set(sp.AnonContext(1, 99, frozenset({"fr_search"}), datastore_exposed=True))
    try:
        allow = sp.current_allowlist()
        assert {"fr_search", "data_list_namespaces", "data_rows"} <= allow
        assert "data_write" not in allow and "data_set_schema" not in allow
        assert sp.current_anon_datastore_exposed() is True
        assert sp.current_anon_datastore_writable() is False
    finally:
        sp._CTX.reset(tok)
    # exposé + WRITE opt-in → data_write + data_set_schema aussi.
    tok = sp._CTX.set(sp.AnonContext(1, 99, frozenset({"fr_search"}),
                                     datastore_exposed=True, datastore_writable=True))
    try:
        allow = sp.current_allowlist()
        assert {"data_write", "data_set_schema"} <= allow
        assert sp.current_anon_datastore_writable() is True
    finally:
        sp._CTX.reset(tok)
    # exposé mais org_id None (projet legacy user-owned) → PAS de data_* (garde).
    tok = sp._CTX.set(sp.AnonContext(1, None, frozenset({"fr_search"}), datastore_exposed=True))
    try:
        assert sp.current_allowlist() == {"fr_search"}
        assert sp.current_anon_datastore_exposed() is False
    finally:
        sp._CTX.reset(tok)


# ── Résolution de credential anonyme (sans sub) ──────────────────────────────
def test_anon_resolve_dispatch(monkeypatch):
    """resolve_credential(sub=None) sous contexte anonyme → _resolve_credential_anon."""
    monkeypatch.setattr(org_store, "get_org_secret",
                        lambda org, prov: "ORGKEY" if (org, prov) == (99, "serper") else None)
    ctx = sp.AnonContext(42, 99, frozenset({"serper_web_search"}))
    tok = sp._CTX.set(ctx)
    try:
        rc = access.resolve_credential("serper", sub=None)
        assert rc.key == "ORGKEY" and rc.mode == "org"
    finally:
        sp._CTX.reset(tok)


def test_anon_resolver_platform_open_key(monkeypatch):
    monkeypatch.setattr(org_store, "get_org_secret", lambda o, p: None)
    # ADR 0044 §F R3 : instance plateforme 'open' (free-tier) servie à l'anon.
    monkeypatch.setattr(access.credentials_store, "list_platform_instances",
                        lambda p: [{"label": "plat", "share_mode": "open", "share_down": [],
                                    "share_side": [], "meta": {}}])
    monkeypatch.setattr(access.credentials_store, "get_credential",
                        lambda et, eid, p, account="": "PLATKEY")
    # serper est platform_key_open dans le registre → clé plateforme ouverte servie
    rc = access._resolve_credential_anon("serper", "auto", 99)
    assert rc.key == "PLATKEY" and rc.is_platform is True


def test_anon_resolver_fail_closed(monkeypatch):
    from mcp.shared.exceptions import McpError
    monkeypatch.setattr(org_store, "get_org_secret", lambda o, p: None)
    monkeypatch.setattr(access.credentials_store, "list_platform_instances", lambda p: [])
    # want=byo → jamais de palier plateforme
    with pytest.raises(McpError):
        access._resolve_credential_anon("attio", "byo", 99)
    # pas d'org propriétaire → refus actionnable
    with pytest.raises(McpError):
        access._resolve_credential_anon("serper", "auto", None)
    # provider inconnu → refus
    with pytest.raises(McpError):
        access._resolve_credential_anon("does_not_exist", "auto", 99)


# ── Visibilité allowlist (fail-closed) ───────────────────────────────────────
class _Tool:
    def __init__(self, name): self.name = name


class _FakeFastMCP:
    def __init__(self, names): self._names = names
    async def list_tools(self, run_middleware=False):
        return [_Tool(n) for n in self._names]


class _FakeCtx:
    def __init__(self, names): self.fastmcp = _FakeFastMCP(names)


class _FakeMwCtx:
    def __init__(self, names): self.fastmcp_context = _FakeCtx(names)


@pytest.mark.asyncio
async def test_anon_visibility_allowlist(monkeypatch):
    hidden = {}

    async def _fake_disable(ctx, names, components):
        hidden["names"] = set(names)

    monkeypatch.setattr(av, "disable_components", _fake_disable)
    allow = frozenset({"frenchtech_search_annuaire", "frenchtech_evenements"})
    monkeypatch.setattr(sp, "_CTX", sp._CTX)  # no-op, lisibilité
    tok = sp._CTX.set(sp.AnonContext(1, 2, allow))
    try:
        mw = av.AnonymousVisibilityMiddleware()
        all_names = ["frenchtech_search_annuaire", "frenchtech_evenements",
                     "serper_web_search", "oto_project", "data_write"]
        await mw.on_initialize(_FakeMwCtx(all_names), lambda c: _async_none())
    finally:
        sp._CTX.reset(tok)
    # seuls les hors-preset sont masqués ; le preset reste visible
    assert hidden["names"] == {"serper_web_search", "oto_project", "data_write"}


@pytest.mark.asyncio
async def test_anon_visibility_exposes_datastore_read(monkeypatch):
    # datastore exposé (lecture) → data_list_namespaces/data_rows VISIBLES au handshake
    # (le bug #193 : le flag rendait résolvable mais laissait masqué). data_write reste
    # masqué sans opt-in write.
    hidden = {}

    async def _fake_disable(ctx, names, components):
        hidden["names"] = set(names)

    monkeypatch.setattr(av, "disable_components", _fake_disable)
    tok = sp._CTX.set(sp.AnonContext(1, 99, frozenset({"fr_search"}), datastore_exposed=True))
    try:
        mw = av.AnonymousVisibilityMiddleware()
        all_names = ["fr_search", "data_list_namespaces", "data_rows",
                     "data_write", "serper_web_search"]
        await mw.on_initialize(_FakeMwCtx(all_names), lambda c: _async_none())
    finally:
        sp._CTX.reset(tok)
    assert hidden["names"] == {"data_write", "serper_web_search"}


async def _async_none():
    return None


# ── Dispatch par Host ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_host_dispatch_routes_anonymous(monkeypatch):
    seen = {}

    async def _authed(scope, receive, send): seen["app"] = "authed"

    async def _anon(scope, receive, send):
        seen["app"] = "anon"
        # le contexte anonyme doit être posé PENDANT l'appel de l'app anonyme
        seen["org"] = sp.current_anon_org()
        seen["allow"] = sp.current_allowlist()

    monkeypatch.setattr(sp, "resolve_project", lambda host: {
        "id": 7, "owner_type": "org", "owner_id": "99",
        "mcp_access": "anonymous", "mcp_tools": ["frenchtech_evenements"]})
    disp = sp.HostDispatch(_authed, _anon)
    scope = {"type": "http", "headers": [(b"host", b"ft.mcp.oto.cx")]}
    await disp(scope, None, None)
    assert seen["app"] == "anon"
    assert seen["org"] == 99
    assert seen["allow"] == {"frenchtech_evenements"}
    # contexte nettoyé après l'appel
    assert sp.current_anon_org() is None


@pytest.mark.asyncio
async def test_host_dispatch_secret_on_share_domain(monkeypatch):
    """`secret` sur `.share.oto.cx` route vers l'app anonyme, identique à `.mcp.oto.cx`
    (équivalence B1) : même contexte anonyme posé, même org propriétaire."""
    sp._BUCKETS.clear()
    seen = {}

    async def _authed(scope, receive, send): seen["app"] = "authed"

    async def _anon(scope, receive, send):
        seen["app"] = "anon"
        seen["org"] = sp.current_anon_org()
        seen["allow"] = sp.current_allowlist()

    monkeypatch.setattr(sp, "resolve_project", lambda host: {
        "id": 12, "owner_type": "org", "owner_id": "99",
        "mcp_access": "secret", "mcp_tools": ["frenchtech_evenements"]})
    disp = sp.HostDispatch(_authed, _anon)
    scope = {"type": "http", "method": "POST", "path": "/mcp",
             "headers": [(b"host", b"mon-projet.share.oto.cx")], "client": ("1.2.3.4", 1)}
    await disp(scope, None, None)
    assert seen["app"] == "anon"
    assert seen["org"] == 99
    assert seen["allow"] == {"frenchtech_evenements"}
    assert sp.current_anon_org() is None


@pytest.mark.asyncio
async def test_host_dispatch_browser_get_serves_ui(monkeypatch):
    """GET navigateur (text/html) sur une route UI → servi par `share_ui`, ni anon ni authed."""
    sp._BUCKETS.clear()
    sent = {}

    async def _authed(scope, receive, send): sent["app"] = "authed"

    async def _anon(scope, receive, send): sent["app"] = "anon"

    async def _send(msg):
        if msg["type"] == "http.response.start":
            sent["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            sent["body"] = msg["body"]

    from oto_mcp import share_ui
    monkeypatch.setattr(sp, "resolve_project", lambda host: {
        "id": 5, "owner_type": "org", "owner_id": "99", "name": "P", "brief_md": "",
        "mcp_access": "secret", "mcp_tools": [], "mcp_expose_datastore": True})
    monkeypatch.setattr(share_ui, "build_page", lambda proj, path, **kw: ("<html>UI</html>", 200))
    disp = sp.HostDispatch(_authed, _anon)
    scope = {"type": "http", "method": "GET", "path": "/procedures/11", "query_string": b"",
             "headers": [(b"host", b"p.share.oto.cx"), (b"accept", b"text/html")]}
    await disp(scope, None, _send)
    assert sent.get("status") == 200 and b"UI" in sent.get("body", b"")
    assert "app" not in sent   # court-circuite le MCP


@pytest.mark.asyncio
async def test_host_dispatch_browser_get_mcp_path_falls_through(monkeypatch):
    """GET text/html sur `/mcp` : `build_page` rend (None,0) → retombe sur l'app MCP anonyme."""
    sp._BUCKETS.clear()
    seen = {}

    async def _authed(scope, receive, send): seen["app"] = "authed"

    async def _anon(scope, receive, send): seen["app"] = "anon"

    from oto_mcp import share_ui
    monkeypatch.setattr(sp, "resolve_project", lambda host: {
        "id": 5, "owner_type": "org", "owner_id": "99",
        "mcp_access": "secret", "mcp_tools": [], "mcp_expose_datastore": False})
    monkeypatch.setattr(share_ui, "build_page", lambda proj, path, **kw: (None, 0))
    disp = sp.HostDispatch(_authed, _anon)
    scope = {"type": "http", "method": "GET", "path": "/mcp", "query_string": b"",
             "headers": [(b"host", b"p.share.oto.cx"), (b"accept", b"text/html")],
             "client": ("1.2.3.4", 1)}
    await disp(scope, None, None)
    assert seen["app"] == "anon"


@pytest.mark.asyncio
async def test_host_dispatch_org_pins_and_uses_authed(monkeypatch):
    from oto_mcp import session_org
    seen = {}

    async def _authed(scope, receive, send):
        seen["app"] = "authed"
        seen["pinned"] = session_org.current_subdomain_candidate()

    async def _anon(scope, receive, send): seen["app"] = "anon"

    monkeypatch.setattr(sp, "resolve_project", lambda host: {
        "id": 8, "owner_type": "org", "owner_id": "35",
        "mcp_access": "org", "mcp_tools": []})
    disp = sp.HostDispatch(_authed, _anon)
    scope = {"type": "http", "headers": [(b"host", b"movinmotion.mcp.oto.cx")]}
    await disp(scope, None, None)
    assert seen["app"] == "authed"
    assert seen["pinned"] == 35


# ── Audience JWT : canonique OU sous-domaine org publié (#44) ────────────────
def test_valid_org_audience(monkeypatch):
    monkeypatch.setattr(db, "get_project_by_mcp_slug",
                        lambda s: {"id": 8, "mcp_access": "org"} if s == "movinmotion"
                        else ({"id": 9, "mcp_access": "anonymous"} if s == "ft" else None))
    assert sp.valid_org_audience("https://movinmotion.mcp.oto.cx/mcp") is True
    assert sp.valid_org_audience("https://ft.mcp.oto.cx/mcp") is False   # anonyme ≠ org
    assert sp.valid_org_audience("https://nope.mcp.oto.cx/mcp") is False # inconnu
    assert sp.valid_org_audience("https://evil.com/mcp") is False        # hors domaine
    assert sp.valid_org_audience("https://movinmotion.mcp.oto.cx") is False  # pas /mcp
    assert sp.valid_org_audience(None) is False


def test_valid_org_audience_share_domain(monkeypatch):
    """Le motif d'audience org accepte aussi `<slug>.share.oto.cx/mcp` (même dispatch)."""
    monkeypatch.setattr(db, "get_project_by_mcp_slug",
                        lambda s: {"id": 8, "mcp_access": "org"} if s == "mon-projet" else None)
    assert sp.valid_org_audience("https://mon-projet.share.oto.cx/mcp") is True
    assert sp.valid_org_audience("https://nope.share.oto.cx/mcp") is False
    assert sp.valid_org_audience("https://mon-projet.share.oto.cx") is False  # pas /mcp


def test_valid_org_audience_fail_closed(monkeypatch):
    def _boom(s): raise RuntimeError("DB down")
    monkeypatch.setattr(db, "get_project_by_mcp_slug", _boom)
    assert sp.valid_org_audience("https://movinmotion.mcp.oto.cx/mcp") is False


def test_verifier_audience_decision(monkeypatch):
    from oto_mcp import server
    v = server._IatGatedVerifier.__new__(server._IatGatedVerifier)
    v._expected_audience = "https://mcp.oto.ninja/mcp"
    # canonique accepté SANS toucher la DB (aud liste ou string)
    monkeypatch.setattr(sp, "valid_org_audience", lambda a: (_ for _ in ()).throw(AssertionError("DB touched")))
    assert v._audience_ok({"aud": "https://mcp.oto.ninja/mcp"}) is True
    assert v._audience_ok({"aud": ["https://mcp.oto.ninja/mcp", "x"]}) is True
    # sous-domaine org → délègue à valid_org_audience
    monkeypatch.setattr(sp, "valid_org_audience", lambda a: a == "https://mm.mcp.oto.cx/mcp")
    assert v._audience_ok({"aud": "https://mm.mcp.oto.cx/mcp"}) is True
    assert v._audience_ok({"aud": "https://other.mcp.oto.cx/mcp"}) is False


def test_verifier_alt_audience(monkeypatch):
    """Audience canonique SECONDAIRE (coexistence multi-domaine, ex. mcp.oto.cx)."""
    from oto_mcp import server
    monkeypatch.setattr(sp, "valid_org_audience", lambda a: (_ for _ in ()).throw(AssertionError("DB touched")))
    v = server._IatGatedVerifier.__new__(server._IatGatedVerifier)
    v._expected_audience = "https://mcp.oto.ninja/mcp"
    v._alt_audiences = frozenset({"https://mcp.oto.cx/mcp"})
    assert v._audience_ok({"aud": "https://mcp.oto.cx/mcp"}) is True                 # alt accepté
    assert v._audience_ok({"aud": ["https://mcp.oto.cx/mcp", "x"]}) is True          # alt en liste
    assert v._audience_ok({"aud": "https://mcp.oto.ninja/mcp"}) is True              # canonique toujours OK
    # _alt_audiences absent (construction __new__ sans __init__) → getattr défaut vide, pas de crash
    v2 = server._IatGatedVerifier.__new__(server._IatGatedVerifier)
    v2._expected_audience = "https://mcp.oto.ninja/mcp"
    assert v2._audience_ok({"aud": "https://mcp.oto.ninja/mcp"}) is True


# ── PRM host-aware (discovery, #44) ──────────────────────────────────────────
def _call_prm(host, valid, monkeypatch):
    import asyncio
    import json as _json
    from starlette.requests import Request
    from oto_mcp import oauth_facade
    monkeypatch.setattr(sp, "valid_org_audience", lambda a: valid)
    routes = oauth_facade.make_routes("https://mcp.oto.ninja", "appid")
    prm = next(r for r in routes if r.path == "/.well-known/oauth-protected-resource/mcp")
    scope = {"type": "http", "method": "GET",
             "path": "/.well-known/oauth-protected-resource/mcp",
             "query_string": b"", "headers": [(b"host", host.encode())]}
    resp = asyncio.new_event_loop().run_until_complete(prm.endpoint(Request(scope)))
    return _json.loads(bytes(resp.body))


def test_prm_canonical(monkeypatch):
    md = _call_prm("mcp.oto.ninja", False, monkeypatch)
    assert md["resource"] == "https://mcp.oto.ninja/mcp"
    assert md["authorization_servers"] == ["https://mcp.oto.ninja/"]
    assert md["resource_name"] == "oto MCP"
    assert md["bearer_methods_supported"] == ["header"]


def test_prm_subdomain_org(monkeypatch):
    md = _call_prm("movinmotion.mcp.oto.cx", True, monkeypatch)
    assert md["resource"] == "https://movinmotion.mcp.oto.cx/mcp"
    assert md["authorization_servers"] == ["https://mcp.oto.ninja/"]   # AS reste canonique


def test_prm_unpublished_subdomain_falls_back(monkeypatch):
    md = _call_prm("evil.mcp.oto.cx", False, monkeypatch)   # non publié → canonique
    assert md["resource"] == "https://mcp.oto.ninja/mcp"


def test_ensure_api_resource_idempotent(monkeypatch):
    from oto_mcp import oauth_facade
    calls = {"post": 0}

    class _Resp:
        def __init__(self, data): self._data = data
        def raise_for_status(self): pass
        def json(self): return self._data

    monkeypatch.setattr(oauth_facade, "_mgmt_token", lambda: "TOK")
    monkeypatch.setattr(oauth_facade, "_logto_base", lambda: "https://auth.x")
    import requests

    def _get(url, **kw): return _Resp([{"indicator": "https://mm.mcp.oto.cx/mcp"}])

    def _post(url, **kw):
        calls["post"] += 1
        return _Resp({"id": "r1"})

    monkeypatch.setattr(requests, "get", _get)
    monkeypatch.setattr(requests, "post", _post)
    # déjà présent → pas de POST
    oauth_facade.ensure_api_resource("https://mm.mcp.oto.cx/mcp")
    assert calls["post"] == 0
    # absent → POST
    oauth_facade.ensure_api_resource("https://new.mcp.oto.cx/mcp")
    assert calls["post"] == 1


# ── Rate-limit anti-abus ─────────────────────────────────────────────────────
def test_rate_limit_bucket(monkeypatch):
    monkeypatch.setenv("OTO_ANON_RATE_PER_MIN", "60")   # 1/s
    monkeypatch.setenv("OTO_ANON_RATE_BURST", "3")
    sp._BUCKETS.clear()
    key = ("1.2.3.4", 7)
    # burst de 3 autorisé instantanément (now figé), puis 429
    assert sp._check_bucket(key, 1000.0) is True
    assert sp._check_bucket(key, 1000.0) is True
    assert sp._check_bucket(key, 1000.0) is True
    assert sp._check_bucket(key, 1000.0) is False
    # 2 s plus tard → 2 jetons rechargés (1/s)
    assert sp._check_bucket(key, 1002.0) is True
    assert sp._check_bucket(key, 1002.0) is True
    assert sp._check_bucket(key, 1002.0) is False
    # une autre IP a son propre bucket
    assert sp._check_bucket(("5.6.7.8", 7), 1000.0) is True


def test_client_ip_priority():
    assert sp._client_ip({}, {b"cf-connecting-ip": b"9.9.9.9"}) == "9.9.9.9"
    assert sp._client_ip({}, {b"x-forwarded-for": b"8.8.8.8, 1.1.1.1"}) == "8.8.8.8"
    assert sp._client_ip({"client": ("7.7.7.7", 5)}, {}) == "7.7.7.7"
    assert sp._client_ip({}, {}) == "unknown"


@pytest.mark.asyncio
async def test_host_dispatch_rate_limits_anon(monkeypatch):
    monkeypatch.setenv("OTO_ANON_RATE_PER_MIN", "60")
    monkeypatch.setenv("OTO_ANON_RATE_BURST", "1")
    sp._BUCKETS.clear()
    calls = {"anon": 0, "status": []}

    async def _authed(scope, receive, send): pass

    async def _anon(scope, receive, send): calls["anon"] += 1

    async def _send(msg):
        if msg["type"] == "http.response.start":
            calls["status"].append(msg["status"])

    monkeypatch.setattr(sp, "resolve_project", lambda host: {
        "id": 7, "owner_type": "org", "owner_id": "99",
        "mcp_access": "anonymous", "mcp_tools": ["frenchtech_evenements"]})
    disp = sp.HostDispatch(_authed, _anon)
    scope = {"type": "http", "headers": [(b"host", b"ft.mcp.oto.cx")],
             "client": ("1.2.3.4", 1)}
    await disp(scope, None, _send)   # 1er : passe
    await disp(scope, None, _send)   # 2e : 429
    assert calls["anon"] == 1
    assert calls["status"] == [429]


@pytest.mark.asyncio
async def test_host_dispatch_canonical_passthrough(monkeypatch):
    seen = {}

    async def _authed(scope, receive, send): seen["app"] = "authed"

    async def _anon(scope, receive, send): seen["app"] = "anon"

    monkeypatch.setattr(sp, "resolve_project", lambda host: None)
    disp = sp.HostDispatch(_authed, _anon)
    scope = {"type": "http", "headers": [(b"host", b"mcp.oto.ninja")]}
    await disp(scope, None, None)
    assert seen["app"] == "authed"
