"""Catalogue des capacités (ADR 0030, object-browser admin).

Expose le registre `CAPABILITIES` comme données : clé, nom MCP, bindings REST,
description et **JSON Schema** de l'Input (dérivé de pydantic v2 `model_json_schema()`).
L'UI admin (navigateur d'objets, niveau PLATEFORME) se peint à partir de ce
catalogue — *derive don't duplicate* : pas d'écran codé par capacité.

REST-only (`mcp=None`), gate `PLATFORM_ADMIN`. Le catalogue se liste lui-même (sans
conséquence). N'expose aucune valeur de secret — uniquement la forme des opérations.
"""
from __future__ import annotations

from pydantic import BaseModel

from ._authz import PLATFORM_ADMIN
from ._types import Capability, ResolvedCtx, RestBinding
from . import registry
from .registry import CAPABILITIES


class _CatalogInput(BaseModel):
    pass


def _authz_label(rule) -> str:
    """Libellé best-effort de la règle d'autz (fonction nommée ou closure)."""
    name = getattr(rule, "__name__", None)
    return name if name and name != "rule" else "scoped"


def _binding_view(b: RestBinding) -> dict:
    return {"method": b.verb, "path": b.path}


def _catalog(ctx: ResolvedCtx, inp: _CatalogInput) -> dict:
    out = []
    for cap in registry.CAPABILITIES:
        try:
            schema = cap.Input.model_json_schema()
        except Exception:
            schema = {}
        out.append({
            "key": cap.key,
            "mcp": cap.mcp,
            "rest": [_binding_view(b) for b in cap.rest_bindings()],
            "description": cap.description,
            "authz": _authz_label(cap.authz),
            "input_schema": schema,
        })
    out.sort(key=lambda c: c["key"])
    return {"capabilities": out}


CAPABILITIES += [
    Capability(
        key="admin.capabilities",
        handler=_catalog,
        Input=_CatalogInput,
        authz=PLATFORM_ADMIN,
        description="List the platform capability registry (keys, surfaces, input JSON schemas).",
        rest=RestBinding("GET", "/api/admin/capabilities"),
    ),
]
