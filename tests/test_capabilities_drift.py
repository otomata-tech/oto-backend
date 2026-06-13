"""Tests anti-drift de la couche capacité (ADR 0009) : parité + unicité.

Garantissent que ce qui est déclaré au registre est réellement monté, et qu'un
nom de tool n'est pas enregistré deux fois (legacy + capacité).
"""
import asyncio

import pytest
from fastmcp import FastMCP

from oto_mcp.capabilities import _mcp_adapter, _rest_adapter, registry


def test_mcp_caps_are_mounted():
    m = FastMCP("t")
    _mcp_adapter.register(m, registry.CAPABILITIES)

    async def go():
        for cap in registry.caps_with_mcp():
            assert await m.get_tool(cap.mcp) is not None, cap.key

    asyncio.run(go())


def test_rest_caps_are_mounted():
    routes = _rest_adapter.make_routes(None, None, None, None, None, registry.CAPABILITIES)
    paths = {r.path for r in routes}
    for cap in registry.caps_with_rest():
        for b in cap.rest_bindings():
            assert b.path in paths, cap.key


def test_mcp_names_unique_within_registry():
    names = [c.mcp for c in registry.caps_with_mcp()]
    assert len(names) == len(set(names))


def test_rest_paths_unique_within_registry():
    keys = [(b.verb, b.path) for c in registry.caps_with_rest() for b in c.rest_bindings()]
    assert len(keys) == len(set(keys))


def test_no_mcp_name_collision_with_legacy():
    """Aucun nom de capacité MCP n'est aussi enregistré par un register() legacy.
    Skip si les deps optionnelles manquent en local (CI les a)."""
    from oto_mcp.tools import register_all
    m = FastMCP("t")
    try:
        register_all(m)
    except Exception as e:  # france_opendata & co. absents du venv local
        pytest.skip(f"register_all indisponible: {e}")
    _mcp_adapter.register(m, registry.CAPABILITIES)  # lèverait si doublon

    async def go():
        for cap in registry.caps_with_mcp():
            assert await m.get_tool(cap.mcp) is not None, cap.key

    asyncio.run(go())
