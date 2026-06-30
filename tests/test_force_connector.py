"""Force-connecteur-par-user (ADR 0031) : un org_admin pousse un connecteur dans la
toolbox d'un membre → override positif (`user_enabled_tools`) sur les tools du
connecteur, scopé sur l'org. Visibilité only, re-masquable.
"""
import pytest

from oto_mcp.capabilities import connectors_force as cf


@pytest.mark.asyncio
async def test_force_sets_overrides_for_connector_tools(monkeypatch):
    calls = []

    async def fake_registry(mcp_instance=None):
        return {"serper_search": {}, "serper_news": {}, "fr_get": {}}

    monkeypatch.setattr(cf.tool_registry, "build_registry", fake_registry)
    monkeypatch.setattr(cf.org_store, "get_org_role", lambda org, sub: "member")
    monkeypatch.setattr(cf.db, "add_user_enabled_tool",
                        lambda sub, tool, org: calls.append((sub, tool, org)))

    inp = cf.ForceConnectorInput(org_id=5, connector="serper", member="christelle")
    res = await cf._force_connector(None, inp)

    assert res["tools_forced"] == 2  # serper_search + serper_news (pas fr_get)
    assert ("christelle", "serper_search", 5) in calls
    assert ("christelle", "serper_news", 5) in calls
    assert all(t != "fr_get" for _, t, _ in calls)


@pytest.mark.asyncio
async def test_force_unknown_connector_raises(monkeypatch):
    inp = cf.ForceConnectorInput(org_id=5, connector="nope", member="u")
    with pytest.raises(Exception):
        await cf._force_connector(None, inp)


@pytest.mark.asyncio
async def test_force_non_member_raises(monkeypatch):
    async def fake_registry(mcp_instance=None):
        return {"serper_search": {}}

    monkeypatch.setattr(cf.tool_registry, "build_registry", fake_registry)
    monkeypatch.setattr(cf.org_store, "get_org_role", lambda org, sub: None)  # pas membre
    inp = cf.ForceConnectorInput(org_id=5, connector="serper", member="stranger")
    with pytest.raises(Exception):  # user_not_in_org
        await cf._force_connector(None, inp)
