"""Capacités de lecture du domaine orgs (ADR 0009, barreau 2d).

Pas de divergence d'autz — mais des formes de réponse divergentes (MCP éclaté
vs REST agrégé). On unifie en **superset** : le handler renvoie toutes les clés
que chaque face consommait → ni le dashboard ni les agents MCP ne cassent.

Surfaces asymétriques préservées : `org.get` est REST-only (le MCP n'avait pas
d'agrégat, mais des tools list séparés, conservés MCP-only). `org.get` (membre)
et `org.admin.get` (platform) partagent le handler, diffèrent par autz+path.
"""
from __future__ import annotations

from pydantic import BaseModel

from .. import db, org_store
from ._authz import ORG_MEMBER_OF, PLATFORM_ADMIN, SUB_ONLY
from ._types import Capability, ResolvedCtx, RestBinding

from .registry import CAPABILITIES

_ID = {"id": "org_id"}


class NoInput(BaseModel):
    pass


class OrgIdInput(BaseModel):
    org_id: int


def _members(org_id: int) -> list[dict]:
    out = []
    for m in org_store.list_org_members(org_id):
        u = db.get_user(m["sub"]) or {}
        out.append({"sub": m["sub"], "email": u.get("email"), "name": u.get("name"),
                    "avatar_url": u.get("avatar_url"),
                    "role": m["org_role"], "active": m["is_active"]})
    return out


def _list_my_orgs(ctx: ResolvedCtx, inp: NoInput) -> dict:
    orgs, active = [], None
    for o in org_store.list_orgs_for_user(ctx.sub):
        if o["is_active"]:
            active = o["org_id"]
        orgs.append({  # superset REST(id/member_count/my_role) + MCP(org_id/role/active)
            "id": o["org_id"], "org_id": o["org_id"], "name": o["name"],
            "logo_url": o.get("logo_url"),
            "member_count": len(org_store.list_org_members(o["org_id"])),
            "my_role": o["org_role"], "role": o["org_role"], "active": o["is_active"],
        })
    return {"orgs": orgs, "active_org": active}


def _list_all_orgs(ctx: ResolvedCtx, inp: NoInput) -> dict:
    return {"orgs": [
        {**o, "member_count": len(org_store.list_org_members(o["id"]))}
        for o in org_store.list_all_orgs()
    ]}


def _org_detail(ctx: ResolvedCtx, inp: OrgIdInput) -> dict:
    org = org_store.get_org(inp.org_id)
    if not org:
        from ._types import AuthzDenied
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    my_role = org_store.get_org_role(inp.org_id, ctx.sub)
    brief = {"id": org["id"], "name": org["name"], "logo_url": org.get("logo_url"),
             "description": org.get("description") or "",
             "member_count": len(org_store.list_org_members(org["id"]))}
    if my_role is not None:
        brief["my_role"] = my_role
    return {
        "org": brief,
        "members": _members(inp.org_id),
        "secrets": org_store.list_org_secrets(inp.org_id),
        "entitlements": [{"namespace": e["namespace"], "granted_at": e["granted_at"]}
                         for e in org_store.list_org_entitlements(inp.org_id)],
        # Options payantes offertes (comp admin) au niveau ORG (couche abonnement).
        "option_comps": db.list_option_comps("org", str(inp.org_id)),
        # Clés plateforme partagées à l'org (couche 2, grant org-level).
        "platform_grants": db.list_org_grants(inp.org_id),
    }


CAPABILITIES += [
    Capability(key="org.list", handler=_list_my_orgs, Input=NoInput, authz=SUB_ONLY,
               description="List the organizations you belong to and which one is active.",
               mcp="oto_list_orgs", rest=RestBinding("GET", "/api/me/orgs")),
    # MCP fusionné dans oto_admin_org(op=list). REST conservé (dashboard).
    Capability(key="org.admin.list", handler=_list_all_orgs, Input=NoInput, authz=PLATFORM_ADMIN,
               description="[platform admin] List all organizations.",
               rest=RestBinding("GET", "/api/admin/orgs")),
    # org.get : REST-only, deux faces (membre vs platform), handler partagé.
    Capability(key="org.get", handler=_org_detail, Input=OrgIdInput, authz=ORG_MEMBER_OF("org_id"),
               rest=RestBinding("GET", "/api/orgs/{id}", _ID)),
    Capability(key="org.admin.get", handler=_org_detail, Input=OrgIdInput, authz=PLATFORM_ADMIN,
               rest=RestBinding("GET", "/api/admin/orgs/{id}", _ID)),
    # org.member.list (MCP-only) fusionné dans oto_admin_org_member(op=list).
    # org.secret.list (MCP-only) retiré du MCP (2026-06-25) : le dashboard lit les
    # secrets via la fiche org (org.admin.get → _org_detail). Pose = dashboard-only.
    # org.entitlement.list (MCP-only) fusionné dans oto_admin_namespace_access(op=list, scope=org).
    # Le dashboard lit les entitlements via la fiche org (org.admin.get → _org_detail).
]
