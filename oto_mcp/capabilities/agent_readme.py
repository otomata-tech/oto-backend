"""Agent README personnel de l'utilisateur — le niveau USER du concept agent_readme.

Un agent_readme = prose markdown libre injectée à chaque session (bloc C des
instructions), CUMULÉE du général au spécifique : plateforme (bloc A, éditeur admin)
→ org (`org_instructions` slug `claude_md`) → équipe (`org_group_instructions` slug
`claude_md`) → user (table `user_agent_readme`, ce module). Les trois premiers niveaux
ont déjà leur surface ; celui-ci donne la sienne au user.

REST-only (dashboard `/account`) : l'agent entretient déjà la fiche structurée via
`oto_profile` — le README est la voix VERBATIM de l'utilisateur, pas un canal agent.
"""
from __future__ import annotations

from pydantic import BaseModel

from .. import guide_store
from ._authz import SUB_ONLY
from ._types import Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class _NoInput(BaseModel):
    pass


class SetReadmeInput(BaseModel):
    body_md: str = ""


def _get_readme(ctx: ResolvedCtx, inp: _NoInput) -> dict:
    return guide_store.get_init_guide("user", ctx.sub)


def _set_readme(ctx: ResolvedCtx, inp: SetReadmeInput) -> dict:
    return guide_store.set_init_guide("user", ctx.sub, inp.body_md)


CAPABILITIES += [
    Capability(
        key="me.agent_readme.get", handler=_get_readme, Input=_NoInput,
        authz=SUB_ONLY,
        description="The user's personal agent README (free markdown injected into "
                    "every session's instructions, after the org and team READMEs).",
        rest=RestBinding("GET", "/api/me/agent-readme"),
    ),
    Capability(
        key="me.agent_readme.set", handler=_set_readme, Input=SetReadmeInput,
        authz=SUB_ONLY,
        description="Set the user's personal agent README (`body_md`; empty clears it).",
        rest=RestBinding("PUT", "/api/me/agent-readme"),
    ),
]
