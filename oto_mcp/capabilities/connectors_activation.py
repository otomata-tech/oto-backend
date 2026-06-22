"""Capacités « activation de connecteur au niveau org » — plafond DUR d'org (ADR 0022).

ADR 0019 distingue exposition (plafond plateforme), proposition (recommandation) et
sélection (membre). Ce module ouvre le **troisième cran de gouvernance à l'org_admin** :
l'**override d'activation per-org** (`connector_activation`, déjà en DB). L'org_admin peut,
pour SA propre org, forcer un connecteur OFF (le retirer à tous ses membres) ou ON.

**Garde-fou (ADR 0022 §4)** : un override d'org ne peut pas *exposer* ce que la plateforme
a coupé — `enabled=True` n'est accepté que si le master global expose le connecteur. Le
deny-by-default plateforme n'est jamais relâché par une org ; l'org peut toujours restreindre.

Lecture = `ORG_MEMBER_OF` (les membres voient la gouvernance), écriture = `ORG_ADMIN_OF`.
`refresh_visibility=True` : la bascule re-pousse la visibilité sur la session MCP du caller.
Effet pour les autres membres : à leur session suivante (gate à la visibilité, pas au boot).
"""
from __future__ import annotations

from pydantic import BaseModel

from .. import connector_activation, org_store, providers
from ._authz import ORG_ADMIN_OF, ORG_MEMBER_OF
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_ID = {"id": "org_id"}     # placeholder {id} → champ Input org_id


class OrgActivationListInput(BaseModel):
    org_id: int


class OrgActivationSetInput(BaseModel):
    org_id: int
    name: str                  # connecteur (placeholder {name}, auto-mappé)
    enabled: bool


class OrgActivationClearInput(BaseModel):
    org_id: int
    name: str


def _org_list(ctx: ResolvedCtx, inp: OrgActivationListInput) -> dict:
    """Pour chaque connecteur du registre : master global, override de CETTE org,
    et l'état effectif (override > master > OFF). Plus `recommended` (baseline org)."""
    if not org_store.get_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    glob: dict[str, bool] = {}
    override: dict[str, bool] = {}
    for r in connector_activation.list_activations():
        if r["org_id"] is None:
            glob[r["connector"]] = bool(r["enabled"])
        elif r["org_id"] == inp.org_id:
            override[r["connector"]] = bool(r["enabled"])
    recommended = set(org_store.get_org_default_connectors(inp.org_id) or [])
    out = []
    for name, c in providers.REGISTRY.items():
        master = glob.get(name)          # None = jamais posé = OFF
        org_ov = override.get(name)      # None = pas d'override
        effective = org_ov if org_ov is not None else bool(master)
        out.append({
            "connector": name, "label": c.label, "help": c.help,
            "namespaces": list(c.namespaces),
            "master_enabled": master, "org_enabled": org_ov, "effective": effective,
            "recommended": name in recommended,
        })
    return {"org_id": inp.org_id, "connectors": out}


def _require_master_exposed(name: str) -> None:
    """Le master global doit exposer le connecteur pour qu'une org puisse l'activer."""
    if not connector_activation.is_exposed(name, org_id=None):
        raise AuthzDenied(409, "platform_disabled",
                          f"Connecteur `{name}` désactivé par la plateforme — ton org ne "
                          f"peut pas l'activer (le plafond plateforme n'est jamais relâché).")


def _org_set(ctx: ResolvedCtx, inp: OrgActivationSetInput) -> dict:
    if inp.name not in providers.REGISTRY:
        raise AuthzDenied(404, "unknown_connector", f"Connecteur `{inp.name}` inconnu.")
    if inp.enabled:
        _require_master_exposed(inp.name)
    connector_activation.set_activation(inp.name, inp.enabled, org_id=inp.org_id, set_by=ctx.sub)
    return {"org_id": inp.org_id, "connector": inp.name, "enabled": inp.enabled}


def _org_clear(ctx: ResolvedCtx, inp: OrgActivationClearInput) -> dict:
    """Supprime l'override d'org → le connecteur retombe sur le master global."""
    if inp.name not in providers.REGISTRY:
        raise AuthzDenied(404, "unknown_connector", f"Connecteur `{inp.name}` inconnu.")
    connector_activation.clear_activation(inp.name, inp.org_id)
    return {"org_id": inp.org_id, "connector": inp.name, "cleared": True}


CAPABILITIES += [
    Capability(
        key="connectors.activation.org_list", handler=_org_list, Input=OrgActivationListInput,
        authz=ORG_MEMBER_OF("org_id"),
        description="List, for your org, each connector's activation: the platform master switch, "
                    "your org's override (if any), the effective state, and whether the org "
                    "recommends it. The org cockpit of connector governance.",
        mcp="oto_org_connector_activation",
        rest=RestBinding("GET", "/api/orgs/{id}/connectors/activation", _ID),
    ),
    Capability(
        key="connectors.activation.set_org", handler=_org_set, Input=OrgActivationSetInput,
        authz=ORG_ADMIN_OF("org_id"), refresh_visibility=True,
        description="[org admin] Force a connector ON or OFF for your whole org (hard ceiling). "
                    "Enabling requires the platform to expose it (the platform ceiling is never "
                    "lifted); disabling always works. Takes effect for members on their next session.",
        mcp="oto_set_org_connector_activation",
        rest=RestBinding("PUT", "/api/orgs/{id}/connectors/{name}/activation", _ID),
    ),
    Capability(
        key="connectors.activation.clear_org", handler=_org_clear, Input=OrgActivationClearInput,
        authz=ORG_ADMIN_OF("org_id"), refresh_visibility=True,
        description="[org admin] Clear your org's activation override for a connector — it falls "
                    "back to the platform master switch.",
        mcp="oto_clear_org_connector_activation",
        rest=RestBinding("DELETE", "/api/orgs/{id}/connectors/{name}/activation", _ID),
    ),
]
