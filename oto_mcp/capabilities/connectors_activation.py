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

from .. import access, connector_activation, db, group_store, org_store, providers
from ._authz import GROUP_ADMIN_OF, GROUP_MEMBER_OF, ORG_ADMIN_OF, ORG_MEMBER_OF
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_ID = {"id": "org_id"}      # placeholder {id} → champ Input org_id
_GID = {"id": "group_id"}   # placeholder {id} → champ Input group_id

# Couche 3 (option de connecteur, ADR 0024) : connecteur → option débloquable.
# Aujourd'hui seul unipile (option « messagerie hébergée »). Map curée — pas de
# champ générique au registre tant qu'il n'y a qu'une option.
# Option payante par connecteur → home canonique dans access.paid_option_for (derive don't duplicate).


def _org_subscribed(org_id: int, option: str) -> bool:
    """L'org a-t-elle l'option `option` débloquée (comp admin) ? Best-effort (ne fait
    jamais échouer la lecture de la liste)."""
    try:
        return db.has_option_comp("org", str(org_id), option)
    except Exception:
        return False


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
        # Invariant : ne lister que ce que la plateforme rend DISPONIBLE à cette org —
        # cohérent avec la surface USER (_visible_catalog). On filtre sur le CAP
        # plateforme (master), pas sur `effective` (sinon un connecteur que l'org a
        # override OFF disparaîtrait → impossible à réactiver). master OFF + pas
        # d'override = jamais activé → invisible (plus de levier inerte).
        if not master and org_ov is None:
            continue
        effective = org_ov if org_ov is not None else bool(master)
        option = access.paid_option_for(name)          # add-on payant (couche 3) ou None
        out.append({
            "connector": name, "label": c.label, "help": c.help,
            "namespaces": list(c.namespaces),
            "master_enabled": master, "org_enabled": org_ov, "effective": effective,
            "recommended": name in recommended,
            "paid_option": option,
            "subscribed": _org_subscribed(inp.org_id, option) if option else False,
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


# ── tier ÉQUIPE (ADR 0012, restrict-only) ────────────────────────────────────
# Un chef d'équipe (`GROUP_ADMIN_OF`) peut COUPER un connecteur pour SON équipe —
# jamais l'exposer au-delà de ce que l'org autorise (invariant MONOTONE). L'équipe
# n'a donc qu'un levier « couper / ré-ouvrir » ; le plancher reste org > plateforme.

class GroupActivationListInput(BaseModel):
    group_id: int


class GroupActivationSetInput(BaseModel):
    group_id: int
    name: str
    enabled: bool


class GroupActivationClearInput(BaseModel):
    group_id: int
    name: str


def _group_org_id(group_id: int) -> int:
    g = group_store.get_group(group_id)
    if not g:
        raise AuthzDenied(404, "unknown_group", f"Équipe #{group_id} inconnue.")
    return g["org_id"]


def _group_list(ctx: ResolvedCtx, inp: GroupActivationListInput) -> dict:
    """Pour chaque connecteur exposé à l'org de l'équipe : l'état effectif pour
    l'équipe (org expose ET pas coupé) + si l'équipe l'a coupé. On ne liste que ce
    que l'org rend disponible (plus une coupure éventuelle résiduelle) — pas de
    levier inerte, cohérent avec la surface org."""
    org_id = _group_org_id(inp.group_id)
    exposed = connector_activation.exposed_connectors(org_id)
    cut = connector_activation.group_cut_connectors(inp.group_id)
    out = []
    for name, c in providers.REGISTRY.items():
        org_available = name in exposed
        group_cut = name in cut
        if not org_available and not group_cut:
            continue
        out.append({
            "connector": name, "label": c.label, "help": c.help,
            "namespaces": list(c.namespaces),
            "org_available": org_available,
            "group_cut": group_cut,
            "effective": org_available and not group_cut,
        })
    return {"group_id": inp.group_id, "connectors": out}


def _require_org_available(group_id: int, name: str) -> None:
    """L'org doit exposer le connecteur pour qu'une équipe puisse le couper (sinon
    il est déjà off — rien à restreindre). Miroir de `_require_master_exposed`."""
    org_id = _group_org_id(group_id)
    if name not in connector_activation.exposed_connectors(org_id):
        raise AuthzDenied(409, "org_disabled",
                          f"Connecteur `{name}` non disponible dans l'org — rien à couper pour l'équipe.")


def _group_set(ctx: ResolvedCtx, inp: GroupActivationSetInput) -> dict:
    if inp.name not in providers.REGISTRY:
        raise AuthzDenied(404, "unknown_connector", f"Connecteur `{inp.name}` inconnu.")
    # Invariant MONOTONE : une équipe ne peut que RESTREINDRE. `enabled=True` (exposer
    # au-delà de l'org) est refusé — pour ré-ouvrir, on RETIRE la coupure (clear).
    if inp.enabled:
        raise AuthzDenied(409, "group_cannot_expose",
                          "Une équipe ne peut que restreindre (couper) un connecteur, jamais "
                          "l'exposer au-delà de l'org. Pour le ré-ouvrir, retire la coupure.")
    _require_org_available(inp.group_id, inp.name)
    connector_activation.set_group_activation(inp.group_id, inp.name, False, set_by=ctx.sub)
    return {"group_id": inp.group_id, "connector": inp.name, "enabled": False}


def _group_clear(ctx: ResolvedCtx, inp: GroupActivationClearInput) -> dict:
    """Retire la coupure d'équipe → le connecteur retombe sur l'exposition de l'org."""
    if inp.name not in providers.REGISTRY:
        raise AuthzDenied(404, "unknown_connector", f"Connecteur `{inp.name}` inconnu.")
    connector_activation.clear_group_activation(inp.group_id, inp.name)
    return {"group_id": inp.group_id, "connector": inp.name, "cleared": True}


CAPABILITIES += [
    Capability(
        key="connectors.activation.group_list", handler=_group_list, Input=GroupActivationListInput,
        authz=GROUP_MEMBER_OF("group_id"),
        description="List, for a team, each connector available to its org: whether the org "
                    "exposes it, whether the team has cut it, and the effective state for the "
                    "team's members. The team cockpit of connector availability.",
        mcp="oto_group_connector_activation",
        rest=RestBinding("GET", "/api/groups/{id}/connectors/activation", _GID),
    ),
    Capability(
        key="connectors.activation.set_group", handler=_group_set, Input=GroupActivationSetInput,
        authz=GROUP_ADMIN_OF("group_id"), refresh_visibility=True,
        description="[team lead] Cut a connector for your whole team (restrict-only — a team can "
                    "only narrow what the org allows, never expose beyond it). Requires the org to "
                    "expose it. Takes effect for members whose active team is this one, next session.",
        mcp="oto_set_group_connector_activation",
        rest=RestBinding("PUT", "/api/groups/{id}/connectors/{name}/activation", _GID),
    ),
    Capability(
        key="connectors.activation.clear_group", handler=_group_clear, Input=GroupActivationClearInput,
        authz=GROUP_ADMIN_OF("group_id"), refresh_visibility=True,
        description="[team lead] Remove your team's cut for a connector — it falls back to the "
                    "org's availability.",
        mcp="oto_clear_group_connector_activation",
        rest=RestBinding("DELETE", "/api/groups/{id}/connectors/{name}/activation", _GID),
    ),
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
