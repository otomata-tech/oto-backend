"""Fiche profil « situation avec oto » de l'utilisateur — surface REST.

`oto_profile` (`tools/profile.py`) expose déjà get/update en MCP (l'agent entretient
la fiche au fil de l'eau). Ce module donne la MÊME fiche au dashboard en **REST-only**
(édition manuelle dans la section Context) : même store `user_account_profile`
(shallow-merge), même schéma suggéré `PROFILE_FIELDS`. Le data model reste ouvert
(clés libres acceptées). `SUB_ONLY` → chacun voit/édite la sienne.
"""
from __future__ import annotations

from pydantic import BaseModel

from .. import db
from ..tools.profile import PROFILE_FIELDS
from ._authz import SUB_ONLY
from ._types import Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class _NoInput(BaseModel):
    pass


class SetProfileInput(BaseModel):
    # Shallow-mergé dans le JSONB `profile` (clé="" efface la valeur, pas la clé).
    fields: dict = {}


def _get_profile(ctx: ResolvedCtx, inp: _NoInput) -> dict:
    return {**db.get_account_profile(ctx.sub), "fields": PROFILE_FIELDS}


def _set_profile(ctx: ResolvedCtx, inp: SetProfileInput) -> dict:
    return {**db.update_account_profile(ctx.sub, inp.fields or {}), "fields": PROFILE_FIELDS}


CAPABILITIES += [
    Capability(
        key="me.profile.get", handler=_get_profile, Input=_NoInput,
        authz=SUB_ONLY,
        description="The user's « situation with oto » profile (free key/value model "
                    "re-read into every session) plus the suggested field schema.",
        rest=RestBinding("GET", "/api/me/profile"),
    ),
    Capability(
        key="me.profile.set", handler=_set_profile, Input=SetProfileInput,
        authz=SUB_ONLY,
        description="Shallow-merge `fields` into the user's profile (empty string clears a value).",
        rest=RestBinding("PUT", "/api/me/profile"),
    ),
]
