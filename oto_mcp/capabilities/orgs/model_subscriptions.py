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

Ce qui fait tourner les agents de l'org (22/11/2026) : l'org_admin SEUL lit, sur la même
ressource, l'état de chaque forfait (`state`) et les travaux qui l'attendent (`plans`,
`pool_waiting_jobs`) — jugés par les clauses mêmes de la réservation
(`org_subscription_pool.ETAT_DU_PRETEUR`, `forfaits_de_l_org`). Un membre lit la forme
d'avant, `plans: []` en plus.

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


#: Ce que l'écran de l'admin lit d'un forfait (`org_subscription_pool.ETATS`).
EtatForfait = Literal["ok", "signed_out", "at_limit", "not_open", "none"]

_DESCRIPTION_ETAT = (
    "Org admins only (absent for members). Why this plan does or does not run the org's "
    "jobs right now, judged by the job queue's own rule: `ok` (it would be picked now), "
    "`signed_out` (disconnected, its session lost, or never set up: its owner "
    "reconnects), `at_limit` (paused at a cap until `reset_at` — possibly a tighter cap "
    "of another org it serves: the pause holds every org's jobs), `not_open` (personal "
    "plan not opened in this org: its owner adds it to `used_in`), `none` (an agent "
    "owner with no plan of this family at all).")


class Preteur(BaseModel):
    """Un membre qui prête son abonnement au pool de l'org.

    ⚠️ Un abonnement appartient à qui le paie (`capabilities/me_abonnements.py`), et
    prêter partage sa CAPACITÉ, pas son état. « Au plafond jusqu'à … » dirait l'usage
    TOTAL d'un compte, usage personnel compris. Un MEMBRE ne lit donc que `sub` et
    `usable_now` — ni palier, ni état, ni échéance, ni usage, ni réglage. L'admin d'org
    lit en plus, et lui seul :

    - `limit_pct` (29/10/2026), le plafond effectif du prêteur — un RÉGLAGE qu'il a
      choisi (ou celui de l'org), jamais son usage : la part de chaque forfait que les
      agents peuvent prendre ;
    - `state`, `last_ok_at` et `reset_at` (22/11/2026) : POURQUOI le forfait ne sert pas.
      Sans eux, `usable_now: false` confondait un compte à reconnecter (son porteur doit
      agir) et un compte au plafond (il suffit d'attendre), et l'admin qui gouverne le
      pool ne savait pas qui prévenir. C'est une SECONDE exception à « même forme pour un
      admin » (08/10/2026), délibérée et bornée : un état et deux instants, jamais le
      palier (`plan`), jamais l'usage mesuré (`limit_utilisation`), jamais la fenêtre.

    `state` est lu dans la même ligne SQL que `usable_now`
    (`org_subscription_pool.ETAT_DU_PRETEUR`) : `state == "ok"` ⇔ `usable_now`."""
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
    state: Optional[EtatForfait] = Field(
        default=None,
        description=_DESCRIPTION_ETAT + " A lender is never `not_open` nor `none`; "
        "`ok` exactly when `usable_now`.")
    last_ok_at: Optional[str] = Field(
        default=None,
        description=("Org admins only (absent for members): when this plan last ran a "
                     "job successfully, ISO 8601; null = never recorded."))
    reset_at: Optional[str] = Field(
        default=None,
        description=("Org admins only (absent for members): when the cap lifts, ISO "
                     "8601 — set only when `state` is `at_limit`."))


class Forfait(BaseModel):
    """Un forfait qui fait tourner (ou devrait faire tourner) les agents de l'org — ce
    que l'admin lit pour savoir ce qui attend, et sur qui (`_servi`).

    Qui y figure : les prêteurs (`lent: true`, sauf en mode `personnel` réglé, où un prêt
    ne sert rien mais reste listé) et, hors mode `pool` réglé, les porteurs d'agents de la
    voie personnelle (`org_subscription_pool.forfaits_de_l_org`). Une personne qui prête
    ET porte des agents n'a qu'une ligne : c'est le même compte."""
    sub: str
    lent: bool = Field(description="Whether this member lends this plan to the org's pool.")
    state: EtatForfait = Field(description=_DESCRIPTION_ETAT.replace(
        "Org admins only (absent for members). ", ""))
    last_ok_at: Optional[str] = Field(
        default=None, description="When this plan last ran a job successfully, ISO 8601.")
    reset_at: Optional[str] = Field(
        default=None, description="When the cap lifts, ISO 8601 — only when `at_limit`.")
    waiting_jobs: int = Field(
        description=("The org's due, pending jobs of this family that the job queue would "
                     "run on THIS plan (the personal route: its owner's own jobs). Jobs "
                     "routed to the pool are counted once, in `pool_waiting_jobs`, never "
                     "on a lender: the queue takes whichever lender is free first."))


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
                     "(`usable_now`) — never their tier or usage. An org admin also "
                     "reads each one's effective cap (`limit_pct`) and why it does or "
                     "does not serve (`state`, `last_ok_at`, `reset_at`); a member reads "
                     "`sub` and `usable_now` only."))
    plans: list[Forfait] = Field(
        default_factory=list,
        description=("Org admins only (`[]` for members): every member plan that runs, "
                     "or should run, the org's agents of this family — the lenders, and "
                     "(unless the org is in `pool` mode) the owners of its ENABLED agents "
                     "on this family plus anyone with a job waiting on their own plan — "
                     "each with its `state` and the jobs waiting on it. One entry per "
                     "member, sorted by `sub`."))
    pool_waiting_jobs: Optional[int] = Field(
        default=None,
        description=("Org admins only (absent for members): the org's due, pending jobs "
                     "of this family that the job queue routes to the POOL — counted once "
                     "here, never per lender."))
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


def _iso_utc(v) -> Optional[str]:
    """Un instant lu en base en ISO 8601 AVEC son fuseau (UTC). ⚠️ La fabrique de lignes
    du pool rend un texte « AAAA-MM-JJ HH:MM:SS » sans fuseau (`db._conn`), qu'un
    navigateur lirait à l'heure LOCALE : l'écran daterait d'une heure de trop (ou de
    moins) le dernier succès d'un forfait."""
    quand = _abonnement.echeance_utc(v)
    return quand.isoformat() if quand else None


def _l_etat(ligne: dict) -> dict:
    """L'état d'un forfait tel que l'admin le lit : l'échéance seulement au plafond."""
    au_plafond = ligne["etat"] == org_subscription_pool.ETAT_AU_PLAFOND
    return {"state": ligne["etat"], "last_ok_at": _iso_utc(ligne.get("last_ok_at")),
            "reset_at": _iso_utc(ligne.get("limit_reset_at")) if au_plafond else None}


def _forfaits(org_id: int, famille: str, regle: Optional[str], lus: list[dict]) -> dict:
    """`plans` et `pool_waiting_jobs` pour l'admin : les prêteurs déjà lus (`lus`) et les
    porteurs de la voie personnelle (`forfaits_de_l_org`, UNE lecture de plus).

    - Mode `pool` réglé : les porteurs n'y sont pas — leur forfait ne sert pas ici.
    - Une personne qui prête ET porte des agents : une seule ligne, `lent: true`. Son
      état est celui de PRÊTEUR (le pool la sert, et ses propres travaux passent par le
      pool quand son abonnement n'est pas ouvert ici) — sauf en mode `personnel` réglé,
      où un prêt ne sert rien : l'état est alors celui de sa voie personnelle.
    - Un prêteur qui ne porte rien attend `0` travail : ceux du pool sont comptés une
      fois, dans `pool_waiting_jobs`."""
    lu = org_subscription_pool.forfaits_de_l_org(org_id, famille)
    par_sub = {p["sub"]: {"sub": p["sub"], "lent": True, **_l_etat(p), "waiting_jobs": 0}
               for p in lus}
    if regle != org_subscription_pool.POOL:
        for o in lu["porteurs"]:
            entree = par_sub.get(o["sub"])
            if entree is None:
                par_sub[o["sub"]] = {"sub": o["sub"], "lent": False, **_l_etat(o),
                                     "waiting_jobs": o["en_attente"]}
                continue
            entree["waiting_jobs"] = o["en_attente"]
            if regle == org_subscription_pool.PERSONNEL:
                entree.update(_l_etat(o))
    return {"plans": [par_sub[s] for s in sorted(par_sub)],
            "pool_waiting_jobs": lu["pool_en_attente"]}


def _servi(org_id: int, famille: str, admin: bool) -> dict:
    """`admin` : le lecteur gouverne l'org — lui seul lit le plafond et l'état de chaque
    prêteur, et `plans` (ce qui fait tourner les agents de l'org, et ce qui attend).

    ⚠️ Un MEMBRE lit la forme d'avant, à la clé près : `lenders` à `sub` + `usable_now`,
    et `plans: []` — rien de neuf sur le forfait de personne."""
    repli = {"api_fallback": org_subscription_pool.repli_api_actif(org_id, famille)}
    ligne = org_subscription_limits.get_limite(org_id, famille)
    de_l_org = ligne["limite_pct"] if ligne else _abonnement.DEFAUT_LIMITE_PCT
    lus = org_subscription_pool.preteurs(org_id, famille)
    preteurs = []
    for p in lus:
        entree = {"sub": p["sub"], "usable_now": bool(p["servable"])}
        if admin:
            # LA règle est `_abonnement.seuil` : le perso s'il est posé (borné), sinon
            # celui de l'org. Relue ici sur la ligne déjà lue, sans requête par prêteur.
            perso = p.get("limite_pct")
            entree["limit_pct"] = (min(perso, user_subscriptions.LIMITE_PERSO_MAX)
                                   if perso is not None else de_l_org)
            entree.update(_l_etat(p))
        preteurs.append(entree)
    # `pool_size` se DÉDUIT de `lenders` : une seule lecture, aucun écart possible
    # entre le compte et la liste montrés sur le même écran.
    # Sans mode CHOISI, l'écran dit `personnel` comme avant, et `mode_chosen: false` :
    # le pool complète (`org_subscription_pool.sql_par_le_pool`).
    regle = org_subscription_pool.mode_regle(org_id, famille)
    pool = {"mode": regle or org_subscription_pool.PERSONNEL,
            "mode_chosen": regle is not None,
            "pool_size": sum(p["usable_now"] for p in preteurs),
            "lenders": preteurs,
            **(_forfaits(org_id, famille, regle, lus) if admin else {"plans": []})}
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
                     "never their tier or usage; an org admin also reads each "
                     "lender's effective cap, `limit_pct`, and its `state`). An org "
                     "admin also reads `plans` — every member plan that runs the org's "
                     "agents of this family, with its `state` (`ok`, `signed_out`, "
                     "`at_limit`, `not_open`, `none`), `last_ok_at`, `reset_at` and the "
                     "jobs waiting on it — and `pool_waiting_jobs`; a member reads "
                     "`plans: []`."),
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
                     "waits either way. Applies from the next job; a running job is never "
                     "cut. Org admin."),
        rest=RestBinding("PUT", _PATH_REPLI, _ID),
    ),
]
