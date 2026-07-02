"""Régression #75 : le manifeste `referenced_tools` doit refléter le registre
BOOT, jamais la visibilité de la session courante.

`list_tools(run_middleware=False)` de fastmcp applique quand même
`apply_session_transforms` → l'appeler depuis un handler de session filtrait le
registre (faux `status=missing` sur un outil masqué par la session). Le fix
réchauffe un cache au démarrage (hors session) que `build_registry` sert ensuite.
"""
import asyncio

import pytest

from oto_mcp import tool_registry


class _Tool:
    def __init__(self, name):
        self.name = name
        self.description = f"desc {name}"


class _FakeMCP:
    """Instance MCP factice dont `list_tools` peut « rétrécir » pour simuler la
    contamination par la visibilité de session."""
    def __init__(self, names):
        self._names = list(names)

    def shrink_to(self, names):
        self._names = list(names)

    async def list_tools(self, *, run_middleware=True):
        return [_Tool(n) for n in self._names]


@pytest.fixture(autouse=True)
def _reset_cache():
    saved = tool_registry._REGISTRY
    tool_registry._REGISTRY = None
    yield
    tool_registry._REGISTRY = saved


def test_build_registry_serves_boot_cache_despite_session_filtering():
    async def scenario():
        full = ["bridge_call", "bridge_describe", "foo_bar"]
        mcp = _FakeMCP(full)

        # Démarrage : réchauffe le cache hors session (registre complet).
        warmed = await tool_registry.warm_registry(mcp)
        assert set(warmed) == set(full)

        # En session, la visibilité masque bridge_* → list_tools rétrécit.
        mcp.shrink_to(["foo_bar"])

        # build_registry doit TOUJOURS servir le registre boot (cache), pas la vue filtrée.
        reg = await tool_registry.build_registry(mcp)
        assert set(reg) == set(full), "le cache boot doit ignorer la visibilité de session"

        # Le manifeste d'une doctrine citant bridge_* les résout donc `status=ok`.
        manifest = await tool_registry.manifest_for(
            "<tool:bridge_call> <tool:bridge_describe>")
        assert [t["status"] for t in manifest] == ["ok", "ok"]

    asyncio.run(scenario())


def test_build_registry_live_fallback_when_not_warmed():
    """Sans réchauffage (tests / boot incomplet) : repli sur construction live."""
    async def scenario():
        mcp = _FakeMCP(["foo_bar"])
        reg = await tool_registry.build_registry(mcp)
        assert set(reg) == {"foo_bar"}

    asyncio.run(scenario())
