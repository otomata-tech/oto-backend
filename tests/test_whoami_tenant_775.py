"""`oto_whoami` sert désormais le tenant de l'appelant (oto-backend#775, point 3).

Avant ce lot, aucune surface servie ne disait DE QUEL tenant relève l'appelant —
la forme de l'identifiant (`tenant:{slug}:…` sur une instance) était bien servie,
mais pas l'appartenance elle-même. `rung_tenant` est la seule fonction qui sait
répondre (registre en process, aucune lecture DB) : `oto_whoami` la rejoue,
fail-open comme le reste de sa réponse.
"""
from __future__ import annotations

import asyncio

from oto_mcp import tenancy


def _mount(monkeypatch):
    from fastmcp import FastMCP
    from oto_mcp.tools import whoami as whoami_tool

    monkeypatch.setattr(whoami_tool, "current_user_sub_from_token", lambda: "pilote:u1")

    m = FastMCP("t")
    whoami_tool.register(m)
    fn = asyncio.run(m.get_tool("oto_whoami")).fn
    return fn(ctx=None)


def test_un_compte_d_un_tenant_tiers_voit_son_slug(monkeypatch):
    monkeypatch.setattr(tenancy, "_INSTALLED", tenancy.IssuerRegistry(tenancy.build(
        "https://auth.oto.ninja/oidc",
        tenants=[{"slug": "pilote", "issuer": "https://auth.pilote.test/oidc"}])),
        raising=False)
    out = _mount(monkeypatch)
    assert out["tenant"] == "pilote"


def test_un_compte_nu_ne_relevant_du_tenant_primaire_rend_none(monkeypatch):
    from fastmcp import FastMCP
    from oto_mcp.tools import whoami as whoami_tool

    monkeypatch.setattr(whoami_tool, "current_user_sub_from_token", lambda: "u1")
    m = FastMCP("t")
    whoami_tool.register(m)
    fn = asyncio.run(m.get_tool("oto_whoami")).fn
    out = fn(ctx=None)
    assert out["tenant"] is None


def test_un_hoquet_de_resolution_ne_fait_pas_echouer_whoami(monkeypatch):
    """Fail-open, même discipline que le reste de cette réponse (org/group/project) :
    un tenant illisible ne doit jamais transformer `oto_whoami` en 500."""
    from fastmcp import FastMCP
    from oto_mcp import tenant_vault
    from oto_mcp.tools import whoami as whoami_tool

    monkeypatch.setattr(whoami_tool, "current_user_sub_from_token", lambda: "u1")
    def _boom(sub):
        raise RuntimeError("registre indisponible")
    monkeypatch.setattr(tenant_vault, "rung_tenant", _boom)
    m = FastMCP("t")
    whoami_tool.register(m)
    fn = asyncio.run(m.get_tool("oto_whoami")).fn
    out = fn(ctx=None)
    assert out["tenant"] is None
