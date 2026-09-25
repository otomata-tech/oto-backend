"""Capacités orgs super-admin (ADR 0009, barreau 2c).

Écritures sur les orgs tierces : créer une org, accorder/révoquer un
entitlement de namespace gouverné. Agir sur une org tierce = escalade en masse
→ réservé au **SUPER_ADMIN** (pas à l'admin opérationnel). Les réponses sont des
**supersets** des deux contrats historiques (mêmes clés qu'avant côté MCP ET
côté REST) pour ne casser aucun consommateur.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from ... import org_store
from .._authz import SUPER_ADMIN
from .._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .members import _resolve_target
from .update import ERREURS_ARCHIVAGE, archive_org_ou_409
from ..registry import CAPABILITIES

_ID = {"id": "org_id"}

_SELF = "me"  # `admin="me"` = « je crée pour moi » (l'opérateur garde la main)


class CreateOrgInput(BaseModel):
    name: str
    # Responsable de la nouvelle org : email | sub | "me". Omis = l'appelant.
    admin: Optional[str] = None


class OrgIdInput(BaseModel):
    org_id: int


def _create_org(ctx: ResolvedCtx, inp: CreateOrgInput) -> dict:
    """Crée une org **avec son responsable** (#297).

    Cette console créait l'org et rien d'autre : elle naissait donc **sans aucun
    membre, donc sans personne pour l'administrer** — l'état que la garde de #280
    refuse désormais, mais qui pouvait toujours naître ici. Deux orgs de juin sont
    encore dans cet état en prod.

    `admin` (email | sub | `"me"`) nomme le responsable, parce que le cas d'usage réel
    de cette console est de provisionner une org **pour quelqu'un d'autre** : le nommer
    est alors le geste juste, et l'opérateur plateforme reste hors de l'org cliente.
    Omis, c'est l'appelant — la garde d'invariant l'emporte sur la pureté (l'org a
    toujours exactement un org_admin à la naissance), le seul consommateur vivant de
    la face REST poste `{name}` seul, et cette custody-là est **visible** (l'opérateur
    apparaît dans les membres) et **transférable** (nommer le client org_admin, puis
    se retirer — l'anti-lockout de #273 interdit de laisser l'org sans responsable).
    Elle ne confère par ailleurs aucun droit neuf : la capacité est SUPER_ADMIN-only,
    et un super_admin est déjà org_admin de TOUTE org par escalade (`roles.py`).

    La cible est résolue AVANT la création : un email inconnu doit échouer sans laisser
    derrière lui l'org orpheline qu'on cherche justement à ne plus produire.
    """
    name = (inp.name or "").strip()
    if not name:
        raise AuthzDenied(400, "missing_name", "Nom d'org requis.")
    target = (inp.admin or "").strip()
    admin_sub = ctx.sub if target.lower() in ("", _SELF) else _resolve_target(target)
    # Le front suit le RESPONSABLE, pas l'opérateur : provisionner une org d'un
    # tenant tiers depuis un compte oto doit produire une org de ce tenant
    # (`front_of`, cf. create_org).
    org_id = org_store.create_org(name, created_by=ctx.sub, front_of=admin_sub)
    org_store.add_org_member(org_id, admin_sub, "org_admin", actor=ctx.sub)
    # superset REST({id}) + MCP({org_id,name}) ; `admin_sub` rend la custody explicite
    # dans la réponse plutôt qu'implicite dans le code.
    return {"id": org_id, "org_id": org_id, "name": name, "admin_sub": admin_sub}


def _archive_org(ctx: ResolvedCtx, inp: OrgIdInput) -> dict:
    if not org_store.get_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    archived = archive_org_ou_409(inp.org_id)
    return {"ok": True, "org_id": inp.org_id, "archived": archived}


CAPABILITIES += [
    Capability(
        key="org.admin.create", handler=_create_org, Input=CreateOrgInput,
        authz=SUPER_ADMIN,
        description="[super admin] Create an organization (perimeter) with its admin. "
                    "`admin` = email|sub of the person who will run it (or \"me\"); "
                    "omitted, you are it. Returns its id.",
        # MCP fusionné dans oto_admin_org(op=create). REST conservé (dashboard).
        rest=RestBinding("POST", "/api/admin/orgs"),
    ),
    Capability(
        key="org.admin.archive", handler=_archive_org, Input=OrgIdInput,
        authz=SUPER_ADMIN, errors=ERREURS_ARCHIVAGE,
        description="[super admin] Archive (soft-delete) an org: hidden from all "
                    "listings, reversible in DB. Members fall back to their other orgs. "
                    "Refused (409 `org_has_active_subscription`) while the org has an "
                    "active subscription: it must be canceled first.",
        # MCP fusionné dans oto_admin_org(op=archive). REST conservé (dashboard).
        rest=RestBinding("DELETE", "/api/admin/orgs/{id}", _ID),
    ),
]
