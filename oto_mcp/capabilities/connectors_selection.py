"""Capacités « sélection de connecteurs » — marketplace (ADR 0019).

Per-membre, scopé à l'org active (`SUB_ONLY` injecte `ctx.org_id`). Trois faits
distincts (cf. `connector_selection`) : exposition (`connector_activation`, plafond),
proposition (`orgs.default_connectors`), sélection (`user_selected_connectors`).

- `connectors.me` (lecture) = catalogue exposé pour l'org active, fusionné avec
  l'état per-membre (`not_selected` | `active` | `paused`) + `recommended` (baseline org).
  Source unique consommée par le dashboard (library + « mes connecteurs »).
- `connectors.select` / `.pause` / `.unselect` (mutation) = installe / met en pause /
  retire un connecteur. Garde : refuser un connecteur non-exposé pour l'org active
  (le plafond d'exposition `connector_activation` n'est jamais relâché).

Handlers SYNC (les adaptateurs n'awaitent pas). NB : la mutation n'a **pas** encore
d'effet de visibilité — le masquage de la pause est branché au middleware en B5
(derrière flag `OTO_CONNECTOR_SELECTION_ENABLED`).
"""
from __future__ import annotations

from pydantic import BaseModel

from .. import access, connector_activation, connector_selection, org_store, providers
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class NoInput(BaseModel):
    """Capacité sans paramètre (classe dédiée — ne jamais passer `BaseModel` nu en
    `Input`, l'adaptateur MCP injecte `__signature__` sur la classe)."""


class ConnectorActionInput(BaseModel):
    name: str                            # nom de connecteur (registre providers.py)


def _visible_catalog(ctx: ResolvedCtx) -> list[dict]:
    """Catalogue exposé pour l'org active du caller — miroir du filtrage de
    `api_routes.connectors_catalog` : activation (plafond) + grant-only entitlé.
    L'admin plateforme voit tout l'exposé."""
    exposed = connector_activation.exposed_connectors(ctx.org_id)
    is_admin = access.is_platform_operator(ctx.sub)
    granted = access.granted_namespaces_for(ctx.sub)
    out = []
    for c in providers.public_catalog():
        if c["name"] not in exposed:
            continue
        # grant-only (platform_granted) : visible seulement si entitlé (un namespace
        # granté) ou admin — jamais relâcher le deny-by-default.
        if c.get("availability") == "platform_granted" and not is_admin:
            if not (set(c.get("namespaces") or []) & granted):
                continue
        out.append(c)
    return out


def _me(ctx: ResolvedCtx, inp: NoInput) -> dict:
    org_id = ctx.org_id or 0
    selection = connector_selection.list_selection(ctx.sub, org_id)
    recommended = set(org_store.get_org_default_connectors(ctx.org_id) or []) if ctx.org_id else set()
    connectors = [
        {**c,
         "state": selection.get(c["name"], "not_selected"),
         "recommended": c["name"] in recommended}
        for c in _visible_catalog(ctx)
    ]
    return {"connectors": connectors}


def _require_exposed(ctx: ResolvedCtx, name: str) -> None:
    """Plafond : un connecteur non-exposé pour l'org active ne peut être ni
    sélectionné ni mis en pause (deny-by-default jamais relâché)."""
    if name not in connector_activation.exposed_connectors(ctx.org_id):
        raise AuthzDenied(404, "unknown_connector",
                          f"Connecteur `{name}` indisponible pour ton org active.")


def _select(ctx: ResolvedCtx, inp: ConnectorActionInput) -> dict:
    _require_exposed(ctx, inp.name)
    connector_selection.set_state(ctx.sub, inp.name, connector_selection.ACTIVE, ctx.org_id or 0)
    return {"connector": inp.name, "state": "active"}


def _pause(ctx: ResolvedCtx, inp: ConnectorActionInput) -> dict:
    _require_exposed(ctx, inp.name)
    connector_selection.set_state(ctx.sub, inp.name, connector_selection.PAUSED, ctx.org_id or 0)
    return {"connector": inp.name, "state": "paused"}


def _unselect(ctx: ResolvedCtx, inp: ConnectorActionInput) -> dict:
    removed = connector_selection.unselect(ctx.sub, inp.name, ctx.org_id or 0)
    return {"connector": inp.name, "state": "not_selected", "removed": removed}


CAPABILITIES += [
    Capability(
        key="connectors.me", handler=_me, Input=NoInput, authz=SUB_ONLY,
        description="List every connector available to you (the marketplace catalog) with your "
                    "per-workspace state: not_selected (in the library) / active / paused, plus "
                    "`recommended` when your org proposes it. Source for both the connector "
                    "library and your installed connectors.",
        mcp="oto_my_connectors", rest=RestBinding("GET", "/api/me/connectors"),
    ),
    Capability(
        key="connectors.select", handler=_select, Input=ConnectorActionInput, authz=SUB_ONLY,
        description="Install a connector into your active workspace (state=active). Its tools "
                    "become visible. name = connector name from the catalog.",
        mcp="oto_select_connector", rest=RestBinding("POST", "/api/me/connectors/{name}/select"),
    ),
    Capability(
        key="connectors.pause", handler=_pause, Input=ConnectorActionInput, authz=SUB_ONLY,
        description="Pause an installed connector (state=paused): kept installed but its tools "
                    "are hidden. Resume by selecting it again.",
        mcp="oto_pause_connector", rest=RestBinding("POST", "/api/me/connectors/{name}/pause"),
    ),
    Capability(
        key="connectors.unselect", handler=_unselect, Input=ConnectorActionInput, authz=SUB_ONLY,
        description="Remove a connector from your workspace (back to the library). Does not touch "
                    "credentials, only your selection.",
        mcp="oto_unselect_connector", rest=RestBinding("DELETE", "/api/me/connectors/{name}"),
    ),
]
