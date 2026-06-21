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


# Modules `tools/<m>.py` que la dérivation register_all doit charger (#24). Set
# FIGÉ : un provider kind="tools" ajouté sans module (ou avec une faute dans
# `modules`) casse ce test — il garde la dérivation honnête sans dépendre des
# deps optionnelles (pur registre, toujours exécuté).
_EXPECTED_TOOL_MODULES = {
    "serper", "hunter", "fr", "attio", "lemlist", "kaspr", "pennylane", "slack",
    "fullenrich", "folk", "silae", "gocardless", "crunchbase",
    "gmail", "tasks", "calendar", "sheets", "drive", "chat",
    "reddit", "culture", "sirene_stock",
    "foncier", "sante", "whatsapp", "unipile",
    "telegram", "instagram", "messenger", "twitter",
    "apollo", "phantombuster", "hithorizons", "topograph", "zerobounce",
    "hubspot", "zoho", "zohodesk", "notion", "supabase", "figma",
    "ashby", "greenhouse", "lever", "recruitee", "teamtailor", "serpapi",
    "n8n", "make", "zapier", "brightdata",
}


def test_tools_module_derivation_is_frozen():
    """register_all dérive le chargement du registre (`Connector.modules` ou le
    nom). Le set de modules à importer = exactement les modules connus."""
    from oto_mcp import providers
    mods: set[str] = set()
    for c in providers.REGISTRY.values():
        if c.kind == "tools":
            mods |= set(c.modules or (c.name,))
    assert mods == _EXPECTED_TOOL_MODULES


def test_tools_namespaces_are_matchable():
    """Un namespace de provider kind="tools" doit pouvoir être produit par
    `namespace_of(tool)` (= 1er token avant `_`) — sinon le gate d'activation
    fail-open en silence. Un namespace multi-mot (`culture_spectacle`) ne matche
    JAMAIS → bug (#24). Pur registre, dep-indépendant.

    Whitelist : `sirene_stock` (namespace_of→`sirene`) reste un fail-open connu,
    limite structurelle de namespace_of, TODO #24-bis."""
    from oto_mcp import providers
    from oto_mcp.tool_visibility import namespace_of

    WHITELIST = {"sirene_stock"}
    for c in providers.REGISTRY.values():
        if c.kind != "tools":
            continue
        for ns in c.namespaces:
            if ns in WHITELIST:
                continue
            assert namespace_of(f"{ns}_x") == ns, (
                f"namespace non matchable {c.name}:{ns} (multi-mot → fail-open du gate)")


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
