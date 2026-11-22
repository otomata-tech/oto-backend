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

Le REPLI d'un forfait épuisé, sur la même ressource et toujours réglé seul : vers la clé
API déposée par l'org (`PUT …/{family}/api-fallback`, la porte), et — sur une instance
qui le permet — vers la clé de PLATEFORME pour une org sans clé
(`PUT …/{family}/platform-key-fallback`, 19/11/2026, `docs/repli-cle-plateforme.md`) ;
et QUAND, au plafond ou aussi déconnecté (`PUT …/{family}/fallback-when`, 22/11/2026).

Lecture = membre ; écriture = org_admin. Une déclaration → deux surfaces : REST
`/api/orgs/{id}/model-subscriptions/{family}` ici, MCP par la console
`oto_org_settings domain=model_subscriptions` (`org_console.py`).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from ... import org_store, roles
from ...db import org_subscription_limits, org_subscription_pool, user_subscriptions
from ...db import runner_triggers as db_runner_triggers
from .. import _abonnement
from .._authz import ORG_ADMIN_OF, ORG_MEMBER_OF
from .._types import (AuthzDenied, Capability, DeclaredError, ResolvedCtx,
                      RestBinding)
from ..registry import CAPABILITIES

_ID = {"id": "org_id"}
_PATH = "/api/orgs/{id}/model-subscriptions/{family}"
_PATH_MODE = "/api/orgs/{id}/model-subscriptions/{family}/mode"
_PATH_REPLI = "/api/orgs/{id}/model-subscriptions/{family}/api-fallback"
_PATH_REPLI_PLATEFORME = "/api/orgs/{id}/model-subscriptions/{family}/platform-key-fallback"
_PATH_REPLI_QUAND = "/api/orgs/{id}/model-subscriptions/{family}/fallback-when"
#: `repli_quand` (la base) ↔ `fallback_when` (les surfaces) ; une ligne d'avant : `limit`.
_QUAND_SERVI = {org_subscription_pool.REPLI_AU_PLAFOND: "limit",
                org_subscription_pool.REPLI_AUSSI_DECONNECTE: "limit_or_signed_out"}
_QUAND_RECU = {v: k for k, v in _QUAND_SERVI.items()}


class Preteur(BaseModel):
    """Un membre qui prête son abonnement au pool de l'org — et RIEN de plus.

    ⚠️ Ni palier, ni état de connexion, ni échéance, ni usage : un abonnement
    appartient à qui le paie (`capabilities/me_abonnements.py`), et prêter partage sa
    CAPACITÉ, pas son état. « Au plafond jusqu'à … » dirait à toute l'org l'usage
    TOTAL d'un compte, usage personnel compris. Même forme pour un admin d'org
    (décision d'Alexis, 08/10/2026), à UNE exception près (décision d'Alexis,
    29/10/2026) : l'org_admin SEUL lit `limit_pct`, le plafond effectif du prêteur —
    un RÉGLAGE qu'il a choisi (ou celui de l'org), jamais son usage. Il dit à qui
    gouverne le pool quelle part de chaque forfait les agents peuvent prendre ; un
    membre ne le lit pas."""
    sub: str
    usable_now: bool = Field(
        description=("Whether this subscription is usable right now (connected, or at a "
                     "cap that has already lifted). It does not promise the job queue "
                     "will pick it: one job in flight per lender, and only while the "
                     "org runs in `pool` mode."))
    limit_pct: Optional[int] = Field(
        default=None,
        description=("Org admins only (absent for members): the lender's effective cap "
                     "in % of their account's TOTAL usage — their own if they set one, "
                     "else the org's. A setting, never usage."))


class AgentEteint(BaseModel):
    """Un agent ÉTEINT parce que l'org a quitté le pool exclusif : son dernier
    changement n'était pas de son propriétaire, et il tournerait désormais sur SON
    forfait (`db.eteindre_les_retouches_d_autrui`)."""
    id: int
    owner: str
    label: Optional[str] = None
    last_changed_by: Optional[str] = Field(
        default=None,
        description="Who last changed it before it was switched off; null = not recorded.")


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
                     "(`usable_now`) — never their tier, state, reset time or usage. "
                     "An org admin also reads each one's effective cap (`limit_pct`)."))
    disabled_agents: Optional[list[AgentEteint]] = Field(
        default=None,
        description=("`set_mode` leaving `pool` only: the org's agents on this family "
                     "that were switched OFF because their last change was not made by "
                     "their owner — they would now run on the owner's own subscription "
                     "with someone else's instructions. Their owner reviews and "
                     "re-enables them. `[]` = none."))
    api_fallback: bool = Field(
        default=False,
        description=("`true`: a job whose subscription (the requester's, or the whole "
                     "pool) is EXHAUSTED — or signed out, per `fallback_when` — replays "
                     "on this org's own deposited API key, same tier, instead of waiting. "
                     "`false`: those jobs wait. Without a setting of the org, the "
                     "instance's default applies. It never spends anyone else's key — "
                     "the platform key only through `platform_key_fallback`."))
    platform_key_fallback: bool = Field(
        default=False,
        description=("The org's own choice, set by an org admin: `true` = when "
                     "`api_fallback` is on and the org has deposited NO API key of its "
                     "own, an exhausted job may replay on the instance's shared "
                     "(platform) key instead, each model call billed to the org's "
                     "credits — but only if that key is granted to the org. In force "
                     "only while `platform_key_fallback_permitted` is `true`; an org "
                     "with its own key always replays on its own key."))
    platform_key_fallback_permitted: bool = Field(
        default=False,
        description=("Whether this instance allows the platform-key fallback at all. "
                     "`false`: it cannot be turned on, and an earlier `true` is "
                     "ignored."))
    platform_key_fallback_set_by: Optional[str] = Field(
        default=None, description="Who last set `platform_key_fallback`; null = never set.")
    platform_key_fallback_set_at: Optional[str] = Field(
        default=None, description="When `platform_key_fallback` was last set.")
    fallback_when: Literal["limit", "limit_or_signed_out"] = Field(
        default="limit",
        description=("WHEN a job falls back, set by an org admin: `limit` (default) "
                     "= only at the subscription's cap; `limit_or_signed_out` = also "
                     "when it is signed out or needs a new login (see "
                     "`set_fallback_when`). It neither turns the fallback on "
                     "(`api_fallback`) nor picks who pays."))
    fallback_when_set_by: Optional[str] = Field(
        default=None, description="Who last set `fallback_when`; null = never set.")
    fallback_when_set_at: Optional[str] = Field(
        default=None, description="When `fallback_when` was last set.")


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
                     "key: with no key deposited by this org, the jobs wait — unless "
                     "the org also turned on `platform_key_fallback`. `false`: those "
                     "jobs wait for the reset, whatever `platform_key_fallback` says. "
                     "Unset, the instance's "
                     "default applies."))


class SetOrgRepliQuandInput(BaseModel):
    org_id: int
    family: str
    when: Literal["limit", "limit_or_signed_out"] = Field(
        description="`limit` (default): only at the cap; `limit_or_signed_out`: also signed out.")


class SetOrgRepliPlateformeInput(BaseModel):
    org_id: int
    family: str
    platform_key_fallback: bool = Field(
        description=("`true`: when the subscription is exhausted, `api_fallback` is on "
                     "and the org has deposited no API key of its own, the org's jobs "
                     "replay on the instance's shared (platform) key, same tier, each "
                     "model call billed to the org's credits — only if that key is "
                     "granted to the org, at most one such run at a time per "
                     "subscription, each capped in tokens. Refused "
                     "(`platform_key_fallback_not_permitted`) on an instance that does "
                     "not allow it. `false`: always accepted; those jobs wait as "
                     "before."))


def _exiger(org_id: int, famille: str) -> None:
    if not org_store.get_org(org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{org_id} inconnue.")
    if not _abonnement.est_abonnement(famille):
        raise AuthzDenied(
            400, "unknown_family",
            f"`{famille}` n'est pas une famille servie par abonnement "
            f"({', '.join(sorted(_abonnement.FAMILLES))}).")


def _date(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _repli_plateforme_servi(org_id: int, famille: str) -> dict:
    """Le réglage du repli sur la clé de PLATEFORME, tel que l'org l'a posé, et si
    l'instance le permet (19/11/2026). Le réglage est rendu TEL QU'IL EST, même quand
    l'instance ne le permet plus : l'écran dit les deux, plutôt qu'un « fermé » qui
    cacherait un réglage qu'il faudrait refermer avant de rouvrir l'instance. Et QUAND
    l'org se replie (`fallback_when`, 22/11/2026), de la même ligne."""
    ligne = org_subscription_pool.get_mode(org_id, famille) or {}
    return {"platform_key_fallback": bool(ligne.get("repli_cle_plateforme")),
            "platform_key_fallback_permitted":
                org_subscription_pool.repli_cle_plateforme_permis(),
            "platform_key_fallback_set_by": ligne.get("repli_cle_plateforme_by"),
            "platform_key_fallback_set_at": _date(ligne.get("repli_cle_plateforme_at")),
            "fallback_when": _QUAND_SERVI[ligne.get("repli_quand")
                                          or org_subscription_pool.REPLI_AU_PLAFOND],
            "fallback_when_set_by": ligne.get("repli_quand_by"),
            "fallback_when_set_at": _date(ligne.get("repli_quand_at"))}


def _servi(org_id: int, famille: str, admin: bool) -> dict:
    """`admin` : le lecteur gouverne l'org — lui seul lit le plafond de chaque prêteur."""
    repli = {"api_fallback": org_subscription_pool.repli_api_actif(org_id, famille),
             **_repli_plateforme_servi(org_id, famille)}
    ligne = org_subscription_limits.get_limite(org_id, famille)
    de_l_org = ligne["limite_pct"] if ligne else _abonnement.DEFAUT_LIMITE_PCT
    preteurs = []
    for p in org_subscription_pool.preteurs(org_id, famille):
        entree = {"sub": p["sub"], "usable_now": bool(p["servable"])}
        if admin:
            # LA règle est `_abonnement.seuil` : le perso s'il est posé (borné), sinon
            # celui de l'org. Relue ici sur la ligne déjà lue, sans requête par prêteur.
            perso = p.get("limite_pct")
            entree["limit_pct"] = (min(perso, user_subscriptions.LIMITE_PERSO_MAX)
                                   if perso is not None else de_l_org)
        preteurs.append(entree)
    # `pool_size` se DÉDUIT de `lenders` : une seule lecture, aucun écart possible
    # entre le compte et la liste montrés sur le même écran.
    # Sans mode CHOISI, l'écran dit `personnel` comme avant, et `mode_chosen: false` :
    # le pool complète (`org_subscription_pool.sql_par_le_pool`).
    regle = org_subscription_pool.mode_regle(org_id, famille)
    pool = {"mode": regle or org_subscription_pool.PERSONNEL,
            "mode_chosen": regle is not None,
            "pool_size": sum(p["usable_now"] for p in preteurs),
            "lenders": preteurs}
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
    return _servi(inp.org_id, inp.family, roles.is_org_admin(ctx.sub, inp.org_id))


def _set_plafond(ctx: ResolvedCtx, inp: SetOrgPlafondInput) -> dict:
    _exiger(inp.org_id, inp.family)
    _abonnement.exiger_limite_valide(inp.limit_pct)
    if inp.limit_pct is None:
        org_subscription_limits.retirer_limite(inp.org_id, inp.family)
    else:
        org_subscription_limits.poser_limite(inp.org_id, inp.family, inp.limit_pct,
                                             ctx.sub)
    return _servi(inp.org_id, inp.family, True)


def _set_mode(ctx: ResolvedCtx, inp: SetOrgModeInput) -> dict:
    """Le MODE de l'org. Il vaut pour le travail SUIVANT : un run en cours finit sur
    l'abonnement qui l'a pris. Passer en `pool` sans prêteur est permis (les membres
    prêtent ensuite) — `pool_size` le dit, et la pose d'un agent le refuse tant qu'il
    est nul.

    ⚠️ QUITTER le pool exclusif : en pool, un partage collectif du propriétaire suffit à
    modifier son agent (`db._forfait_prete_par_le_pool`) ; hors pool, l'agent tourne de
    nouveau sur SON forfait. Ceux qu'un autre a changés en dernier sont ÉTEINTS et
    listés (`disabled_agents`) — jamais refusé : l'admin n'est pas bloqué, et un
    propriétaire parti ne fait pas d'impasse. Deux passes : avant le changement de mode
    (leurs travaux en attente sont périmés avant qu'un seul ne puisse partir sur le
    forfait du propriétaire), puis après (ce qu'un éditeur aurait rallumé ou retouché
    entre les deux — le mode posé, ses droits sont refermés)."""
    _exiger(inp.org_id, inp.family)
    quitte_le_pool = (inp.mode != org_subscription_pool.POOL
                      and org_subscription_pool.en_pool(inp.org_id, inp.family))
    eteints = []
    if quitte_le_pool:
        eteints += db_runner_triggers.eteindre_les_retouches_d_autrui(
            inp.org_id, inp.family, ctx.sub)
    org_subscription_pool.poser_mode(inp.org_id, inp.family, inp.mode, ctx.sub)
    if not quitte_le_pool:
        return _servi(inp.org_id, inp.family, True)
    eteints += db_runner_triggers.eteindre_les_retouches_d_autrui(
        inp.org_id, inp.family, ctx.sub)
    return {**_servi(inp.org_id, inp.family, True), "disabled_agents": eteints}


def _set_repli(ctx: ResolvedCtx, inp: SetOrgRepliInput) -> dict:
    """Le REPLI vers la clé API de l'org quand le forfait est épuisé (OTO-130).
    FERMÉ par défaut (décision du 28/09/2026) : une clé déposée l'a été pour des
    agents API, et y déplacer des travaux d'abonnement se CHOISIT. Ouvert, le repli
    exige en plus que l'org ait déposé sa propre clé. Vaut pour le travail SUIVANT."""
    _exiger(inp.org_id, inp.family)
    org_subscription_pool.set_repli_api(inp.org_id, inp.family, inp.api_fallback,
                                        ctx.sub)
    return _servi(inp.org_id, inp.family, True)


def _set_repli_plateforme(ctx: ResolvedCtx, inp: SetOrgRepliPlateformeInput) -> dict:
    """Le repli d'un abonnement épuisé sur la clé de PLATEFORME (19/11/2026) : le
    second moyen, explicite, pour une org de dire « je paie » quand elle n'a pas déposé
    de clé (`docs/repli-cle-plateforme.md`). Il choisit le PAYEUR, pas le repli :
    `api_fallback` reste la porte, et fermée rien ne replie.

    ⚠️ L'OUVRIR exige une instance qui le permet (`OTO_REPLI_CLE_PLATEFORME_PERMIS`) :
    ailleurs, refus nommé — l'org lirait « je paie » là où personne ne facture. Le
    REFERMER passe toujours : un réglage resté ouvert sur une instance qui ne le permet
    plus doit pouvoir se refermer. Vaut pour le travail SUIVANT."""
    _exiger(inp.org_id, inp.family)
    if inp.platform_key_fallback and not org_subscription_pool.repli_cle_plateforme_permis():
        raise AuthzDenied(
            409, "platform_key_fallback_not_permitted",
            "Cette instance ne permet pas le repli sur la clé de plateforme : un travail "
            "au plafond sans clé d'org attend la réinitialisation de son forfait, ou "
            "rejoue sur la clé que l'organisation dépose (Connecteurs).")
    org_subscription_pool.set_repli_cle_plateforme(inp.org_id, inp.family,
                                                   inp.platform_key_fallback, ctx.sub)
    return _servi(inp.org_id, inp.family, True)


def _set_repli_quand(ctx: ResolvedCtx, inp: SetOrgRepliQuandInput) -> dict:
    """QUAND l'org se replie (22/11/2026, `docs/repli-cle-plateforme.md` §Quand se
    replier) : au plafond seulement (`limit`, la règle d'avant), ou aussi déconnecté
    (`limit_or_signed_out`). Ni la porte (`api_fallback`) ni le payeur, donc aucune
    permission d'instance : chaque payeur garde ses portes. Vaut au travail SUIVANT."""
    _exiger(inp.org_id, inp.family)
    org_subscription_pool.set_repli_quand(inp.org_id, inp.family, _QUAND_RECU[inp.when],
                                          ctx.sub)
    return _servi(inp.org_id, inp.family, True)


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
                     "never their tier, state or usage; an org admin also reads each "
                     "lender's effective cap, `limit_pct`)."),
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
                     "next job; a running job is never cut. Leaving `pool` switches OFF "
                     "the org's agents on that family whose last change was not made by "
                     "their owner (they would run on the owner's plan with someone "
                     "else's instructions) and lists them in `disabled_agents`; their "
                     "owner re-enables them. Org admin."),
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
                     "waits, unless the org also turned on `platform_key_fallback` "
                     "(`set_platform_key_fallback`). Applies from the next job; a running "
                     "job is never "
                     "cut. Org admin."),
        rest=RestBinding("PUT", _PATH_REPLI, _ID),
    ),
    Capability(
        key="org.model_subscriptions.set_platform_key_fallback",
        handler=_set_repli_plateforme, Input=SetOrgRepliPlateformeInput,
        authz=ORG_ADMIN_OF("org_id"), Output=OrgPlafond,
        errors=_ERREURS + (DeclaredError(
            409, "platform_key_fallback_not_permitted",
            "l'instance ne permet pas le repli sur la clé de plateforme"),),
        description=("Allow or refuse, for this org, that a job whose model subscription "
                     "is EXHAUSTED replays on the instance's shared (platform) key when "
                     "the org has deposited no API key of its own — the second explicit "
                     "way for the org to say it pays, each model call billed to its "
                     "credits. It chooses the payer, not the fallback: `api_fallback` "
                     "must be on as well, and an org with its own key always replays "
                     "on it. Only if the shared key is granted to the org, the replayed "
                     "model is priced in the credit scale and the org itself (not each "
                     "person) pays its credits — otherwise the job waits; same cases "
                     "as `api_fallback` (a cap with a future reset — and a signed-out "
                     "subscription if `fallback_when` says so — never an empty pool), "
                     "one replayed run at a time per subscription, capped in "
                     "tokens. Turning it on is refused "
                     "(`platform_key_fallback_not_permitted`) on an instance that does "
                     "not allow it; turning it off always works. Applies from the next "
                     "job; a running job is never cut. Org admin."),
        rest=RestBinding("PUT", _PATH_REPLI_PLATEFORME, _ID),
    ),
    Capability(
        key="org.model_subscriptions.set_fallback_when", handler=_set_repli_quand,
        Input=SetOrgRepliQuandInput, authz=ORG_ADMIN_OF("org_id"), Output=OrgPlafond,
        errors=_ERREURS,
        description=("Choose WHEN this org's subscription jobs fall back: `limit` "
                     "(default) — only at the cap; `limit_or_signed_out` — also when "
                     "signed out or needing a new login for 10+ minutes: the "
                     "requester's own subscription opened here, or every lender of the "
                     "pool (each capped or signed out; with no mode set, also the pool "
                     "of a requester with no subscription here). Neither turns the "
                     "fallback on (`api_fallback`) nor picks who pays; same bounds. "
                     "Still waiting either way: a pool with no lender, a requester with "
                     "no subscription outside that pool case, a model the org switched "
                     "off. Reconnected, new jobs run on it again; replayed ones finish "
                     "on the fallback key. Next job on. Org admin."),
        rest=RestBinding("PUT", _PATH_REPLI_QUAND, _ID),
    ),
]
