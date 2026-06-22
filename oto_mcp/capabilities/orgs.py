"""Capacités du domaine orgs (ADR 0009). Barreau 1 : `org.use_org`.

`oto_use_org` (MCP) et `PUT /api/me/active-org` (REST) étaient câblés séparément
(drift de surface). Une seule `Capability` les co-déclare ; les deux adaptateurs
en dérivent.
"""
from __future__ import annotations

import os

from pydantic import BaseModel, Field

from .. import org_store, session_org
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_MAX_ORGS_PER_USER = int(os.environ.get("OTO_MCP_MAX_ORGS_PER_USER", "10"))


class NoInput(BaseModel):
    pass


class CreateOrgInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)


def _create_org(ctx: ResolvedCtx, inp: CreateOrgInput) -> dict:
    """Self-serve : crée un espace, en fait l'admin, le bascule actif."""
    if org_store.count_orgs_created_by(ctx.sub) >= _MAX_ORGS_PER_USER:
        raise AuthzDenied(429, "org_quota",
                          f"Limite de {_MAX_ORGS_PER_USER} espaces créés atteinte.")
    name = inp.name.strip()
    if not name:
        raise AuthzDenied(400, "invalid_name", "Nom d'espace requis.")
    org_id = org_store.create_org(name, created_by=ctx.sub)
    org_store.add_org_member(org_id, ctx.sub, "org_admin")
    org_store.set_active_org(ctx.sub, org_id)  # nouvelle org = ton org maison (défaut)
    sid = session_org.current_session_id()
    if sid is not None:
        session_org.set_override(sid, org_id)  # + active dans la conversation courante
    return {"org_id": org_id, "name": name, "active_org": org_id, "org_role": "org_admin"}


class UseOrgInput(BaseModel):
    org: str  # id (ex "3") ou nom exact — contrat unifié MCP + REST


def _use_org(ctx: ResolvedCtx, inp: UseOrgInput) -> dict:
    """Bascule l'org active (ADR 0023). Sur la face MCP (session présente) =
    **override de session éphémère** (cette conversation seulement) ; sur la face
    REST (dashboard) = pose l'**org maison** persistante (défaut). `org` = id/nom."""
    try:
        org_id = org_store.resolve_org_for_user(ctx.sub, inp.org)  # garantit l'appartenance
    except ValueError as e:
        raise AuthzDenied(404, "unknown_org", str(e))
    sid = session_org.current_session_id()
    if sid is not None:
        session_org.set_override(sid, org_id)        # MCP : org de session
    else:
        org_store.set_active_org(ctx.sub, org_id)    # REST : org maison
    o = org_store.get_org(org_id)
    return {"active_org": org_id, "name": o["name"] if o else None}


def _clear_org(ctx: ResolvedCtx, inp: NoInput) -> dict:
    """Retour au profil perso/global (ADR 0015/0023). MCP = perso pour CETTE
    conversation (override de session) ; REST = efface l'org maison."""
    sid = session_org.current_session_id()
    if sid is not None:
        session_org.set_override(sid, None)          # MCP : perso pour la session
    else:
        org_store.clear_active_org(ctx.sub)          # REST : efface la maison
    return {"active_org": None}


CAPABILITIES += [
    Capability(
        key="org.create",
        handler=_create_org,
        Input=CreateOrgInput,
        authz=SUB_ONLY,
        description=(
            "Create your own organization (workspace). You become its org_admin "
            "and it becomes your active org. Self-serve — any authenticated user."
        ),
        mcp="oto_create_org",
        rest=RestBinding("POST", "/api/me/orgs"),
        refresh_visibility=True,  # bascule l'org active → toolbox de la nouvelle org
    ),
    Capability(
        key="org.use_org",
        handler=_use_org,
        Input=UseOrgInput,
        authz=SUB_ONLY,
        description=(
            "Switch the organization you act under, FOR THIS CONVERSATION ONLY "
            "(by id or name). The active org decides which shared secrets, tools "
            "and data scope your calls. Ephemeral: it does NOT change your home "
            "org or any other conversation, and a new conversation starts back "
            "on your home org. Set the org once per conversation when needed."
        ),
        mcp="oto_use_org",
        rest=RestBinding("PUT", "/api/me/active-org"),
        refresh_visibility=True,  # recharge la toolbox de l'org de session (live in-session)
    ),
    Capability(
        key="org.clear",
        handler=_clear_org,
        Input=NoInput,
        authz=SUB_ONLY,
        description=(
            "Act under your personal/global profile (no org) FOR THIS "
            "CONVERSATION — your personal toolset and settings apply. Ephemeral: "
            "a new conversation reverts to your home org."
        ),
        mcp="oto_clear_org",
        rest=RestBinding("DELETE", "/api/me/active-org"),
        refresh_visibility=True,  # retour profil perso/global → toolbox perso
    ),
]
