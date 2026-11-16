"""The connectors a tenant OFFERS — its activation ceiling, on ITS admin surface
(2026-09-26).

A partner serving oto under its brand does not offer the whole catalog: a Google
service its Google Cloud project does not declare, a connector it does not want to
support. Until now it only had two levers, both wrong: the platform
master (which cuts for EVERYONE, oto included) and the org override (one row
per org — sixty orgs, sixty actions — and which an org admin can undo).

The TENANT notch of `connector_availability` (`connectors/activation.py`) is a
CEILING: `enabled=false` cuts for all the tenant's orgs, and nothing below it —
org override, team — reopens. `enabled=true` only removes the cut: the
platform ceiling stays its own, a tenant never exposes what the platform does not
give (same rule as the org, `_require_master_exposed`).

Three capabilities, one per gesture — list, cut/reopen, remove the row — on
`/api/admin/tenants/{slug}/connectors/activation[/{name}]`, at the floor of tenant keys and
apps (`TENANT_ADMIN_OF(slug)`: the tenant's admin OR the operator). The primary
tenant is refused: its ceiling IS the platform master.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from .. import providers, tenancy
from ..connectors import activation as connector_activation
from ._authz import PLATFORM_ADMIN, SUPER_ADMIN, TENANT_ADMIN_OF
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES
from .tenant_keys import _known


class TenantConnectorsInput(BaseModel):
    slug: str


class TenantConnectorSetInput(BaseModel):
    slug: str
    name: str                  # connector ({name} placeholder, auto-mapped)
    enabled: bool


class TenantConnectorClearInput(BaseModel):
    slug: str
    name: str


class TenantConnectorRow(BaseModel):
    """A connector as seen from the tenant: the platform ceiling, ITS row, the result
    for its orgs (before their own overrides)."""
    connector: str
    label: str
    help: Optional[str] = None
    # `None` = never set on the platform side, which counts as OFF.
    master_enabled: Optional[bool] = None
    # `None` = no tenant row: the connector follows the platform.
    tenant_enabled: Optional[bool] = None
    effective: bool                         # master AND (tenant row, if set)


class TenantConnectors(BaseModel):
    """The tenant's activation cockpit. FILTERED to the platform ceiling, like an
    org's: a connector the platform does not expose does not appear there (no inert
    lever) — unless it carries a tenant row, so that it stays removable."""
    slug: str
    connectors: list[TenantConnectorRow]


class TenantConnectorSet(BaseModel):
    """Echo of the setting. `enabled=false` applies to ALL the tenant's orgs, from
    their next session; `true` removes the cut without exposing anything more than
    the platform."""
    slug: str
    connector: str
    enabled: bool


class TenantConnectorCleared(BaseModel):
    """Row removal: the connector follows the platform again. `cleared`
    is ALWAYS `true` (idempotent) — it does not prove a row existed."""
    slug: str
    connector: str
    cleared: bool


def _tenant(slug: str) -> str:
    slug = _known(slug)
    if slug == tenancy.primary_slug():
        raise AuthzDenied(400, "primary_tenant_activation",
                          f"Tenant `{slug}` has no tenant ceiling: its own is "
                          "the platform master (/api/admin/connectors/activation).")
    return slug


def _connu(name: str) -> str:
    if name not in providers.REGISTRY:
        raise AuthzDenied(404, "unknown_connector", f"Unknown connector `{name}`." + providers.refus_renomme(name))
    return name


def _list(ctx: ResolvedCtx, inp: TenantConnectorsInput) -> dict:  # noqa: ARG001
    slug = _tenant(inp.slug)
    master = {r["connector"]: bool(r["enabled"])
              for r in connector_activation.list_activations() if r["org_id"] is None}
    tenant_map = connector_activation.list_tenant_activations(slug)
    out = []
    for name, c in providers.REGISTRY.items():
        m, t = master.get(name), tenant_map.get(name)
        if not m and t is None:
            continue
        out.append({"connector": name, "label": c.label, "help": c.help,
                    "master_enabled": m, "tenant_enabled": t,
                    "effective": bool(m) and (t is not False)})
    return {"slug": slug, "connectors": out}


def _set(ctx: ResolvedCtx, inp: TenantConnectorSetInput) -> dict:
    slug = _tenant(inp.slug)
    name = _connu(inp.name)
    if inp.enabled and not connector_activation.is_exposed(name, org_id=None):
        raise AuthzDenied(409, "platform_disabled",
                          f"Connector `{name}` is disabled by the platform — a tenant does not "
                          "expose it beyond that (the platform ceiling is never loosened).")
    connector_activation.set_tenant_activation(slug, name, inp.enabled, set_by=ctx.sub)
    return {"slug": slug, "connector": name, "enabled": inp.enabled}


def _clear(ctx: ResolvedCtx, inp: TenantConnectorClearInput) -> dict:  # noqa: ARG001
    slug = _tenant(inp.slug)
    name = _connu(inp.name)
    connector_activation.clear_tenant_activation(slug, name)
    return {"slug": slug, "connector": name, "cleared": True}


_PATH = "/api/admin/tenants/{slug}/connectors/activation"

CAPABILITIES += [
    Capability(
        key="admin.tenant_connectors", handler=_list, Input=TenantConnectorsInput,
        Output=TenantConnectors, authz=TENANT_ADMIN_OF("slug", platform=PLATFORM_ADMIN),
        description=("Connectors as this tenant offers them: platform master, the tenant's "
                     "own cut, and the result for its orgs (before their overrides)."),
        rest=RestBinding("GET", _PATH),
    ),
    Capability(
        key="admin.tenant_connector_set", handler=_set, Input=TenantConnectorSetInput,
        Output=TenantConnectorSet, authz=TENANT_ADMIN_OF("slug", platform=SUPER_ADMIN),
        refresh_visibility=True,
        description=("Cut (`enabled=false`) or reopen a connector for EVERY org of the "
                     "tenant — a ceiling no org override reopens; `true` never exposes "
                     "beyond the platform master."),
        rest=RestBinding("PUT", _PATH + "/{name}"),
    ),
    Capability(
        key="admin.tenant_connector_clear", handler=_clear, Input=TenantConnectorClearInput,
        Output=TenantConnectorCleared, authz=TENANT_ADMIN_OF("slug", platform=SUPER_ADMIN),
        refresh_visibility=True,
        description="Remove the tenant's line for a connector (it follows the platform again).",
        rest=RestBinding("DELETE", _PATH + "/{name}"),
    ),
]
