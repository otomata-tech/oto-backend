"""Garde de l'axe `_org=` (`call_axes.ORG`, posé par CallContextMiddleware pour tout outil)
— aligné sur `oto_use_org`.

Régression prod 2026-07-04 : `oto_my_connectors(org=35)` renvoyait une erreur opaque
alors qu'`oto_use_org(35)` marchait — les deux gardes divergeaient (is_org_member vs
resolve_org_for_user) et l'exception du middleware outermost était invisible. Le garde
`org=` utilise DÉSORMAIS la même résolution que la bascule, et toute erreur devient un
McpError PROPRE.
"""
import pytest
from oto_mcp.mcp_errors import McpError
from oto_mcp import call_axes, org_store, session_org


@pytest.fixture(autouse=True)
def _sub(monkeypatch):
    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: "u")


@pytest.mark.asyncio
async def test_pin_org_poses_when_member(monkeypatch):
    monkeypatch.setattr(org_store, "resolve_org_for_user", lambda sub, org: int(org))
    undo = await call_axes.ORG.pin_for(35, "oto_trigger")
    try:
        assert session_org.current_call_org() == 35     # org de l'appel posée
    finally:
        for reset, tok in reversed(undo):
            reset(tok)
    assert session_org.current_call_org() is None


@pytest.mark.asyncio
async def test_pin_org_rejects_non_member_cleanly(monkeypatch):
    def _raise(sub, org):
        raise ValueError("Tu n'es membre d'aucune org #35.")
    monkeypatch.setattr(org_store, "resolve_org_for_user", _raise)
    with pytest.raises(McpError) as ei:
        await call_axes.ORG.pin_for(35, "oto_trigger")
    assert "membre" in str(ei.value)                     # message actionnable
    assert session_org.current_call_org() is None


@pytest.mark.asyncio
async def test_pin_org_db_error_becomes_clean_mcperror(monkeypatch):
    def _boom(sub, org):
        raise RuntimeError("pool timeout")               # erreur DB, pas ValueError
    monkeypatch.setattr(org_store, "resolve_org_for_user", _boom)
    with pytest.raises(McpError) as ei:
        await call_axes.ORG.pin_for(35, "oto_trigger")
    assert "interne" in str(ei.value).lower()            # opaque → propre, jamais un 500 nu
    assert session_org.current_call_org() is None
