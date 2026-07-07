"""Préférence de langue de l'utilisateur — le niveau USER d'une préférence d'UI.

`locale` ∈ {'en', 'fr'} : la langue choisie pour le dashboard. Purement UI (pas un
canal agent) → REST-only, `authz=SUB_ONLY` (chacun écrit la sienne). Exposée en
lecture par `GET /api/me` (`locale`), écrite ici par `PUT /api/me/locale`.

La validation de l'énum vit dans l'`Input` pydantic (Literal) — une valeur hors
{'en','fr'} est rejetée par l'adaptateur (400) avant d'atteindre le handler.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from .. import db
from ._authz import SUB_ONLY
from ._types import Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class SetLocaleInput(BaseModel):
    locale: Literal["en", "fr"]


def _set_locale(ctx: ResolvedCtx, inp: SetLocaleInput) -> dict:
    db.set_user_locale(ctx.sub, inp.locale)
    return {"locale": inp.locale}


CAPABILITIES += [
    Capability(
        key="me.locale.set", handler=_set_locale, Input=SetLocaleInput,
        authz=SUB_ONLY,
        description="Set the user's dashboard UI language preference (`locale`, 'en' or 'fr').",
        rest=RestBinding("PUT", "/api/me/locale"),
    ),
]
