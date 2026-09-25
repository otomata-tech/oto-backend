"""Les connecteurs qu'un tenant OFFRE — son plafond d'activation, sur SA surface admin
(2026-09-26).

Un partenaire qui sert oto sous sa marque n'offre pas tout le catalogue : un service
Google que son projet Google Cloud ne déclare pas, un connecteur qu'il ne veut pas
supporter. Jusqu'ici il n'avait que deux leviers, tous deux faux : le master
plateforme (qui coupe pour TOUT LE MONDE, oto compris) et l'override d'org (une ligne
par org — soixante orgs, soixante gestes — et qu'un admin d'org peut défaire).

Le cran TENANT de `connector_availability` (`connectors/activation.py`) est un
PLAFOND : `enabled=false` coupe pour toutes les orgs du tenant, et rien en dessous —
override d'org, équipe — ne rouvre. `enabled=true` ne fait que retirer la coupure : le
plafond plateforme reste le sien, un tenant n'expose jamais ce que la plateforme ne
donne pas (même règle que l'org, `_require_master_exposed`).

Trois capacités, une par geste — lister, couper/rouvrir, retirer la ligne — sur
`/api/admin/tenants/{slug}/connectors/activation[/{name}]`, au plancher des clés et des
apps de tenant (`TENANT_ADMIN_OF(slug)` : l'admin du tenant OU l'opérateur). Le tenant
primaire est refusé : son plafond EST le master plateforme.
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
    name: str                  # connecteur (placeholder {name}, auto-mappé)
    enabled: bool


class TenantConnectorClearInput(BaseModel):
    slug: str
    name: str


class TenantConnectorRow(BaseModel):
    """Un connecteur vu du tenant : le plafond plateforme, SA ligne, la résultante
    pour ses orgs (avant leurs propres overrides)."""
    connector: str
    label: str
    help: Optional[str] = None
    # `None` = jamais posé côté plateforme, ce qui vaut OFF.
    master_enabled: Optional[bool] = None
    # `None` = pas de ligne tenant : le connecteur suit la plateforme.
    tenant_enabled: Optional[bool] = None
    effective: bool                         # master ET (ligne tenant, si posée)


class TenantConnectors(BaseModel):
    """Le cockpit d'activation du tenant. FILTRÉ au plafond plateforme, comme celui
    d'une org : un connecteur que la plateforme n'expose pas n'y figure pas (pas de
    levier inerte) — sauf s'il porte une ligne tenant, pour qu'elle reste retirable."""
    slug: str
    connectors: list[TenantConnectorRow]


class TenantConnectorSet(BaseModel):
    """Écho de la pose. `enabled=false` vaut pour TOUTES les orgs du tenant, dès
    leur prochaine session ; `true` retire la coupure sans rien exposer de plus que
    la plateforme."""
    slug: str
    connector: str
    enabled: bool


class TenantConnectorCleared(BaseModel):
    """Retrait de la ligne : le connecteur suit à nouveau la plateforme. `cleared`
    vaut TOUJOURS `true` (idempotent) — il ne prouve pas qu'une ligne existait."""
    slug: str
    connector: str
    cleared: bool


def _tenant(slug: str) -> str:
    slug = _known(slug)
    if slug == tenancy.PRIMARY_SLUG:
        raise AuthzDenied(400, "primary_tenant_activation",
                          f"Le tenant `{slug}` n'a pas de plafond de tenant : le sien est "
                          "le master plateforme (/api/admin/connectors/activation).")
    return slug


def _connu(name: str) -> str:
    if name not in providers.REGISTRY:
        raise AuthzDenied(404, "unknown_connector", f"Connecteur `{name}` inconnu.")
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
                          f"Connecteur `{name}` désactivé par la plateforme — un tenant ne "
                          "l'expose pas au-delà (le plafond plateforme n'est jamais relâché).")
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
