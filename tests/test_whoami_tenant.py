"""`oto_whoami` sert le TENANT de l'org active (oto-backend#775).

Audit d'expérience du 01/09/2026 : « tenant » apparaît 17 fois dans le contrat
servi (niveaux de connecteurs, propriétaire d'instance), sans qu'aucune surface
ne dise à l'appelant DUQUEL il relève. Le rectificatif du 06/09 a clos le débat
sur le mot lui-même (il est juste — un décalage de point de vue, pas une
inversion) et recentré sur trois gestes additifs, dont celui-ci : servir le
tenant à l'appelant, sur une surface qu'il rencontre sans le chercher.
"""
from __future__ import annotations

import asyncio

import pytest


def _mount(monkeypatch, *, active_org=None, tenant_slug=None):
    from fastmcp import FastMCP
    from oto_mcp import access
    from oto_mcp.tools import whoami as whoami_tool
    from oto_mcp.db import tenants as db_tenants

    monkeypatch.setattr(whoami_tool, "current_user_sub_from_token", lambda: "u")
    monkeypatch.setattr(access, "status_for", lambda sub: {"providers": {}})
    monkeypatch.setattr(access, "current_org", lambda sub: active_org)
    if tenant_slug is not None:
        monkeypatch.setattr(db_tenants, "org_tenant_slug", lambda org_id: tenant_slug)

    m = FastMCP("t")
    whoami_tool.register(m)
    fn = asyncio.run(m.get_tool("oto_whoami")).fn
    return fn(ctx=None)


def test_pas_d_org_active_pas_de_tenant(monkeypatch):
    """Espace perso : pas d'org, donc pas de tenant à dire — `null`, pas `'oto'`
    par défaut, qui affirmerait une appartenance qui n'existe pas."""
    out = _mount(monkeypatch, active_org=None)
    assert out["tenant"] is None


def test_notre_propre_tenant_le_dit(monkeypatch):
    out = _mount(monkeypatch, active_org=7, tenant_slug="oto")
    assert out["tenant"] == {"slug": "oto", "is_ours": True}


def test_org_hebergee_par_un_partenaire_nomme_le_partenaire(monkeypatch):
    """Le cas que #775 signalait sans réponse servie : un membre d'une org
    hébergée doit pouvoir lire QUEL tenant l'héberge, pas seulement `'tenant'`
    comme valeur d'énuméré ailleurs dans le contrat."""
    out = _mount(monkeypatch, active_org=42, tenant_slug="acme")
    assert out["tenant"] == {"slug": "acme", "is_ours": False}


def test_lookup_qui_echoue_ne_casse_pas_whoami(monkeypatch):
    """Best-effort, comme le reste de `oto_whoami` (org/group/project) : un
    hoquet DB sur le tenant ne doit pas priver l'agent de tout le reste."""
    from oto_mcp import access
    from oto_mcp.db import tenants as db_tenants

    def _boom(org_id):
        raise RuntimeError("DB down")

    monkeypatch.setattr(db_tenants, "org_tenant_slug", _boom)
    out = _mount(monkeypatch, active_org=7)
    assert out["tenant"] is None
    assert out["account"]["sub"] == "u"
