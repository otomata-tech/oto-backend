"""Keystone des axes-contexte d'appel sur tools plats (#108/#112) — axe `_account=`.

Trois contrats : exposition SÉLECTIVE du schéma (dérivée du registre), strip+pose de
la ContextVar par le middleware, lecture par le seam de résolution (`resolve_credential`
sélectionne le compte de l'axe en multi-compte — « 2 Zoho »)."""
import pytest
from mcp.shared.exceptions import McpError

from oto_mcp import access, call_axes, credentials_store, db, session_org
from oto_mcp.middleware.call_context import CallContextMiddleware


# ── 1. Exposition sélective (applies) ────────────────────────────────────────

def test_account_axis_applies_to_multi_account_tools():
    assert call_axes.axes_for("zoho_record")          # zoho = multi-compte
    assert call_axes.axes_for("gmail_message")      # google = multi-compte
    assert call_axes.axes_for("tasks_task")
    assert call_axes.axes_for("calendar_event")


def _params(name):
    return {a.param for a in call_axes.axes_for(name)}


def test_account_axis_applies_to_identity_bearing_tools():
    # ADR 0051 : unipile (1 clé partagée → N identités opérées) porte l'axe
    # _account= pour épingler le compte LinkedIn/messagerie à opérer.
    assert "_account" in _params("linkedin_search")
    assert "_account" in _params("whatsapp_chat")


def test_account_axis_excludes_single_and_spine():
    # STATIQUEMENT, l'axe reste curé (budget du handshake, test_call_axes_budget) :
    # serper/pennylane sont multi-compte par défaut depuis 2026-08-25 mais ne
    # l'ANNONCENT que si l'appelant détient plusieurs clés (dynamique, ci-dessous)
    # — et l'ACCEPTENT toujours à l'appel (`axes_for_call`).
    for name in ("serper_search", "pennylane_ref",
                 "oto_create_org", "oto_whoami", "data_write"):
        assert "_account" not in _params(name), name


def test_account_axis_accepted_on_every_keyed_tool():
    # Fonctionnel : tout connecteur à clé d'API lit `_account=` s'il est fourni.
    assert "_account" in {a.param for a in call_axes.axes_for_call("serper_search")}
    assert "_account" in {a.param for a in call_axes.axes_for_call("hunter_domain_search")}
    assert "_account" not in {a.param for a in call_axes.axes_for_call("oto_whoami")}


def test_account_axis_advertised_where_the_caller_holds_several_keys():
    # Dynamique : annoncé sur serper quand l'appelant a ≥ 2 clés serper, pas sinon.
    assert "_account" in {a.param for a in call_axes.axes_for_listing("serper_search", {"serper"})}
    assert "_account" not in {a.param for a in call_axes.axes_for_listing("serper_search", set())}
    assert "_account" not in {a.param for a in call_axes.axes_for_listing("oto_whoami", {"serper"})}


def test_account_axis_advertised_for_counts_member_rows(monkeypatch):
    from oto_mcp import access, credentials_store
    monkeypatch.setattr(access, "current_org", lambda sub: 7)
    monkeypatch.setattr(credentials_store, "member_id", lambda org, sub: f"{org}:{sub}")
    rows = [{"connector": "serper", "account": ""}, {"connector": "serper", "account": "eu"},
            {"connector": "hunter", "account": ""}, {"connector": "unipile", "account": "a"},
            {"connector": "unipile", "account": "b"}]
    monkeypatch.setattr(credentials_store, "list_credentials", lambda et, eid: rows)
    # serper : 2 clés d'API → annoncé ; hunter : 1 → non ; unipile : hosted, pas
    # multi-credential → non (il porte déjà l'axe statiquement, autre famille).
    assert call_axes.account_axis_advertised_for("u") == {"serper"}
    assert call_axes.account_axis_advertised_for(None) == set()


def test_account_axis_applies_to_folk():
    # folk : N clés API personnelles nommées (ex. plusieurs workspaces Folk),
    # même mécanisme générique que zoho (« 2 Zoho »).
    assert "_account" in _params("folk_record")


def test_inject_schema_adds_optional_account_property():
    base = {"type": "object", "additionalProperties": False,
            "properties": {"id": {"type": "string"}}, "required": ["id"]}
    out = call_axes.inject_schema(base, call_axes.axes_for("zoho_record"))
    assert out["properties"]["_account"]["type"] == "string"
    assert "_account" not in out.get("required", [])       # jamais requis
    assert out["additionalProperties"] is False           # inchangé
    assert base["properties"] == {"id": {"type": "string"}}  # copie, pas de mutation


# ── 2. Middleware : advertise + strip + pose ─────────────────────────────────

class _Tool:
    """Double minimal d'un Tool FastMCP (name + parameters + model_copy)."""
    def __init__(self, name, parameters):
        self.name = name
        self.parameters = parameters

    def model_copy(self, update):
        return _Tool(self.name, update.get("parameters", self.parameters))


class _Msg:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _Ctx:
    def __init__(self, msg):
        self.message = msg


@pytest.mark.asyncio
async def test_on_list_tools_advertises_only_where_applicable():
    mw = CallContextMiddleware(reserved_org_tools=set())
    tools = [
        _Tool("zoho_record", {"type": "object", "properties": {}}),
        _Tool("serper_search", {"type": "object", "properties": {}}),
    ]

    async def _next(_ctx):
        return tools

    # Sans sub (pas de token) → seuls les axes statiques : serper n'annonce rien.
    out = await mw.on_list_tools(_Ctx(_Msg("tools/list", {})), _next)
    by = {t.name: t for t in out}
    assert "_account" in by["zoho_record"].parameters["properties"]
    assert "_account" not in by["serper_search"].parameters["properties"]


@pytest.mark.asyncio
async def test_on_list_tools_advertises_account_where_the_caller_has_several_keys(monkeypatch):
    from oto_mcp.middleware import call_context as mwmod
    mw = CallContextMiddleware(reserved_org_tools=set())
    tools = [_Tool("serper_search", {"type": "object", "properties": {}}),
             _Tool("hunter_domain_search", {"type": "object", "properties": {}})]

    async def _next(_ctx):
        return tools

    monkeypatch.setattr(mwmod, "current_user_sub_from_token", lambda: "u")
    monkeypatch.setattr(call_axes, "account_axis_advertised_for", lambda sub: {"serper"})
    out = await mw.on_list_tools(_Ctx(_Msg("tools/list", {})), _next)
    by = {t.name: t for t in out}
    assert "_account" in by["serper_search"].parameters["properties"]
    assert "_account" not in by["hunter_domain_search"].parameters["properties"]


@pytest.mark.asyncio
async def test_on_call_tool_pins_account_on_keyed_tool_even_if_not_advertised():
    mw = CallContextMiddleware(reserved_org_tools=set())
    seen = {}

    async def _next(ctx):
        seen["args"] = dict(ctx.message.arguments)
        seen["account"] = session_org.current_call_account()
        return "ok"

    assert await mw.on_call_tool(_Ctx(_Msg("serper_search", {"q": "x", "_account": "eu"})), _next) == "ok"
    assert seen["args"] == {"q": "x"}
    assert seen["account"] == "eu"
    assert session_org.current_call_account() is None


@pytest.mark.asyncio
async def test_on_call_tool_strips_axis_and_poses_contextvar():
    mw = CallContextMiddleware(reserved_org_tools=set())
    args = {"id": "42", "_account": "boulot"}
    seen = {}

    async def _next(ctx):
        # l'axe a été retiré des arguments AVANT le dispatch (la fn du tool ne le
        # déclare pas) ; la ContextVar est posée pendant l'appel.
        seen["args"] = dict(ctx.message.arguments)
        seen["account"] = session_org.current_call_account()
        return "ok"

    ctx = _Ctx(_Msg("zoho_record", args))
    assert await mw.on_call_tool(ctx, _next) == "ok"
    assert seen["args"] == {"id": "42"}          # _account strippé
    assert seen["account"] == "boulot"           # posé pendant l'appel
    assert session_org.current_call_account() is None  # reset après (finally)


@pytest.mark.asyncio
async def test_on_call_tool_ignores_axis_on_non_applicable_tool():
    mw = CallContextMiddleware(reserved_org_tools=set())
    args = {"query": "x", "_account": "boulot"}  # oto_whoami n'expose pas _account=

    async def _next(ctx):
        # non applicable → l'axe n'est PAS strippé (resterait un arg métier si déclaré)
        return dict(ctx.message.arguments)

    out = await mw.on_call_tool(_Ctx(_Msg("oto_whoami", args)), _next)
    assert out == {"query": "x", "_account": "boulot"}
    assert session_org.current_call_account() is None


# ── 3. Seam de résolution : account= sélectionne le compte membre ────────────

def _wire_multi_account(monkeypatch, provider, org, sub, accounts, keys):
    """Stub le coffre + org active pour un provider multi-compte."""
    monkeypatch.setattr(access, "current_org", lambda s: org)
    monkeypatch.setattr(access, "require_connector_access", lambda *a, **k: None)
    monkeypatch.setattr(access, "_is_multi_account", lambda p: True)
    monkeypatch.setattr(access, "project_pinned_identity", lambda p, project_id=None: None)
    monkeypatch.setattr(credentials_store, "list_accounts",
                        lambda et, eid, con: [{"account": a} for a in accounts])
    monkeypatch.setattr(credentials_store, "member_id", lambda o, s: f"{o}:{s}")
    monkeypatch.setattr(db, "get_member_api_key",
                        lambda s, o, p, account="": keys.get(account))
    monkeypatch.setattr(db, "member_instance_suspended", lambda *a, **k: False)


def test_resolve_reads_account_axis(monkeypatch):
    _wire_multi_account(monkeypatch, "zoho", 7, "u", ["boulot", "perso"],
                        {"boulot": "K_BOULOT", "perso": "K_PERSO"})
    tok = session_org.set_call_account("perso")
    try:
        rc = access.resolve_credential("zoho", want="auto", sub="u")
    finally:
        session_org.reset_call_account(tok)
    assert rc.key == "K_PERSO"
    assert rc.account == "perso"


def test_explicit_account_param_beats_axis(monkeypatch):
    _wire_multi_account(monkeypatch, "zoho", 7, "u", ["boulot", "perso"],
                        {"boulot": "K_BOULOT", "perso": "K_PERSO"})
    tok = session_org.set_call_account("perso")
    try:
        rc = access.resolve_credential("zoho", want="auto", sub="u", account="boulot")
    finally:
        session_org.reset_call_account(tok)
    assert rc.key == "K_BOULOT"      # param explicite prime sur l'axe


# ── 4. Sans pin, 2+ comptes : le défaut (`meta.is_default`) tranche ──────────

def _wire_multi_account_with_meta(monkeypatch, provider, org, sub, accounts_meta, keys):
    """Comme `_wire_multi_account`, mais chaque compte porte son `meta` (pour
    exercer la résolution par défaut `oto_identity(op='set')` → `meta.is_default`,
    lue par `_member_fetch` avant de lever l'ambiguïté)."""
    monkeypatch.setattr(access, "current_org", lambda s: org)
    monkeypatch.setattr(access, "require_connector_access", lambda *a, **k: None)
    monkeypatch.setattr(access, "_is_multi_account", lambda p: True)
    monkeypatch.setattr(access, "project_pinned_identity", lambda p, project_id=None: None)
    monkeypatch.setattr(credentials_store, "list_accounts",
                        lambda et, eid, con: [{"account": a, "meta": m} for a, m in accounts_meta])
    monkeypatch.setattr(credentials_store, "member_id", lambda o, s: f"{o}:{s}")
    monkeypatch.setattr(db, "get_member_api_key",
                        lambda s, o, p, account="": keys.get(account))
    monkeypatch.setattr(db, "member_instance_suspended", lambda *a, **k: False)


def test_no_pin_resolves_to_the_marked_default(monkeypatch):
    _wire_multi_account_with_meta(
        monkeypatch, "folk", 42, "u",
        [("Julien's access - Tangible", {"is_default": True}), ("Second key", {})],
        {"Julien's access - Tangible": "K_DEFAULT", "Second key": "K_OTHER"})
    rc = access.resolve_credential("folk", want="auto", sub="u")
    assert rc.key == "K_DEFAULT"
    assert rc.account == "Julien's access - Tangible"


def test_no_pin_and_no_default_raises_ambiguity_error(monkeypatch):
    _wire_multi_account_with_meta(
        monkeypatch, "folk", 42, "u",
        [("boulot", {}), ("perso", {})],
        {"boulot": "K_BOULOT", "perso": "K_PERSO"})
    with pytest.raises(McpError, match="Plusieurs comptes"):
        access.resolve_credential("folk", want="auto", sub="u")


def test_no_pin_and_two_defaults_still_raises(monkeypatch):
    # Ne devrait jamais arriver en pratique (`_keyed_select` pose un défaut
    # UNIQUE), mais la résolution ne doit jamais deviner entre deux défauts
    # concurrents plutôt que de lever.
    _wire_multi_account_with_meta(
        monkeypatch, "folk", 42, "u",
        [("boulot", {"is_default": True}), ("perso", {"is_default": True})],
        {"boulot": "K_BOULOT", "perso": "K_PERSO"})
    with pytest.raises(McpError, match="Plusieurs comptes"):
        access.resolve_credential("folk", want="auto", sub="u")
