"""Couche capacité (ADR 0009) : descripteurs co-déclarés + adaptateurs MCP/REST.

Importer les modules de domaine ICI peuple `registry.CAPABILITIES` à l'import
du package — avant que `server.py` / `api_routes.py` ne bouclent dessus.
"""
from . import _mcp_adapter, _rest_adapter, registry
from . import orgs  # noqa: F401 — peuple registry.CAPABILITIES (org.use_org)
from . import orgs_members  # noqa: F401 — org.member.{add,set_role,remove}

__all__ = ["registry", "_mcp_adapter", "_rest_adapter"]
