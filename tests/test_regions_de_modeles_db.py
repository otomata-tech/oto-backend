"""Where an org lets its agents' models run, IN THE DATABASE: the column's default (every
org allowed), the org setting that turns it off, and the catalogue that then hides the
models that run in China.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def org(live):
    from oto_mcp.db._conn import _connect
    with _connect() as c:
        return c.execute("INSERT INTO orgs (name) VALUES ('banc-regions') "
                         "RETURNING id").fetchone()["id"]


def _reglage(op, org_id, **champs):
    from oto_mcp.capabilities import org_console as oc
    from oto_mcp.capabilities._types import ResolvedCtx
    return oc._org_settings(ResolvedCtx(sub="admin", org_id=org_id, role="org_admin"),
                            oc.OrgSettingsInput(op=op, domain="model_regions",
                                                org_id=org_id, **champs))


def test_permise_par_defaut_puis_ecartee(org, monkeypatch):
    from oto_mcp.capabilities import _cle_exigee, _modele
    monkeypatch.setattr(_cle_exigee, "manquantes", lambda *a, **k: [])
    assert _reglage("get", org) == {"org_id": org, "allow_cn": True}
    assert _reglage("set", org, allow_cn=False) == {"org_id": org, "allow_cn": False}
    etat = _modele.etat_servi({"families": ["openrouter"]}, org)
    flash = next(m for m in etat["models"] if m["id"] == "deepseek/deepseek-v4.1-flash-1017")
    assert flash["allowed"] is False
    assert _modele.region_bloquee(org, "deepseek/deepseek-v4.1-flash-1017")
    assert not _modele.region_bloquee(org, "claude-sonnet-5")
    assert _reglage("set", org, allow_cn=True)["allow_cn"] is True


def test_la_route_rest_lit_et_pose(org):
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.orgs import model_regions as mr
    ctx = ResolvedCtx(sub="admin", org_id=org, role="org_admin")
    assert mr._get(ctx, mr.GetRegionsInput(org_id=org)) == {"org_id": org, "allow_cn": True}
    assert mr._set(ctx, mr.SetRegionsInput(org_id=org, allow_cn=False))["allow_cn"] is False
