"""Schéma plat des tools MCP de capacité (ADR 0009 §6) — prérequis testé.

Sans `apply_flat_signature`, un param pydantic unique donnerait un schéma
imbriqué (`{"p": {"$ref": …}}`) cassant le contrat plat. On vérifie que les
tools de capacité exposent leurs champs `Input` à plat.
"""
import asyncio

from fastmcp import FastMCP

from oto_mcp.capabilities import _mcp_adapter, registry


def _schema(tool):
    for attr in ("parameters", "input_schema", "inputSchema"):
        s = getattr(tool, attr, None)
        if isinstance(s, dict):
            return s
    return None


def test_capability_tools_have_flat_schema():
    m = FastMCP("t")
    _mcp_adapter.register(m, registry.CAPABILITIES)

    async def go():
        for cap in registry.caps_with_mcp():
            tool = await m.get_tool(cap.mcp)
            s = _schema(tool)
            assert s is not None, cap.mcp
            props = set(s.get("properties", {}).keys())
            expected = set(cap.Input.model_fields.keys())
            # Axe-contexte `org=` (jeton d'appel, modèle sans état de session #108/#112)
            # injecté à plat sur les caps org-scopées qui ne déclarent pas déjà un `org`.
            if _mcp_adapter._org_param_reserved(cap):
                expected.add("org")
            assert props == expected, cap.mcp                            # plat
            assert "$defs" not in s, cap.mcp                              # pas imbriqué

    asyncio.run(go())
