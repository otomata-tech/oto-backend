"""Gouvernance générique des ressources possédées (ADR 0030).

Une capacité unique `oto_resource(op, …)` réunit lister / inspecter / transférer /
(dé)partager une ressource possédée, quel que soit son type. L'autz est **déclarée**
via `RESOURCE_GOVERN` : owner ∪ escalade `roles.py` (`ownership.can_govern`) pour les
ops ciblées ; `list` ouvert à tout authentifié, le handler FILTRE aux ressources
gouvernables. C'est le chemin qui ferme le trou « un super_admin ne peut pas
transférer un datastore » et qui alimente l'object-browser admin.

Plan GOUVERNANCE uniquement (transférer/lister/partager **sans lire** le contenu) —
la lecture du contenu d'une ressource perso reste l'exception auditée view-as
(ADR 0023). Pilote : `resource_type='datastore_namespace'`.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from .. import access, db, org_store, ownership, roles
from ._authz import RESOURCE_GOVERN
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class ResourceInput(BaseModel):
    op: Literal["list", "get", "transfer", "share", "unshare"]
    resource_type: str = "datastore_namespace"
    resource_id: Optional[str] = None
    new_owner_email: Optional[str] = None   # transfer
    email: Optional[str] = None             # share / unshare (principal user)
    permission: Literal["read", "write"] = "write"  # share


_SUPPORTED = ("datastore_namespace",)


def _check_type(resource_type: str) -> None:
    if resource_type not in _SUPPORTED:
        raise AuthzDenied(400, "unsupported_resource_type",
                          f"type `{resource_type}` non supporté (pilote : {list(_SUPPORTED)}).")


def _owner_label(owner_type: str, owner_id: str) -> Optional[str]:
    """Libellé lisible du propriétaire (email pour un user, nom pour une org)."""
    if owner_type == "user":
        u = db.get_user(owner_id)
        return u.get("email") if u else None
    if owner_type == "org":
        try:
            o = org_store.get_org(int(owner_id))
        except (TypeError, ValueError):
            return None
        return o.get("name") if o else None
    return None


def _enrich_datastore(row: dict) -> dict:
    ns_id = int(row["id"])
    return {
        "resource_type": "datastore_namespace",
        "resource_id": str(ns_id),
        "namespace": row["namespace"],
        "owner_type": row.get("owner_type"),
        "owner_id": row.get("owner_id"),
        "owner_label": _owner_label(row.get("owner_type"), row.get("owner_id")),
        "row_count": db.count_datastore_rows_for_ns(ns_id),
        "created_at": row.get("created_at"),
    }


def _grants_view(resource_type: str, resource_id: str) -> list[dict]:
    return [
        {"principal_type": g.get("principal_type"), "principal_id": g.get("principal_id"),
         "email": g.get("email"), "permission": g.get("permission"),
         "granted_at": g.get("granted_at")}
        for g in ownership.list_grants(resource_type, resource_id)
    ]


def _resolve_recipient(email: Optional[str]) -> dict:
    email = (email or "").strip()
    if not email:
        raise AuthzDenied(400, "email_required", "`email` requis.")
    u = db.get_user_by_email(email)
    if not u:
        raise AuthzDenied(404, "unknown_user", f"aucun utilisateur oto avec l'email {email}")
    return u


def _resources(ctx: ResolvedCtx, inp: ResourceInput) -> dict:
    _check_type(inp.resource_type)

    if inp.op == "list":
        # PLATEFORME → tout ; sinon → ce que l'acteur gouverne (perso + orgs/groupes
        # qu'il administre). Plan gouvernance : métadonnées seulement, pas de contenu.
        if access.is_platform_operator(ctx.sub):
            rows = db.list_all_datastore_namespaces()
        else:
            scope = ownership.accessor_scope(ctx.sub)
            governed = [("user", ctx.sub)]
            governed += [("org", str(o)) for o in scope.org_ids if roles.is_org_admin(ctx.sub, o)]
            rows = db.list_datastore_namespaces_for_owners(governed)
        return {"resource_type": inp.resource_type,
                "resources": [_enrich_datastore(r) for r in rows]}

    if inp.resource_id is None:
        raise AuthzDenied(400, "missing_resource_id", "`resource_id` requis.")
    rid = str(inp.resource_id)

    if inp.op == "get":
        row = db.get_datastore_namespace_by_id(int(rid))
        if not row:
            raise AuthzDenied(404, "not_found", "ressource introuvable.")
        out = _enrich_datastore(row)
        out["grants"] = _grants_view(inp.resource_type, rid)
        return out

    if inp.op == "transfer":
        recipient = _resolve_recipient(inp.new_owner_email)
        try:
            ownership.transfer(inp.resource_type, rid, "user", recipient["sub"])
        except ValueError as e:
            raise AuthzDenied(409, "transfer_failed", str(e))
        return {"ok": True, "resource_id": rid, "new_owner": recipient.get("email")}

    if inp.op == "share":
        recipient = _resolve_recipient(inp.email)
        ownership.grant(inp.resource_type, rid, "user", recipient["sub"],
                        inp.permission, granted_by=ctx.sub)
        return {"ok": True, "resource_id": rid, "shared_with": recipient.get("email"),
                "permission": inp.permission}

    # unshare
    recipient = _resolve_recipient(inp.email)
    removed = ownership.revoke(inp.resource_type, rid, "user", recipient["sub"])
    return {"ok": True, "resource_id": rid, "unshared_with": recipient.get("email"),
            "removed": removed}


CAPABILITIES += [
    Capability(
        key="resources.govern",
        handler=_resources,
        Input=ResourceInput,
        authz=RESOURCE_GOVERN(),
        description=(
            "Govern an OWNED resource (ADR 0030) without reading its content. "
            "op=list: resources you govern (platform admins see all); op=get: owner + "
            "shares + metadata; op=transfer: hand ownership to `new_owner_email` (the "
            "previous owner keeps write access); op=share/unshare: grant/revoke access to "
            "`email` (`permission` read|write). Pilot resource_type='datastore_namespace'. "
            "Owner OR org/platform admin governing it; never exposes row content."
        ),
        mcp="oto_resource",
        rest=RestBinding("POST", "/api/resources"),
    ),
]
