"""Axe d'appel `group=` (ADR 0038 B3) — pin gardé, org parente co-posée.

Miroir de test_call_context_org_axis pour l'axe équipe : la pose passe la garde
`can_read_group` (même garde que la résolution), co-pose l'org PARENTE du groupe
(invariant « groupe ⊂ org » par construction), et le reset LIFO nettoie les deux.
"""
import asyncio

import pytest
from mcp.shared.exceptions import McpError

from oto_mcp import call_axes, group_store, roles, session_org


@pytest.fixture(autouse=True)
def _sub(monkeypatch):
    # `call_axes` importe la fonction PAR NOM → patcher sa liaison locale.
    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: "u")
    yield


def _unpin(undo):
    for reset, tok in reversed(undo):
        reset(tok)


def test_pin_group_poses_group_and_parent_org(monkeypatch):
    monkeypatch.setattr(group_store, "get_group", lambda gid: {"id": gid, "org_id": 42})
    monkeypatch.setattr(roles, "can_read_group", lambda sub, gid: True)

    async def _scenario():
        # pin + lecture + unpin dans le MÊME contexte (comme le middleware en prod —
        # un token contextvar ne se reset pas depuis un autre contexte).
        undo = await call_axes._pin_group(5)
        try:
            assert session_org.current_call_group() == 5
            assert session_org.current_call_org() == 42  # org parente co-posée (invariant)
        finally:
            _unpin(undo)
        assert session_org.current_call_group() is None  # reset propre des DEUX axes
        assert session_org.current_call_org() is None

    asyncio.run(_scenario())


def test_pin_group_refused_when_not_reader(monkeypatch):
    monkeypatch.setattr(group_store, "get_group", lambda gid: {"id": gid, "org_id": 42})
    monkeypatch.setattr(roles, "can_read_group", lambda sub, gid: False)
    with pytest.raises(McpError):
        asyncio.run(call_axes._pin_group(5))
    assert session_org.current_call_group() is None   # rien posé sur refus
    assert session_org.current_call_org() is None


def test_pin_group_unknown_group(monkeypatch):
    monkeypatch.setattr(group_store, "get_group", lambda gid: None)
    with pytest.raises(McpError):
        asyncio.run(call_axes._pin_group(5))
    assert session_org.current_call_group() is None


def test_pin_group_none_is_inert():
    assert asyncio.run(call_axes._pin_group(None)) == []
