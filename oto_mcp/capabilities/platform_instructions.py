"""Édition du bloc d'instructions PLATEFORME (#50) — secret sauce (bloc A). Surface
admin plateforme (PLATFORM_ADMIN) : ce bloc est injecté à TOUS les comptes au handshake
et **inviolable par l'org**. (L'onboarding n'est plus un bloc : c'est un projet, ADR 0032 §7.)

Pattern ADR 0009 : capacités par-verbe (avec REST `/api/admin/platform-instructions`)
+ un outil MCP op-aware consolidé `oto_admin_platform_instructions` qui les réutilise.
Prose (pas un credential) → l'édition est permise aussi côté MCP."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from .. import db, instructions
from ._authz import PLATFORM_ADMIN
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_KEYS = (instructions.KEY_SECRET_SAUCE,)


class _NoInput(BaseModel):
    pass


class KeyInput(BaseModel):
    key: str


class SetInput(BaseModel):
    key: str
    body_md: str


class PlatformInstrInput(BaseModel):
    op: Literal["list", "get", "set"]
    key: Optional[str] = None
    body_md: Optional[str] = None


def _require_key(key: Optional[str]) -> str:
    if key not in _KEYS:
        raise AuthzDenied(400, "unknown_key",
                          f"`key` doit être l'un de {', '.join(_KEYS)}.")
    return key


def _view(key: str) -> dict:
    """L'état effectif d'un bloc : la ligne DB, ou le seed (is_seed=True) si jamais
    posée. `default_md` accompagne toujours (pour un bouton « rétablir le défaut »)."""
    row = db.get_platform_instruction(key)
    default_md = instructions.default_block(key)
    if row:
        return {**row, "is_seed": False, "default_md": default_md}
    return {"key": key, "body_md": default_md, "updated_at": None,
            "updated_by": None, "is_seed": True, "default_md": default_md}


def _list(ctx: ResolvedCtx, inp: _NoInput) -> dict:
    return {"blocks": [_view(k) for k in _KEYS], "keys": list(_KEYS)}


def _get(ctx: ResolvedCtx, inp: KeyInput) -> dict:
    return _view(_require_key(inp.key))


def _set(ctx: ResolvedCtx, inp: SetInput) -> dict:
    key = _require_key(inp.key)
    db.set_platform_instruction(key, inp.body_md or "", ctx.sub)
    return _view(key)


def _platform_instructions(ctx: ResolvedCtx, inp: PlatformInstrInput) -> dict:
    if inp.op == "list":
        return _list(ctx, _NoInput())
    if inp.op == "get":
        return _get(ctx, KeyInput(key=inp.key or ""))
    if inp.body_md is None:
        raise AuthzDenied(400, "missing_body", "`body_md` requis pour set.")
    return _set(ctx, SetInput(key=inp.key or "", body_md=inp.body_md))


CAPABILITIES += [
    # MCP op-aware consolidé.
    Capability(
        key="platform.instructions", handler=_platform_instructions,
        Input=PlatformInstrInput, authz=PLATFORM_ADMIN,
        description=(
            "Platform-level injected instruction block (#50), shown to EVERY account at "
            "handshake, editable only by platform admins, immutable by orgs. op=list / "
            "get (`key`) / set (`key`, `body_md`). key = 'secret_sauce' (block A: posture "
            "+ usage loop + derived namespace catalog, always injected)."),
        mcp="oto_admin_platform_instructions",
    ),
    # Faces REST par-verbe (dashboard éditeur).
    Capability(
        key="platform.instructions.list", handler=_list, Input=_NoInput,
        authz=PLATFORM_ADMIN,
        description="List platform instruction blocks (A/B) with their effective content.",
        rest=RestBinding("GET", "/api/admin/platform-instructions"),
    ),
    Capability(
        key="platform.instructions.get", handler=_get, Input=KeyInput,
        authz=PLATFORM_ADMIN,
        description="Get one platform instruction block by `key`.",
        rest=RestBinding("GET", "/api/admin/platform-instructions/{key}"),
    ),
    Capability(
        key="platform.instructions.set", handler=_set, Input=SetInput,
        authz=PLATFORM_ADMIN,
        description="Edit one platform instruction block (`key`, `body_md`).",
        rest=RestBinding("PUT", "/api/admin/platform-instructions/{key}"),
    ),
]
