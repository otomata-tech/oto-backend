"""Catalogue de test construit explicitement, sans démarrage ni fédération."""
from functools import lru_cache


@lru_cache(maxsize=1)
def static_mcp():
    from oto_mcp.server import _build_mcp
    return _build_mcp("noauth")
