"""Capacité : OÙ l'org laisse tourner les modèles de ses agents (`orgs.allow_cn_models`).

Un modèle tourne là où sont ses hôtes (`runner_models.hebergement`) ; `cn` dès qu'un seul
de sa liste, un secours compris, est en Chine. Permis par défaut. Écartés par un admin de
l'org : plus proposés (`allowed: false` au catalogue), plus posables
(`model_region_blocked`), et un travail déjà en file sur l'un d'eux est arrêté à la
réservation, raison écrite (`runner_jobs._avec_cle`).

Lecture = membre ; écriture = org_admin. Une déclaration → deux surfaces : REST
`/api/orgs/{id}/model-regions` ici, MCP par la console
`oto_org_settings domain=model_regions` (`org_console.py`).
"""
from __future__ import annotations

from pydantic import BaseModel

from ... import db
from .._authz import ORG_ADMIN_OF, ORG_MEMBER_OF
from .._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES

_ID = {"id": "org_id"}
_PATH = "/api/orgs/{id}/model-regions"


class GetRegionsInput(BaseModel):
    org_id: int


class SetRegionsInput(BaseModel):
    org_id: int
    allow_cn: bool


class RegionsDeModeles(BaseModel):
    org_id: int
    #: Les modèles qui tournent en Chine sont-ils permis aux agents de l'org.
    allow_cn: bool


def _lire(org_id: int) -> dict:
    permise = db.regions_de_modeles.chine_permise(org_id)
    if permise is None:
        raise AuthzDenied(404, "unknown_org", f"Org #{org_id} inconnue.")
    return {"org_id": org_id, "allow_cn": permise}


def _get(ctx: ResolvedCtx, inp: GetRegionsInput) -> dict:
    return _lire(inp.org_id)


def _set(ctx: ResolvedCtx, inp: SetRegionsInput) -> dict:
    if not db.regions_de_modeles.poser_chine_permise(inp.org_id, inp.allow_cn):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    return _lire(inp.org_id)


_ERREURS = (DeclaredError(404, "unknown_org", "l'org n'existe pas"),)

CAPABILITIES += [
    Capability(
        key="org.model_regions.get", handler=_get, Input=GetRegionsInput,
        authz=ORG_MEMBER_OF("org_id"), Output=RegionsDeModeles, errors=_ERREURS,
        description=("Read where the org lets its agents' models run: `allow_cn` = whether "
                     "models with any host in China (`runner.models[].hosting.region` = cn) "
                     "may be used. Default true."),
        rest=RestBinding("GET", _PATH, _ID),
    ),
    Capability(
        key="org.model_regions.set", handler=_set, Input=SetRegionsInput,
        authz=ORG_ADMIN_OF("org_id"), Output=RegionsDeModeles, errors=_ERREURS,
        description=("Allow or turn off, for this org, models that run in China. Off: they "
                     "are no longer offered (`allowed: false`), an agent cannot be set on "
                     "one (`model_region_blocked`), and a job already queued on one is "
                     "stopped at the claim with the reason written. Org admin."),
        rest=RestBinding("PUT", _PATH, _ID),
    ),
]
