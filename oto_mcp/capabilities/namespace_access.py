"""Accès aux namespaces gouvernés — capacité consolidée (ADR 0009, fusion `*_op`).

Un connecteur **grant-only** (non exposé par défaut, ex. un bridge remote `mm`,
`gocardless`) s'ouvre soit à une **org** entière (entitlement), soit à un **user**
nommé (grant per-user). Historiquement 6 outils MCP séparés :
`oto_admin_{grant,revoke}_org_entitlement`, `oto_admin_list_org_entitlements`,
`oto_admin_{grant,revoke}_namespace`, `oto_admin_list_namespace_grants`.

On les réunit en UN outil `oto_admin_namespace_access(op, scope, …)` — même concept
« donner accès à un namespace gouverné », deux portées (`scope=org|user`). L'autz
reste **déclarée** via le combinateur op-aware `ADMIN_BY_OP` : grant/revoke =
`SUPER_ADMIN` (octroi d'accès = escalade), list = `PLATFORM_ADMIN` (supervision).

Les faces REST historiques ne bougent pas : `orgs_admin` (entitlement grant/revoke)
garde son `rest=`, on lui retire juste le `mcp=`. Le grant per-user côté REST reste
servi par `api_routes_orgs`. Cette capacité est donc **MCP-only**.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from .. import db, org_store
from ..tool_visibility import ADMIN_GRANT_ONLY_NAMESPACES
from ._authz import ADMIN_BY_OP, PLATFORM_ADMIN, SUPER_ADMIN
from ._types import AuthzDenied, Capability, ResolvedCtx
from .registry import CAPABILITIES


class NamespaceAccessInput(BaseModel):
    op: Literal["grant", "revoke", "list"]
    scope: Literal["user", "org"] = "user"
    # user : `sub` Logto ou email du destinataire ; org : l'id d'org (entier).
    # Pour op=list scope=user, `target` est ignoré (liste globale, filtre `namespace`).
    target: Optional[str] = None
    namespace: Optional[str] = None


def _require(val, code: str, msg: str):
    if val is None or (isinstance(val, str) and not val.strip()):
        raise AuthzDenied(400, code, msg)
    return val


def _check_controlled(namespace: str) -> None:
    if namespace not in ADMIN_GRANT_ONLY_NAMESPACES:
        raise AuthzDenied(400, "namespace_not_controlled",
                          f"`{namespace}` n'est pas un namespace gouverné "
                          f"(contrôlés : {sorted(ADMIN_GRANT_ONLY_NAMESPACES)}).")


def _resolve_sub(target: str) -> str:
    if "@" in target:
        u = db.get_user_by_email(target)
        if not u:
            raise AuthzDenied(404, "unknown_user", f"Aucun user connu avec l'email `{target}`.")
        return u["sub"]
    return target


def _resolve_org_id(target: str) -> int:
    try:
        org_id = int(target)
    except (TypeError, ValueError):
        raise AuthzDenied(400, "invalid_org",
                          "scope=org : `target` doit être un id d'org (entier).")
    if not org_store.get_org(org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{org_id} inconnue.")
    return org_id


def _namespace_access(ctx: ResolvedCtx, inp: NamespaceAccessInput) -> dict:
    op, scope = inp.op, inp.scope

    if op == "list":
        if scope == "org":
            org_id = _resolve_org_id(_require(
                inp.target, "missing_target", "scope=org : `target` (id d'org) requis pour lister."))
            return {"scope": "org", "org_id": org_id,
                    "entitlements": org_store.list_org_entitlements(org_id)}
        # scope=user : liste globale, filtre `namespace` optionnel.
        return {"scope": "user", "grants": db.list_namespace_grants(inp.namespace)}

    # grant / revoke — namespace requis + gouverné, target requis.
    namespace = _require(inp.namespace, "missing_namespace", "`namespace` requis.")
    _check_controlled(namespace)
    target = _require(inp.target, "missing_target", "`target` requis.")

    if scope == "org":
        org_id = _resolve_org_id(target)
        if op == "grant":
            org_store.grant_org_entitlement(org_id, namespace, granted_by=ctx.sub)
            return {"ok": True, "scope": "org", "org_id": org_id,
                    "namespace": namespace, "granted": True}
        existed = org_store.revoke_org_entitlement(org_id, namespace)
        return {"ok": True, "scope": "org", "org_id": org_id, "namespace": namespace,
                "revoked": existed, "existed": existed}

    # scope=user
    target_sub = _resolve_sub(target)
    if op == "grant":
        db.grant_namespace(target_sub, namespace, granted_by=ctx.sub)
        return {"ok": True, "scope": "user", "target": target_sub,
                "namespace": namespace, "granted": True}
    existed = db.revoke_namespace(target_sub, namespace)
    return {"ok": True, "scope": "user", "target": target_sub, "namespace": namespace,
            "revoked": existed, "existed": existed}


CAPABILITIES += [
    Capability(
        key="connectors.namespace_access",
        handler=_namespace_access,
        Input=NamespaceAccessInput,
        authz=ADMIN_BY_OP({"grant": SUPER_ADMIN, "revoke": SUPER_ADMIN, "list": PLATFORM_ADMIN}),
        description=(
            "Grant/revoke/list access to a CONTROLLED (grant-only) connector namespace "
            "(e.g. a remote bridge like `mm`, `gocardless`). `scope=org` entitles a whole org "
            "(`target` = org id); `scope=user` grants one named user (`target` = sub or email). "
            "op=list: scope=org lists that org's entitlements (`target` = org id); scope=user lists "
            "user grants (optional `namespace` filter). grant/revoke = super admin; list = platform admin."
        ),
        mcp="oto_admin_namespace_access",
    ),
]
