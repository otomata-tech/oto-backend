"""Domaine doctrine/instructions d'org migré en capacités (ADR 0009).

Couvre : préservation du contrat REST `/api/me/instructions*`, présence des outils
MCP (membre + admin), la garde anti-dérive sur le nom d'outil compté par l'usage
(le bug d'origine), le combinateur d'autz `ORG_ADMIN`, et le support des handlers
asynchrones par l'adaptateur (doctrine + manifeste).
"""
import asyncio

import pytest
from pydantic import BaseModel

from oto_mcp.capabilities import _authz, _mcp_adapter, registry
from oto_mcp.capabilities import orgs_instructions as oi
from oto_mcp.capabilities._types import AuthzDenied, Capability, RawCtx, ResolvedCtx


class _Empty(BaseModel):
    pass


# ── Contrat REST préservé (consommé par oto-dashboard) ──────────────────────
def test_member_instruction_routes_preserved():
    pairs = {(b.verb, b.path) for c in registry.caps_with_rest() for b in c.rest_bindings()}
    for vp in [
        ("GET", "/api/me/instructions"),
        ("GET", "/api/me/instructions/{slug}"),
        ("PUT", "/api/me/instructions/{slug}"),
        ("DELETE", "/api/me/instructions/{slug}"),
        ("GET", "/api/me/instructions/{slug}/versions"),
        ("GET", "/api/me/instructions/{slug}/usage"),
        ("POST", "/api/me/instructions/{slug}/revert"),
    ]:
        assert vp in pairs, vp


def test_doctrine_mcp_tools_present():
    # ADR 0047 B2 : la face MCP est consolidée — oto_procedure (membre + bibliothèque)
    # et oto_admin_doctrine (palier admin) remplacent les 8 tools par-verbe.
    names = {c.mcp for c in registry.caps_with_mcp()}
    assert "oto_procedure" in names
    assert "oto_admin_doctrine" in names
    for n in [
        "oto_get_doctrine", "oto_list_doctrines", "oto_set_doctrine", "oto_delete_doctrine",
        "oto_admin_get_doctrine", "oto_admin_list_doctrines",
        "oto_admin_set_doctrine", "oto_admin_delete_doctrine",
    ]:
        assert n not in names, n


# ── Garde anti-dérive (le bug d'origine) ────────────────────────────────────
def test_usage_tool_name_is_a_mounted_tool():
    """L'outil interrogé par l'usage doctrine DOIT être un tool MCP réellement
    monté — sinon le filtre `tool_calls` renvoie 0 (cause du bug initial)."""
    names = {c.mcp for c in registry.caps_with_mcp()}
    assert oi._DOCTRINE_GET_TOOL in names


# ── Combinateur d'autz ORG_ADMIN (org active) ───────────────────────────────
def test_org_admin_active_combinator(monkeypatch):
    monkeypatch.setattr(_authz.access, "get_user_role", lambda sub: "member")

    # L'org active est résolue via le seam access.current_org (ADR 0023 : org de
    # session ?? maison) — c'est lui qu'on simule, plus org_store directement.
    monkeypatch.setattr(_authz.access, "current_org", lambda sub: None)
    with pytest.raises(AuthzDenied) as e:
        _authz.ORG_ADMIN(RawCtx(sub="u1"))
    assert e.value.status == 400 and e.value.code == "no_active_org"

    monkeypatch.setattr(_authz.access, "current_org", lambda sub: 7)
    monkeypatch.setattr(_authz.roles, "is_org_admin", lambda sub, oid: False)
    with pytest.raises(AuthzDenied) as e:
        _authz.ORG_ADMIN(RawCtx(sub="u1"))
    assert e.value.status == 403

    monkeypatch.setattr(_authz.roles, "is_org_admin", lambda sub, oid: True)
    ctx = _authz.ORG_ADMIN(RawCtx(sub="u1"))
    assert ctx.org_id == 7 and ctx.sub == "u1"


# ── Support handler asynchrone par l'adaptateur MCP ─────────────────────────
def test_mcp_adapter_awaits_async_handler(monkeypatch):
    monkeypatch.setattr(_mcp_adapter, "current_user_sub_from_token", lambda: "u1")

    async def h(ctx, inp):
        return {"ok": True, "sub": ctx.sub}

    cap = Capability(
        key="t.async", handler=h, Input=_Empty,
        authz=lambda raw, inp: ResolvedCtx(sub="u1"), mcp="t_async",
    )
    tool = _mcp_adapter._make_tool(cap)
    assert asyncio.run(tool()) == {"ok": True, "sub": "u1"}


# ── Contexte d'instance d'un projet actif (ADR 0032 §5, B3.2) ───────────────
def _wire_project(monkeypatch, pid):
    monkeypatch.setattr(oi.access, "current_project", lambda: pid)
    monkeypatch.setattr(oi.db, "get_project_by_id",
                        lambda p: {"id": p, "name": "Démo B3"} if p else None)
    monkeypatch.setattr(oi.db, "list_project_links", lambda p: [
        {"target_type": "connecteur", "target_ref": "fr", "label": "FR",
         "role": "enrichissement", "config": {"identity_id": "acc_1"}},
        {"target_type": "tableau", "target_ref": "9", "label": "Leads",
         "role": None, "config": {}},
    ])


def test_project_instance_none_without_project(monkeypatch):
    _wire_project(monkeypatch, None)
    assert oi._project_instance(member_mode=True) is None


def test_project_instance_none_in_admin_mode(monkeypatch):
    _wire_project(monkeypatch, 7)
    assert oi._project_instance(member_mode=False) is None   # bracelet = notion membre


def test_project_instance_lists_entities(monkeypatch):
    _wire_project(monkeypatch, 7)
    pi = oi._project_instance(member_mode=True)
    assert pi["project_id"] == 7 and pi["name"] == "Démo B3"
    refs = {e["target_ref"] for e in pi["entities"]}
    assert refs == {"fr", "9"}
    fr = next(e for e in pi["entities"] if e["target_ref"] == "fr")
    assert fr["config"]["identity_id"] == "acc_1"


def test_get_doctrine_includes_project_instance(monkeypatch):
    _wire_project(monkeypatch, 7)
    monkeypatch.setattr(oi.org_store, "get_instruction",
                        lambda org, slug, version=None: {"slug": "prospection", "title": "T",
                                                         "description": "d", "version": 1, "body_md": "…"})

    async def _manifest(*a, **k):
        return []
    monkeypatch.setattr(oi.tool_registry, "manifest_for", _manifest)
    out = asyncio.run(oi._get_doctrine(ResolvedCtx(sub="u1", org_id=3),
                                       oi.DoctrineGetInput(slug="prospection")))
    assert out["slug"] == "prospection"
    assert out["project_instance"]["project_id"] == 7


def test_get_doctrine_no_project_no_instance(monkeypatch):
    _wire_project(monkeypatch, None)
    monkeypatch.setattr(oi.org_store, "get_instruction",
                        lambda org, slug, version=None: {"slug": "prospection", "title": "T",
                                                         "description": "d", "version": 1, "body_md": "…"})

    async def _manifest(*a, **k):
        return []
    monkeypatch.setattr(oi.tool_registry, "manifest_for", _manifest)
    out = asyncio.run(oi._get_doctrine(ResolvedCtx(sub="u1", org_id=3),
                                       oi.DoctrineGetInput(slug="prospection")))
    assert "project_instance" not in out
