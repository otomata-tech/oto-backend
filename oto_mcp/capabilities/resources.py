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
    new_owner_email: Optional[str] = None   # transfer → un utilisateur
    new_owner_org: Optional[int] = None     # transfer → une de SES orgs (ADR 0030, owner_type='org')
    email: Optional[str] = None             # share / unshare (principal user)
    permission: Literal["read", "write"] = "write"  # share


def _check_type(resource_type: str) -> None:
    if resource_type not in _OPS:
        raise AuthzDenied(400, "unsupported_resource_type",
                          f"type `{resource_type}` non supporté ({list(_OPS)}).")


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


def _enrich_project(row: dict) -> dict:
    return {
        "resource_type": "project",
        "resource_id": str(row["id"]),
        "name": row["name"],
        "owner_type": row.get("owner_type"),
        "owner_id": row.get("owner_id"),
        "owner_label": _owner_label(row.get("owner_type"), row.get("owner_id")),
        "archived_at": row.get("archived_at"),
        "created_at": row.get("created_at"),
    }


# Dispatch par type de ressource pour list/get (transfer/share/unshare sont déjà
# génériques via le seam `ownership`). Étendre = une entrée ici.
# Lambdas (pas des références directes) → `db.X` est résolu au call-time (testable,
# le monkeypatch de db.X est vu).
_OPS: dict[str, dict] = {
    "datastore_namespace": {
        "list_all": lambda: db.list_all_datastore_namespaces(),
        "list_for_owners": lambda owners: db.list_datastore_namespaces_for_owners(owners),
        "get_by_id": lambda i: db.get_datastore_namespace_by_id(i),
        "enrich": _enrich_datastore,
    },
    "project": {
        "list_all": lambda: db.list_all_projects(),
        "list_for_owners": lambda owners: db.list_projects_for_owners(owners),
        "get_by_id": lambda i: db.get_project_by_id(i),
        "enrich": _enrich_project,
    },
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
    ops = _OPS[inp.resource_type]

    if inp.op == "list":
        # PLATEFORME → tout ; sinon → ce que l'acteur gouverne (perso + orgs/groupes
        # qu'il administre). Plan gouvernance : métadonnées seulement, pas de contenu.
        if access.is_platform_operator(ctx.sub):
            rows = ops["list_all"]()
        else:
            scope = ownership.accessor_scope(ctx.sub)
            governed = [("user", ctx.sub)]
            governed += [("org", str(o)) for o in scope.org_ids if roles.is_org_admin(ctx.sub, o)]
            rows = ops["list_for_owners"](governed)
        return {"resource_type": inp.resource_type,
                "resources": [ops["enrich"](r) for r in rows]}

    if inp.resource_id is None:
        raise AuthzDenied(400, "missing_resource_id", "`resource_id` requis.")
    rid = str(inp.resource_id)

    if inp.op == "get":
        row = ops["get_by_id"](int(rid))
        if not row:
            raise AuthzDenied(404, "not_found", "ressource introuvable.")
        out = ops["enrich"](row)
        out["grants"] = _grants_view(inp.resource_type, rid)
        return out

    if inp.op == "transfer":
        # Cible : une de SES orgs (owner_type='org') OU un utilisateur (par email).
        # Transférer VERS une org exige d'en être membre (on n'envoie pas une ressource
        # dans une org où on n'est pas — comme on ne crée un namespace d'org que membre).
        if inp.new_owner_org is not None:
            org_id = int(inp.new_owner_org)
            if not roles.is_org_member(ctx.sub, org_id):
                raise AuthzDenied(403, "not_org_member",
                                  "tu dois être membre de l'org cible pour lui transférer une ressource.")
            new_owner_type, new_owner_id = "org", str(org_id)
            new_owner_label = _owner_label("org", str(org_id))
        else:
            recipient = _resolve_recipient(inp.new_owner_email)
            new_owner_type, new_owner_id = "user", recipient["sub"]
            new_owner_label = recipient.get("email")
        try:
            ownership.transfer(inp.resource_type, rid, new_owner_type, new_owner_id)
        except ValueError as e:
            raise AuthzDenied(409, "transfer_failed", str(e))
        return {"ok": True, "resource_id": rid, "new_owner": new_owner_label}

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
            "shares + metadata; op=transfer: hand ownership to a user (`new_owner_email`) "
            "OR to one of YOUR orgs (`new_owner_org`, you must be a member); the previous "
            "owner keeps write access; op=share/unshare: grant/revoke access to "
            "`email` (`permission` read|write). resource_type ∈ {datastore_namespace, project}. "
            "Owner OR org/platform admin governing it; never exposes row content."
        ),
        mcp="oto_resource",
        rest=RestBinding("POST", "/api/resources"),
    ),
]
