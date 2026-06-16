"""Doctrine & skills d'un GROUPE (ADR 0012) — éditeur self-service du chef d'équipe.

Miroir de la doctrine d'org au grain groupe : lecture = membre du groupe
(`GROUP_MEMBER_OF`), écriture = chef d'équipe (`GROUP_ADMIN_OF`, escalade
org_admin/platform). Modèle versionné (slug réservé `claude_md` = doctrine de
base servie en complément de celle de l'org par `get_claude_md`). Édité par le
dashboard via REST `/api/groups/{id}/instructions*`.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from .. import group_store, org_store, roles
from ._authz import GROUP_ADMIN_OF, GROUP_MEMBER_OF
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_GID = {"id": "group_id"}
_GID_SLUG = {"id": "group_id", "slug": "slug"}
_BASE = org_store.BASE_SLUG


class GroupIdInput(BaseModel):
    group_id: int


class InstrGetInput(BaseModel):
    group_id: int
    slug: str
    version: Optional[int] = None


class InstrSetInput(BaseModel):
    group_id: int
    slug: str
    body_md: str
    title: Optional[str] = None
    description: Optional[str] = None


class InstrSlugInput(BaseModel):
    group_id: int
    slug: str


class InstrRevertInput(BaseModel):
    group_id: int
    slug: str
    version: int


def _list(ctx: ResolvedCtx, inp: GroupIdInput) -> dict:
    base = group_store.get_group_instruction(inp.group_id, _BASE)
    return {
        "group_id": inp.group_id,
        "doctrine": (base or {}).get("body_md", "") or "",
        "doctrine_version": (base or {}).get("version"),
        "instructions": group_store.list_group_instructions(inp.group_id),
        "can_edit": roles.can_admin_group(ctx.sub, inp.group_id),
    }


def _get(ctx: ResolvedCtx, inp: InstrGetInput) -> dict:
    instr = group_store.get_group_instruction(inp.group_id, inp.slug, inp.version)
    if not instr:
        raise AuthzDenied(404, "unknown_instruction",
                          f"Instruction `{org_store.normalize_slug(inp.slug)}` absente.")
    return {"group_id": inp.group_id, **instr}


def _set(ctx: ResolvedCtx, inp: InstrSetInput) -> dict:
    if not (inp.body_md or "").strip():
        raise AuthzDenied(400, "empty_body", "body_md vide.")
    slug = org_store.normalize_slug(inp.slug)
    if not slug:
        raise AuthzDenied(400, "invalid_slug", "slug vide ou invalide ([a-z0-9_-]).")
    version = group_store.set_group_instruction(
        inp.group_id, slug, inp.body_md, title=inp.title,
        description=inp.description, set_by=ctx.sub)
    return {"group_id": inp.group_id, "slug": slug, "version": version, "set": True}


def _delete(ctx: ResolvedCtx, inp: InstrSlugInput) -> dict:
    deleted = group_store.delete_group_instruction(inp.group_id, inp.slug)
    return {"group_id": inp.group_id, "slug": org_store.normalize_slug(inp.slug),
            "deleted": deleted}


def _versions(ctx: ResolvedCtx, inp: InstrSlugInput) -> dict:
    return {"group_id": inp.group_id, "slug": org_store.normalize_slug(inp.slug),
            "versions": group_store.list_group_instruction_versions(inp.group_id, inp.slug)}


def _revert(ctx: ResolvedCtx, inp: InstrRevertInput) -> dict:
    old = group_store.get_group_instruction(inp.group_id, inp.slug, inp.version)
    if not old:
        raise AuthzDenied(404, "unknown_version",
                          f"Pas de version {inp.version} pour `{org_store.normalize_slug(inp.slug)}`.")
    new_version = group_store.set_group_instruction(
        inp.group_id, inp.slug, old["body_md"], title=old["title"],
        description=old["description"], set_by=ctx.sub)
    return {"group_id": inp.group_id, "slug": org_store.normalize_slug(inp.slug),
            "version": new_version, "reverted_from": inp.version}


CAPABILITIES += [
    Capability(
        key="group.instruction.list", handler=_list, Input=GroupIdInput,
        authz=GROUP_MEMBER_OF("group_id"),
        description="Group base doctrine + skills index (+ can_edit flag).",
        rest=RestBinding("GET", "/api/groups/{id}/instructions", _GID),
    ),
    Capability(
        key="group.instruction.get", handler=_get, Input=InstrGetInput,
        authz=GROUP_MEMBER_OF("group_id"),
        description="Full markdown of one group instruction (slug `claude_md` = base doctrine).",
        rest=RestBinding("GET", "/api/groups/{id}/instructions/{slug}", _GID_SLUG),
    ),
    Capability(
        key="group.instruction.set", handler=_set, Input=InstrSetInput,
        authz=GROUP_ADMIN_OF("group_id"),
        description=("Create/update a group instruction (team lead). slug `claude_md` "
                     "= the group base doctrine; any other slug = a named skill."),
        mcp="oto_set_group_instruction",
        rest=RestBinding("PUT", "/api/groups/{id}/instructions/{slug}", _GID_SLUG),
    ),
    Capability(
        key="group.instruction.delete", handler=_delete, Input=InstrSlugInput,
        authz=GROUP_ADMIN_OF("group_id"),
        description="Delete a group instruction and its history.",
        rest=RestBinding("DELETE", "/api/groups/{id}/instructions/{slug}", _GID_SLUG),
    ),
    Capability(
        key="group.instruction.versions", handler=_versions, Input=InstrSlugInput,
        authz=GROUP_MEMBER_OF("group_id"),
        description="Version history of one group instruction (metadata, latest first).",
        rest=RestBinding("GET", "/api/groups/{id}/instructions/{slug}/versions", _GID_SLUG),
    ),
    Capability(
        key="group.instruction.revert", handler=_revert, Input=InstrRevertInput,
        authz=GROUP_ADMIN_OF("group_id"),
        description="Restore an older version of a group instruction as a new version.",
        rest=RestBinding("POST", "/api/groups/{id}/instructions/{slug}/revert", _GID_SLUG),
    ),
]
