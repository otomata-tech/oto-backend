"""Capacité : le PLAFOND de consommation des abonnements, réglé par l'org.

Un agent de famille `claude_subscription` tourne sur l'abonnement PERSONNEL de son
porteur (`capabilities/_abonnement`). L'org règle la part maximale, en %, de l'usage
TOTAL du compte du fournisseur (fenêtres cinq heures et sept jours, usage perso
compris) que ses travaux peuvent atteindre : au-delà, les travaux suivants ATTENDENT
la réinitialisation — le run en cours finit toujours. Sans réglage, le défaut du code
(`_abonnement.DEFAUT_LIMITE_PCT`). Une personne peut poser le sien
(`PATCH /api/me/model-subscriptions/{family}`), plus bas ou plus haut (≤ 95) : il
remplace celui de l'org sur son forfait — calculé par `_abonnement.seuil`.

Le MODE de l'org, sur la même ressource : `personnel` (défaut — chaque travail sur
l'abonnement de son demandeur) ou `pool` (sur l'abonnement d'un membre qui l'a prêté à
l'org, flottes comprises ; `org_subscription_pool`). Réglé à part
(`PUT …/{family}/mode`) : un plafond et un mode ne se règlent pas dans le même geste,
et un corps partiel ne dirait pas lequel des deux on voulait laisser.

Lecture = membre ; écriture = org_admin. Une déclaration → deux surfaces : REST
`/api/orgs/{id}/model-subscriptions/{family}` ici, MCP par la console
`oto_org_settings domain=model_subscriptions` (`org_console.py`).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from ... import org_store
from ...db import org_subscription_limits, org_subscription_pool
from .. import _abonnement
from .._authz import ORG_ADMIN_OF, ORG_MEMBER_OF
from .._types import (AuthzDenied, Capability, DeclaredError, ResolvedCtx,
                      RestBinding)
from ..registry import CAPABILITIES

_ID = {"id": "org_id"}
_PATH = "/api/orgs/{id}/model-subscriptions/{family}"
_PATH_MODE = "/api/orgs/{id}/model-subscriptions/{family}/mode"
_PATH_REPLI = "/api/orgs/{id}/model-subscriptions/{family}/api-fallback"


class Preteur(BaseModel):
    """Un membre qui prête son abonnement au pool de l'org — et RIEN de plus.

    ⚠️ Ni palier, ni état de connexion, ni échéance, ni plafond perso : un abonnement
    appartient à qui le paie (`capabilities/me_abonnements.py`), et prêter partage sa
    CAPACITÉ, pas son état. « Au plafond jusqu'à … » dirait à toute l'org l'usage
    TOTAL d'un compte, usage personnel compris. Même forme pour un admin d'org
    (décision d'Alexis, 08/10/2026)."""
    sub: str
    usable_now: bool = Field(
        description=("Whether this subscription is usable right now (connected, or at a "
                     "cap that has already lifted). It does not promise the job queue "
                     "will pick it: one job in flight per lender, and only while the "
                     "org runs in `pool` mode."))


class OrgPlafond(BaseModel):
    """Le plafond de l'org pour une famille d'abonnement."""
    org_id: int
    family: str
    limit_pct: int = Field(
        description=("The cap, in % of each member's provider account TOTAL usage "
                     "(5-hour and 7-day windows). Past it, the org's next jobs on that "
                     "subscription wait for the window to reset."))
    default: bool = Field(
        description="`true` = the org has set nothing; this is the platform default.")
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None
    mode: str = Field(
        description=("`personnel`: each job runs on the subscription of the member who "
                     "asked for it. `pool`: the org's jobs — fleets included — run "
                     "only on the subscription of a member who LENT theirs to this org "
                     "(`PATCH /api/me/model-subscriptions/{family}` `lent_to`), the "
                     "least recently used free one first. Unset (`mode_chosen: false`, "
                     "read as `personnel`): the pool COMPLEMENTS — a member whose own "
                     "subscription is usable here keeps using it; the lent ones serve "
                     "members without one (or whose own is at its cap) and fleets."))
    mode_chosen: bool = Field(
        default=False,
        description=("`true` = an org admin set `mode`. `false` = nothing set: lending "
                     "is enough for the pool to serve members who have no usable "
                     "subscription of their own, without anyone's own being replaced."))
    pool_size: int = Field(
        description=("How many members currently lend this org a usable subscription "
                     "(the `lenders` with `usable_now`). In `pool` mode, zero means the "
                     "org's jobs wait."))
    lenders: list[Preteur] = Field(
        default_factory=list,
        description=("One entry per member who lends this org their subscription, in "
                     "either mode: who (`sub`) and whether it is usable right now "
                     "(`usable_now`) — never their tier, state, reset time or usage."))
    api_fallback: bool = Field(
        default=False,
        description=("`true`: a job whose subscription (the requester's, or the whole "
                     "pool) is EXHAUSTED replays on this org's own deposited API key, "
                     "same tier, instead of waiting for the plan to reset. `false`: those "
                     "jobs wait. Without a setting of the org, the instance's default "
                     "applies. It never spends anyone else's key."))


class GetOrgPlafondInput(BaseModel):
    org_id: int
    family: str


class SetOrgPlafondInput(BaseModel):
    org_id: int
    family: str
    # Requis, nullable : l'omettre est une erreur de forme, `null` revient au défaut.
    limit_pct: Optional[int] = Field(
        ..., description="1..100, or `null` to go back to the platform default.")


class SetOrgModeInput(BaseModel):
    org_id: int
    family: str
    mode: Literal["personnel", "pool"] = Field(
        description="`personnel` (each requester's own subscription) or `pool`.")


class SetOrgRepliInput(BaseModel):
    org_id: int
    family: str
    api_fallback: bool = Field(
        description=("`true`: when the subscription is exhausted, the org's jobs replay "
                     "on the org's OWN deposited API key, same tier, instead of waiting "
                     "for the plan to reset — at most one such run at a time per "
                     "subscription, each capped in tokens. Never spends anyone else's "
                     "key: with no key deposited by this org, the jobs wait either way. "
                     "`false`: those jobs wait for the reset. Unset, the instance's "
                     "default applies."))


def _exiger(org_id: int, famille: str) -> None:
    if not org_store.get_org(org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{org_id} inconnue.")
    if not _abonnement.est_abonnement(famille):
        raise AuthzDenied(
            400, "unknown_family",
            f"`{famille}` n'est pas une famille servie par abonnement "
            f"({', '.join(sorted(_abonnement.FAMILLES))}).")


def _servi(org_id: int, famille: str) -> dict:
    repli = {"api_fallback": org_subscription_pool.repli_api_actif(org_id, famille)}
    preteurs = [{"sub": p["sub"], "usable_now": bool(p["servable"])}
                for p in org_subscription_pool.preteurs(org_id, famille)]
    # `pool_size` se DÉDUIT de `lenders` : une seule lecture, aucun écart possible
    # entre le compte et la liste montrés sur le même écran.
    # Sans mode CHOISI, l'écran dit `personnel` comme avant, et `mode_chosen: false` :
    # le pool complète (`org_subscription_pool.sql_par_le_pool`).
    regle = org_subscription_pool.mode_regle(org_id, famille)
    pool = {"mode": regle or org_subscription_pool.PERSONNEL,
            "mode_chosen": regle is not None,
            "pool_size": sum(p["usable_now"] for p in preteurs),
            "lenders": preteurs}
    ligne = org_subscription_limits.get_limite(org_id, famille)
    if not ligne:
        return {"org_id": org_id, "family": famille,
                "limit_pct": _abonnement.DEFAUT_LIMITE_PCT, "default": True,
                "updated_at": None, "updated_by": None, **pool, **repli}
    quand = ligne.get("updated_at")
    return {"org_id": org_id, "family": famille, "limit_pct": ligne["limite_pct"],
            "default": False,
            "updated_at": quand.isoformat() if hasattr(quand, "isoformat") else quand,
            "updated_by": ligne.get("updated_by"), **pool, **repli}


# `def`, pas `async def` : I/O bloquante (psycopg), servie en threadpool
# (`docs/event-loop-perf.md`).
def _get_plafond(ctx: ResolvedCtx, inp: GetOrgPlafondInput) -> dict:
    _exiger(inp.org_id, inp.family)
    return _servi(inp.org_id, inp.family)


def _set_plafond(ctx: ResolvedCtx, inp: SetOrgPlafondInput) -> dict:
    _exiger(inp.org_id, inp.family)
    _abonnement.exiger_limite_valide(inp.limit_pct)
    if inp.limit_pct is None:
        org_subscription_limits.retirer_limite(inp.org_id, inp.family)
    else:
        org_subscription_limits.poser_limite(inp.org_id, inp.family, inp.limit_pct,
                                             ctx.sub)
    return _servi(inp.org_id, inp.family)


def _set_mode(ctx: ResolvedCtx, inp: SetOrgModeInput) -> dict:
    """Le MODE de l'org. Il vaut pour le travail SUIVANT : un run en cours finit sur
    l'abonnement qui l'a pris. Passer en `pool` sans prêteur est permis (les membres
    prêtent ensuite) — `pool_size` le dit, et la pose d'un agent le refuse tant qu'il
    est nul."""
    _exiger(inp.org_id, inp.family)
    org_subscription_pool.poser_mode(inp.org_id, inp.family, inp.mode, ctx.sub)
    return _servi(inp.org_id, inp.family)


def _set_repli(ctx: ResolvedCtx, inp: SetOrgRepliInput) -> dict:
    """Le REPLI vers la clé API de l'org quand le forfait est épuisé (OTO-130).
    FERMÉ par défaut (décision du 28/09/2026) : une clé déposée l'a été pour des
    agents API, et y déplacer des travaux d'abonnement se CHOISIT. Ouvert, le repli
    exige en plus que l'org ait déposé sa propre clé. Vaut pour le travail SUIVANT."""
    _exiger(inp.org_id, inp.family)
    org_subscription_pool.set_repli_api(inp.org_id, inp.family, inp.api_fallback,
                                        ctx.sub)
    return _servi(inp.org_id, inp.family)


_ERREURS = (
    DeclaredError(404, "unknown_org", "l'org n'existe pas"),
    DeclaredError(400, "unknown_family", "une famille qui n'est pas servie par abonnement"),
)

CAPABILITIES += [
    Capability(
        key="org.model_subscriptions.get", handler=_get_plafond, Input=GetOrgPlafondInput,
        authz=ORG_MEMBER_OF("org_id"), Output=OrgPlafond, errors=_ERREURS,
        description=("Read the org's consumption cap on its members' personal model "
                     "subscriptions (e.g. `claude_subscription`): the max share, in %, "
                     "of each member's provider account TOTAL usage (5-hour and 7-day "
                     "windows) the org's jobs may reach before the next ones wait for "
                     "the reset. `default: true` = not set, platform default (80). A "
                     "member may set their own cap (1..95), which replaces the org's for them. "
                     "Also returns the org's `mode` (`personnel` | `pool`), its "
                     "`pool_size` (members lending a usable subscription) and "
                     "`lenders` (who lends, and whether each is usable right now — "
                     "never their tier, state or usage)."),
        rest=RestBinding("GET", _PATH, _ID),
    ),
    Capability(
        key="org.model_subscriptions.set", handler=_set_plafond, Input=SetOrgPlafondInput,
        authz=ORG_ADMIN_OF("org_id"), Output=OrgPlafond,
        errors=_ERREURS + (DeclaredError(400, "invalid_limit", "`limit_pct` hors de 1..100"),),
        description=("Set the org's consumption cap on its members' personal model "
                     "subscriptions: `limit_pct` 1..100 (% of each member's provider "
                     "account TOTAL usage, 5-hour and 7-day windows alike), or `null` to "
                     "go back to the platform default (80). Applies from the next job "
                     "report; a running job is never cut. Org admin."),
        rest=RestBinding("PUT", _PATH, _ID),
    ),
    Capability(
        key="org.model_subscriptions.set_mode", handler=_set_mode, Input=SetOrgModeInput,
        authz=ORG_ADMIN_OF("org_id"), Output=OrgPlafond, errors=_ERREURS,
        description=("Set how the org's jobs on a personal model subscription family "
                     "(e.g. `claude_subscription`) are paid for. `personnel` (default): "
                     "each job runs on the subscription of the member who asked for it; "
                     "fleets are refused. `pool`: jobs — fleets included — run on the "
                     "subscription of a member who explicitly LENT theirs to this org, "
                     "the least recently used free one first; with no lender available "
                     "they wait, none fail. Each lender's own cap applies to their "
                     "account (the lower of the org's cap and theirs). Applies from the "
                     "next job; a running job is never cut. Org admin."),
        rest=RestBinding("PUT", _PATH_MODE, _ID),
    ),
    Capability(
        key="org.model_subscriptions.set_api_fallback", handler=_set_repli,
        Input=SetOrgRepliInput, authz=ORG_ADMIN_OF("org_id"), Output=OrgPlafond,
        errors=_ERREURS,
        description=("Allow or refuse, for this org, that a job whose model subscription "
                     "is EXHAUSTED — the requester's own, or every lender of the org's "
                     "pool — replays on the org's OWN deposited API key at the same tier "
                     "instead of waiting for the plan to reset. The default depends on the instance; the org can turn it on or off. A pause "
                     "set by another org's tighter cap never triggers it; one replayed "
                     "run at a time per subscription, capped in tokens. It never spends "
                     "another party's key: without a key deposited BY THIS ORG the job "
                     "waits either way. Applies from the next job; a running job is never "
                     "cut. Org admin."),
        rest=RestBinding("PUT", _PATH_REPLI, _ID),
    ),
]
