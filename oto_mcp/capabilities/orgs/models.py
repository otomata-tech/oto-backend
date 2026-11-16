"""Capacité : les modèles qu'une org ÉTEINT pour ses agents (`orgs.disabled_models`).

Un interrupteur par modèle du catalogue (`runner_models.MODELES`), hors abonnements
personnels (`sub:*` : chacun le sien, rien à éteindre pour l'org). Tous allumés par
défaut. Éteint par un admin de l'org : plus proposé (`allowed: false, disabled: true` au
catalogue, jamais par défaut), plus posable (`model_disabled`). Un agent déjà dessus ne
tourne plus, sans repli sur un autre modèle : ses occurrences sont refusées et nommées
(`refused_model_disabled` sur sa page, 409 `model_disabled` au webhook), un lancement à la
main est refusé (`model_disabled`), une campagne s'arrête (`stop_reason`), et un travail
déjà en file est arrêté à la réservation, raison écrite (`runner_jobs._avec_cle`). La
bascule rend les agents de l'org posés sur ce modèle (`agents_on_model`), en pause
compris : rallumés, ils buteraient sur le même arrêt.

⚠️ Le modèle par DÉFAUT de l'org ne s'éteint pas (`model_is_default`) : c'est celui que
l'écran propose à une nouvelle pose. Le défaut se redérive (`_modele.etat_servi`), donc
éteindre l'actuel défaut exige d'en avoir un autre — un geste à part que l'écran ne
propose pas.

Lecture = membre ; écriture = org_admin. Une déclaration → deux surfaces : REST
`/api/orgs/{id}/models` ici, MCP par la console `oto_org_settings domain=models`
(`org_console.py`).
"""
from __future__ import annotations

from pydantic import BaseModel

from ... import db, runner_models
from .. import _modele
from .._authz import ORG_ADMIN_OF, ORG_MEMBER_OF
from .._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES

_ID = {"id": "org_id"}
_PATH = "/api/orgs/{id}/models"


class GetModelsInput(BaseModel):
    org_id: int


class SetModelInput(BaseModel):
    org_id: int
    model: str
    enabled: bool


class ModelesDeLOrg(BaseModel):
    org_id: int
    #: Les ids des modèles éteints par un admin de l'org, triés. Vide = tous allumés.
    disabled: list[str]


class AgentsDuModele(BaseModel):
    #: Combien d'agents de l'org sont posés sur le modèle basculé, en pause compris.
    count: int
    #: Leurs ids (`runner_triggers.id`), triés.
    ids: list[int]


class ModeleBascule(ModelesDeLOrg):
    #: Le modèle basculé.
    model: str
    #: Les agents de l'org posés sur ce modèle : éteint, aucun ne tourne plus.
    agents_on_model: AgentsDuModele


def lire(org_id: int) -> dict:
    eteints = db.modeles_eteints.eteints(org_id)
    if eteints is None:
        raise AuthzDenied(404, "unknown_org", f"Org #{org_id} inconnue.")
    return {"org_id": org_id, "disabled": eteints}


def basculer(org_id: int, model: str, allume: bool) -> dict:
    """Allume ou éteint UN modèle pour l'org. Partagé par le REST et la console MCP."""
    m = next((x for x in runner_models.MODELES if x.id == model), None)
    if m is None or m.family in runner_models.FAMILLES_PERSONNELLES:
        connus = ", ".join(x.id for x in runner_models.MODELES
                           if x.family not in runner_models.FAMILLES_PERSONNELLES)
        raise AuthzDenied(400, "unknown_model",
                          f"`{model}` n'est pas un modèle que l'org peut allumer ou éteindre "
                          f"(modèles : {connus}).")
    lire(org_id)   # 404 d'abord : une org inconnue n'a pas de défaut à protéger
    if not allume:
        etat = _modele.etat_servi(db.runner_arme(org_id), org_id)
        defaut = next((x["id"] for x in etat.get("models") or [] if x.get("default")), None)
        if defaut == model:
            raise AuthzDenied(
                409, "model_is_default",
                f"`{model}` est le modèle par défaut de l'organisation : c'est celui que "
                "propose l'écran à une nouvelle pose. Il reste allumé.")
    if not db.modeles_eteints.poser(org_id, model, allume):
        raise AuthzDenied(404, "unknown_org", f"Org #{org_id} inconnue.")
    agents = db.modeles_eteints.agents_sur(org_id, model)
    return {**lire(org_id), "model": model,
            "agents_on_model": {"count": len(agents), "ids": agents}}


def _get(ctx: ResolvedCtx, inp: GetModelsInput) -> dict:
    return lire(inp.org_id)


def _set(ctx: ResolvedCtx, inp: SetModelInput) -> dict:
    return basculer(inp.org_id, inp.model, inp.enabled)


_ERREURS_GET = (DeclaredError(404, "unknown_org", "l'org n'existe pas"),)
_ERREURS_SET = (
    DeclaredError(404, "unknown_org", "l'org n'existe pas"),
    DeclaredError(400, "unknown_model", "le modèle n'est pas au catalogue, ou c'est un "
                  "abonnement personnel"),
    DeclaredError(409, "model_is_default", "le modèle par défaut de l'org ne s'éteint pas"),
)

CAPABILITIES += [
    Capability(
        key="org.models.get", handler=_get, Input=GetModelsInput,
        authz=ORG_MEMBER_OF("org_id"), Output=ModelesDeLOrg, errors=_ERREURS_GET,
        description=("Read the models the org turned off for its agents: `disabled` = their "
                     "ids (`runner.models[].id`). Empty by default: every model is on."),
        rest=RestBinding("GET", _PATH, _ID),
    ),
    Capability(
        key="org.models.set", handler=_set, Input=SetModelInput,
        authz=ORG_ADMIN_OF("org_id"), Output=ModeleBascule, errors=_ERREURS_SET,
        description=("Turn one model on or off for this org's agents (`model`, `enabled`). "
                     "Off: no longer offered (`allowed: false, disabled: true`), an agent "
                     "cannot be set on it (`model_disabled`), an agent already on it stops "
                     "running, with no fallback model (each occurrence refused as "
                     "`model_disabled`, a fleet stopped), and a job already queued on it "
                     "is stopped at the claim with the reason written. Returns "
                     "`agents_on_model` (`count`, `ids`): the org's agents set on this "
                     "model, paused ones included. The org's default model cannot be "
                     "turned off (`model_is_default`); personal subscription models are "
                     "not switchable. Org admin."),
        rest=RestBinding("PUT", _PATH, _ID),
    ),
]
