"""Keystone des axes-contexte d'appel sur tools plats (#108/#112) — axe `account=`.

Trois contrats : exposition SÉLECTIVE du schéma (dérivée du registre), strip+pose de
la ContextVar par le middleware, lecture par le seam de résolution (`resolve_credential`
sélectionne le compte de l'axe en multi-compte — « 2 Zoho »)."""
import pytest

from oto_mcp import access, call_axes, credentials_store, db, session_org
from oto_mcp.middleware import CallContextMiddleware


# ── 1. Exposition sélective (applies) ────────────────────────────────────────

def test_account_axis_applies_to_multi_account_tools():
    assert call_axes.axes_for("zoho_get")          # zoho = multi-compte
    assert call_axes.axes_for("gmail_search")      # google = multi-compte
    assert call_axes.axes_for("tasks_list")
    assert call_axes.axes_for("calendar_events")


def _params(name):
    return {a.param for a in call_axes.axes_for(name)}


def test_account_axis_applies_to_identity_bearing_tools():
    # ADR 0051 : unipile (1 clé partagée → N identités opérées) porte l'axe
    # account=/identity= pour épingler le compte LinkedIn/messagerie à opérer.
    assert "account" in _params("unipile_search")
    assert "account" in _params("whatsapp_send_message")


def test_account_axis_excludes_single_and_spine():
    for name in ("folk_search", "serper_web_search", "pennylane_company",
                 "oto_create_org", "oto_whoami", "data_write"):
        assert "account" not in _params(name), name


def test_inject_schema_adds_optional_account_property():
    base = {"type": "object", "additionalProperties": False,
            "properties": {"id": {"type": "string"}}, "required": ["id"]}
    out = call_axes.inject_schema(base, call_axes.axes_for("zoho_get"))
    assert out["properties"]["account"]["type"] == "string"
    assert "account" not in out.get("required", [])       # jamais requis
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
        _Tool("zoho_get", {"type": "object", "properties": {}}),
        _Tool("folk_search", {"type": "object", "properties": {}}),
    ]

    async def _next(_ctx):
        return tools

    out = await mw.on_list_tools(_Ctx(_Msg("tools/list", {})), _next)
    by = {t.name: t for t in out}
    assert "account" in by["zoho_get"].parameters["properties"]
    assert "account" not in by["folk_search"].parameters["properties"]


@pytest.mark.asyncio
async def test_on_call_tool_strips_axis_and_poses_contextvar():
    mw = CallContextMiddleware(reserved_org_tools=set())
    args = {"id": "42", "account": "boulot"}
    seen = {}

    async def _next(ctx):
        # l'axe a été retiré des arguments AVANT le dispatch (la fn du tool ne le
        # déclare pas) ; la ContextVar est posée pendant l'appel.
        seen["args"] = dict(ctx.message.arguments)
        seen["account"] = session_org.current_call_account()
        return "ok"

    ctx = _Ctx(_Msg("zoho_get", args))
    assert await mw.on_call_tool(ctx, _next) == "ok"
    assert seen["args"] == {"id": "42"}          # account strippé
    assert seen["account"] == "boulot"           # posé pendant l'appel
    assert session_org.current_call_account() is None  # reset après (finally)


@pytest.mark.asyncio
async def test_on_call_tool_ignores_axis_on_non_applicable_tool():
    mw = CallContextMiddleware(reserved_org_tools=set())
    args = {"query": "x", "account": "boulot"}   # folk n'expose pas account=

    async def _next(ctx):
        # non applicable → l'axe n'est PAS strippé (resterait un arg métier si déclaré)
        return dict(ctx.message.arguments)

    out = await mw.on_call_tool(_Ctx(_Msg("folk_search", args)), _next)
    assert out == {"query": "x", "account": "boulot"}
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
