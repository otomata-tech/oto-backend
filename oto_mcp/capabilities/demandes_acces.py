"""Capacités « demandes d'accès » : une personne bloquée DEMANDE, qui peut le lui accorder
l'accorde d'un geste (`docs/demandes-d-acces.md`).

`POST|GET /api/me/access-requests`, puis `POST /api/me/access-requests/{request_id}/
approve|decline|cancel|remind`. Face REST seule : c'est l'écran « Demandes » qui les
sert.

**La demande n'accorde rien.** Approuver REJOUE la capacité qui accorde déjà ce geste —
partage d'une ressource (`resources.govern`), activation d'un connecteur pour l'org,
outils d'un agent (`runner.triggers`), entrée dans une équipe — SOUS L'IDENTITÉ de qui
approuve, sa règle d'autz et ses gardes comprises. Aucun droit n'est réécrit ici : qui
ne pourrait pas accorder à la main ne peut pas approuver, et le refus est celui du
geste lui-même.

**Qui décide** = qui peut déjà accorder : qui GOUVERNE la ressource (propriétaire,
gérant, escalade `roles.py`), un admin de l'org pour un connecteur, le propriétaire ou
un éditeur de l'agent, le chef de l'équipe. Pas seulement les admins.

**Périmètre : l'org de l'appel.** Une demande naît dans l'org active du demandeur, sur
une cible qui VIT dans cette org (sinon 404, même refus qu'une cible inconnue), et ne
se lit ni ne se décide que depuis elle.

Une demande déjà satisfaite est refusée (409 `already_granted`) : on ne demande pas ce
qu'on a. Redemander la même chose rend la demande en attente (`created: false`).
"""
from __future__ import annotations

import dataclasses
import inspect
from typing import Callable, Literal, Optional

from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from .. import db, group_store, org_store, ownership, providers, roles
from ..connectors import activation as connector_activation
from . import _acces_agent
from ._authz import ORG_MEMBER
from ._types import AuthzDenied, Capability, DeclaredError, RawCtx, ResolvedCtx, RestBinding
from .registry import CAPABILITIES, by_key
from .resources_contract import KIND_OF

KINDS = ("resource_access", "connector_activation", "agent_access", "agent_tools", "team_join")
NOTE_MAX = 1000
OUTILS_MAX = 50
#: Demandes VIVANTES d'une personne dans une org, au plus : au-delà, une demande neuve
#: est refusée (409 `too_many_pending_requests`) jusqu'à ce qu'une se décide, s'annule
#: ou échoie. Borne l'écriture d'un seul compte, et donc ce que `box=review` doit juger.
EN_ATTENTE_MAX = 20
#: Demandes vivantes d'une org que `box=review` juge, au plus — les plus RÉCENTES
#: (`db.demandes_acces.en_attente`) ; au-delà, `truncated` le dit.
REVUE_MAX = 200
_ROLES = ("viewer", "editor", "manager")

# --- Entrées / sorties --------------------------------------------------------


def _note(v: Optional[str]) -> Optional[str]:
    v = (v or "").strip()
    if len(v) > NOTE_MAX:
        raise ValueError(f"`note` is longer than {NOTE_MAX} characters")
    return v or None


class CreateRequestInput(BaseModel):
    kind: Literal["resource_access", "connector_activation", "agent_access", "agent_tools",
                  "team_join"]
    resource_type: Optional[Literal["project", "datastore_namespace", "procedure", "doc"]] = Field(
        default=None, description="`resource_access`: the family of the resource.")
    resource_id: Optional[str] = Field(default=None, description="`resource_access`: its id.")
    role: Optional[Literal["viewer", "editor", "manager"]] = Field(default=None, description=(
        "`resource_access`: the role asked for (default `viewer`). `agent_access`: "
        "`viewer` or `editor` (default `editor`)."))
    connector: Optional[str] = Field(default=None, description=(
        "`connector_activation`: the connector to turn on for the org."))
    trigger_id: Optional[int] = Field(default=None, description=(
        "`agent_access` / `agent_tools`: the agent."))
    tools: Optional[list[str]] = Field(default=None, description=(
        f"`agent_tools`: the tools the agent should be allowed (at most {OUTILS_MAX})."))
    group_id: Optional[int] = Field(default=None, description="`team_join`: the team.")
    note: Optional[str] = Field(default=None, description=(
        f"Why, for the person who decides ({NOTE_MAX} characters at most)."))

    @field_validator("note", mode="after")
    @classmethod
    def _note_bornee(cls, v: Optional[str]) -> Optional[str]:
        return _note(v)


class ListRequestsInput(BaseModel):
    box: Literal["review", "sent"] = Field(default="review", description=(
        "`review`: the pending requests the caller can decide. `sent`: the caller's "
        "own requests, every outcome, newest first."))


class _UneDemande(BaseModel):
    request_id: int


class ApproveRequestInput(_UneDemande):
    role: Optional[Literal["viewer", "editor", "manager"]] = Field(default=None, description=(
        "`resource_access` / `agent_access`: grant this role instead of the one asked "
        "(an agent takes `viewer` or `editor` only)."))
    tools: Optional[list[str]] = Field(default=None, description=(
        "`agent_tools`: allow only these tools, among those asked."))
    note: Optional[str] = None

    @field_validator("note", mode="after")
    @classmethod
    def _note_bornee(cls, v: Optional[str]) -> Optional[str]:
        return _note(v)


class DeclineRequestInput(_UneDemande):
    note: Optional[str] = None

    @field_validator("note", mode="after")
    @classmethod
    def _note_bornee(cls, v: Optional[str]) -> Optional[str]:
        return _note(v)


class RequestTarget(BaseModel):
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    connector: Optional[str] = None
    trigger_id: Optional[int] = None
    group_id: Optional[int] = None


class AccessRequest(BaseModel):
    id: int
    kind: str
    status: Literal["pending", "approved", "declined", "cancelled", "expired"] = Field(
        description="A pending request past `expires_at` is served `expired`.")
    requester: str
    requester_email: Optional[str] = None
    target: RequestTarget
    target_label: Optional[str] = Field(default=None, description=(
        "The target's name as the caller may read it; absent when it is gone."))
    asked: dict = Field(description="What was asked: `role` or `tools`.")
    note: Optional[str] = None
    created_at: Optional[str] = None
    expires_at: Optional[str] = None
    reminded_at: Optional[str] = None
    decided_by: Optional[str] = None
    decided_by_email: Optional[str] = None
    decided_at: Optional[str] = None
    decision: Optional[dict] = Field(default=None, description="What was really granted.")
    decision_note: Optional[str] = None
    mine: bool = Field(description="The caller asked it.")
    can_decide: bool = Field(description="The caller may approve or decline it now.")


class CreatedRequest(BaseModel):
    request: AccessRequest
    created: bool = Field(description="`false`: the same request was already pending.")


class RequestList(BaseModel):
    box: str
    requests: list[AccessRequest]
    pending_count: int = Field(description=(
        "`review`: how many requests the caller can decide (the badge). `sent`: how "
        "many of the caller's are still pending."))
    truncated: bool = Field(default=False, description=(
        f"`review`: the organization has more than {REVUE_MAX} pending requests; only the "
        f"{REVUE_MAX} newest were judged, so `pending_count` is a lower bound."))


class Reminded(BaseModel):
    reminded: bool = Field(description=(
        "`false`: too early — one reminder per 24 hours, the first a day after asking."))


# --- Les gestes ------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class _Geste:
    #: (ctx, entrée) → (cible, clé canonique, demandé) ; refuse ce qui n'a pas lieu d'être.
    poser: Callable[[ResolvedCtx, CreateRequestInput], tuple[dict, str, dict]]
    #: (sub, demande) → peut-il l'accorder ?
    peut_decider: Callable[[str, dict], bool]
    #: (demande, accordé) → (clé de la capacité qui accorde, son entrée)
    accorder: Callable[[dict, dict], tuple[str, BaseModel]]
    #: (sub, demande) → le nom de la cible, ou None
    libelle: Callable[[str, dict], Optional[str]]


def _deja() -> AuthzDenied:
    return AuthzDenied(409, "already_granted", "You already have this.")




def _exige(champ: str, valeur):
    if valeur is None or valeur == "" or valeur == []:
        raise AuthzDenied(400, "missing_fields", f"`{champ}` is required for this kind.")
    return valeur


# resource_access — un projet, un tableau, une procédure, une page

def _org_de_la_ressource(kind: str, rid: str) -> Optional[int]:
    """L'org où vit la ressource. Une page n'a pas d'org à elle : elle vit dans celle de
    son projet (`governed_by`), même règle que sa gouvernance."""
    k = ownership.RESOURCE_KINDS.get(kind)
    if k is not None and k.governed_by is not None:
        parent = k.governed_by(rid)
        return _org_de_la_ressource(*parent) if parent else None
    return ownership.org_ou_vit(kind, rid)[0]


def _ressource_poser(ctx: ResolvedCtx, inp: CreateRequestInput):
    rtype = _exige("resource_type", inp.resource_type)
    rid = str(_exige("resource_id", inp.resource_id)).strip()
    kind = KIND_OF[rtype]
    # Une cible inconnue et une cible d'une AUTRE org se refusent pareil : la demande
    # ne dit pas ce qui existe ailleurs.
    if ownership.owner_of(kind, rid) is None or _org_de_la_ressource(kind, rid) != ctx.org_id:
        raise AuthzDenied(404, "target_not_found", "No such resource in this organization.")
    role = inp.role or "viewer"
    if role == "manager":
        if ownership.can_govern(ctx.sub, kind, rid):
            raise _deja()
    elif ownership.can_access(ctx.sub, kind, rid, "write" if role == "editor" else "read"):
        raise _deja()
    return ({"resource_type": rtype, "resource_id": rid}, f"{rtype}:{rid}", {"role": role})


def _ressource_peut(sub: str, d: dict) -> bool:
    c = d["cible"]
    return ownership.can_govern(sub, KIND_OF[c["resource_type"]], c["resource_id"])


def _ressource_accorder(d: dict, accorde: dict):
    from .resources import ResourceInput
    c = d["cible"]
    return "resources.govern", ResourceInput(
        op="share", resource_type=c["resource_type"], resource_id=c["resource_id"],
        audience="person", sub=d["requester_sub"], role=accorde["role"])


def _ressource_libelle(sub: str, d: dict) -> Optional[str]:
    """Le nom, à qui peut DÉJÀ le lire ou décider seulement : demander par id ne doit pas
    servir à lire, un à un, les noms des objets privés des collègues. Le demandeur le
    reçoit une fois l'accès accordé (l'écran d'où il a demandé le connaît déjà)."""
    from .resources import _OPS
    c = d["cible"]
    kind = KIND_OF[c["resource_type"]]
    if not str(c["resource_id"]).isdigit() or not (
            ownership.can_access(sub, kind, c["resource_id"], "read")
            or ownership.can_govern(sub, kind, c["resource_id"])):
        return None
    row = _OPS[c["resource_type"]]["get_by_id"](int(c["resource_id"]))
    if not row:
        return None
    return row.get("name") or row.get("title") or row.get("datastore") or row.get("slug")


# connector_activation — un connecteur coupé pour l'org

def _connecteur_poser(ctx: ResolvedCtx, inp: CreateRequestInput):
    name = str(_exige("connector", inp.connector)).strip()
    if name not in providers.REGISTRY:
        raise AuthzDenied(404, "target_not_found", f"Connector `{name}` unknown.")
    cran = connector_activation.cran_qui_coupe(name, ctx.org_id)
    if cran is None:
        raise _deja()
    # Coupé par l'hôte, ou fermé par la plateforme : personne dans l'org ne le rouvre,
    # une demande n'aurait personne pour l'accorder (`_org_set` le refuserait).
    if cran == "tenant" or not connector_activation.is_exposed(name, org_id=None):
        raise AuthzDenied(409, "cut_above_org",
                          f"Connector `{name}` is closed above your organization: no one "
                          "in it can turn it on.")
    return {"connector": name}, f"connector:{name}", {}


def _connecteur_accorder(d: dict, accorde: dict):
    from .connectors.activation import OrgActivationSetInput
    return "connectors.activation.set_org", OrgActivationSetInput(
        org_id=int(d["org_id"]), name=d["cible"]["connector"], enabled=True)


# agent_tools — des outils qu'un agent appelle sans en avoir le droit

def _peut_ecrire_l_agent(sub: str, org_id: int, agent: dict) -> bool:
    """Ce que `runner.triggers` op=update exige : écrire l'agent (`ECRIRE`), et qu'il ne
    tourne pas sur le forfait personnel d'un autre qui ne le lui a pas prêté."""
    return (_acces_agent.niveau(sub, int(org_id), agent) in _acces_agent.ECRIRE
            and not _acces_agent.forfait_ferme(sub, agent))


def _agent(org_id: int, trigger_id: int) -> Optional[dict]:
    return db.get_trigger(int(trigger_id), int(org_id))


def _agent_poser(ctx: ResolvedCtx, inp: CreateRequestInput):
    tid = int(_exige("trigger_id", inp.trigger_id))
    voulus = list(dict.fromkeys(t.strip() for t in _exige("tools", inp.tools) if t and t.strip()))
    if not voulus:
        raise AuthzDenied(400, "missing_fields", "`tools` is required for this kind.")
    if len(voulus) > OUTILS_MAX:
        raise AuthzDenied(400, "too_many_tools", f"At most {OUTILS_MAX} tools.")
    agent = _agent(ctx.org_id, tid)
    if agent is None or _acces_agent.niveau(ctx.sub, ctx.org_id, agent) is None:
        raise AuthzDenied(404, "target_not_found", "No such agent in this organization.")
    if _peut_ecrire_l_agent(ctx.sub, ctx.org_id, agent):
        raise AuthzDenied(409, "you_can_grant",
                          "You can change this agent's tools yourself.")
    manquants = [t for t in voulus if t not in (agent.get("tools") or [])]
    if not manquants:
        raise _deja()
    return {"trigger_id": tid}, f"agent:{tid}", {"tools": manquants}


def _agent_acces_poser(ctx: ResolvedCtx, inp: CreateRequestInput):
    tid = int(_exige("trigger_id", inp.trigger_id))
    role = inp.role or "editor"
    if role not in ("viewer", "editor"):
        raise AuthzDenied(400, "invalid_role", "An agent is shared as `viewer` or `editor`.")
    agent = _agent(ctx.org_id, tid)
    # Un agent que l'appelant ne VOIT pas se demande quand même : c'est l'objet du geste
    # (l'accès à l'agent privé d'un collègue, dont on tient l'id d'un lien ou de lui),
    # comme `resource_access` sur un projet privé. Qu'il EXISTE dans l'org se lit donc,
    # par id et dans l'org de l'appel seulement (`get_trigger` filtre l'org : un agent
    # d'une autre org est inconnu) ; son NOM ne se lit pas (`_agent_libelle_lisible`).
    # Rendre 404 à qui ne le voit pas fermerait le geste. `agent_tools`, lui, rend 404
    # (`_agent_poser`) : on ne demande des outils que pour un agent qu'on voit.
    if agent is None:
        raise AuthzDenied(404, "target_not_found", "No such agent in this organization.")
    n = _acces_agent.niveau(ctx.sub, ctx.org_id, agent)
    # Un éditeur dont l'agent est FERMÉ par le forfait de son propriétaire (il ne peut
    # que l'éteindre) demande encore le rôle `editor`, et lui seul : l'accord du
    # propriétaire le nomme éditeur, ce qui prête le forfait. Sans cela le front ne lui
    # laissait que « attends que quelqu'un le lise ». Tout autre rôle, il l'a déjà.
    if n in _acces_agent.ECRIRE:
        if role != "editor" or not _acces_agent.forfait_ferme(ctx.sub, agent):
            raise _deja()
    elif role == "viewer" and n == "viewer":
        raise _deja()
    return {"trigger_id": tid}, f"agent_access:{tid}", {"role": role}


def _agent_gouverne(sub: str, d: dict) -> bool:
    """Ce que `runner.triggers` op=share exige : gouverner l'agent (propriétaire, admin)."""
    agent = _agent(d["org_id"], d["cible"]["trigger_id"])
    if agent is None or _acces_agent.niveau(
            sub, int(d["org_id"]), agent) not in _acces_agent.GOUVERNER:
        return False
    # Une demande de déblocage (le demandeur écrit déjà l'agent, le forfait le ferme) ne
    # s'accorde que par le PROPRIÉTAIRE : lui seul prête son forfait, un partage d'admin
    # ne prête rien.
    demandeur = d.get("requester_sub")
    if (demandeur and _acces_agent.niveau(demandeur, int(d["org_id"]), agent)
            in _acces_agent.ECRIRE and _acces_agent.forfait_ferme(demandeur, agent)):
        return agent.get("sub") == sub
    return True


def _agent_acces_accorder(d: dict, accorde: dict):
    from .runner_triggers import TriggerInput
    return "runner.triggers", TriggerInput(
        op="share", trigger_id=int(d["cible"]["trigger_id"]),
        share_with_sub=d["requester_sub"], role=accorde["role"])


def _agent_libelle_lisible(sub: str, d: dict) -> Optional[str]:
    """Le nom de l'agent à qui le voit déjà : un agent PRIVÉ ne se nomme pas au seul
    demandeur (même règle que `_ressource_libelle`)."""
    agent = _agent(d["org_id"], d["cible"]["trigger_id"])
    if agent is None or _acces_agent.niveau(sub, int(d["org_id"]), agent) is None:
        return None
    return agent.get("label")


def _agent_peut(sub: str, d: dict) -> bool:
    agent = _agent(d["org_id"], d["cible"]["trigger_id"])
    return agent is not None and _peut_ecrire_l_agent(sub, d["org_id"], agent)


def _agent_accorder(d: dict, accorde: dict):
    from .runner_triggers import TriggerInput
    agent = _agent(d["org_id"], d["cible"]["trigger_id"])
    if agent is None:
        raise AuthzDenied(404, "target_not_found", "The agent no longer exists.")
    actuels = list(agent.get("tools") or [])
    return "runner.triggers", TriggerInput(
        op="update", trigger_id=int(agent["id"]),
        tools=actuels + [t for t in accorde["tools"] if t not in actuels])


def _agent_libelle(sub: str, d: dict) -> Optional[str]:
    agent = _agent(d["org_id"], d["cible"]["trigger_id"])
    return agent.get("label") if agent else None


# team_join — entrer dans une équipe de l'org

def _equipe_poser(ctx: ResolvedCtx, inp: CreateRequestInput):
    gid = int(_exige("group_id", inp.group_id))
    g = group_store.get_group(gid)
    if g is None or int(g["org_id"]) != int(ctx.org_id):
        raise AuthzDenied(404, "target_not_found", "No such team in this organization.")
    if group_store.get_group_role(gid, ctx.sub) is not None:
        raise _deja()
    return {"group_id": gid}, f"group:{gid}", {}


def _equipe_accorder(d: dict, accorde: dict):
    from .groups.members import AddGroupMemberInput
    return "group.member.add", AddGroupMemberInput(
        group_id=int(d["cible"]["group_id"]), target=d["requester_sub"])


def _equipe_libelle(sub: str, d: dict) -> Optional[str]:
    """Le nom de l'équipe, à quiconque lit la demande, sans contrôle : aucune équipe
    n'est cachée aux membres de son org — `group.list` (`ORG_MEMBER_OF`) les nomme
    toutes, membre de l'équipe ou non. Demandeur et décideur lisent la demande depuis
    son org ; ce nom ne leur apprend rien que `group.list` ne serve déjà."""
    g = group_store.get_group(int(d["cible"]["group_id"]))
    return g.get("name") if g else None


GESTES: dict[str, _Geste] = {
    "resource_access": _Geste(_ressource_poser, _ressource_peut, _ressource_accorder,
                              _ressource_libelle),
    "connector_activation": _Geste(
        _connecteur_poser, lambda sub, d: roles.is_org_admin(sub, int(d["org_id"])),
        _connecteur_accorder, lambda sub, d: d["cible"]["connector"]),
    "agent_access": _Geste(_agent_acces_poser, _agent_gouverne, _agent_acces_accorder,
                           _agent_libelle_lisible),
    "agent_tools": _Geste(_agent_poser, _agent_peut, _agent_accorder, _agent_libelle),
    "team_join": _Geste(_equipe_poser,
                        lambda sub, d: roles.can_admin_group(sub, int(d["cible"]["group_id"])),
                        _equipe_accorder, _equipe_libelle),
}
assert set(GESTES) == set(KINDS)


# --- Le service ---------------------------------------------------------------------

def _statut(d: dict) -> str:
    if d["status"] == "pending" and not d["vivante"]:
        return "expired"
    return d["status"]


def _iso(v) -> Optional[str]:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _peut_decider(sub: str, d: dict) -> bool:
    return (d["status"] == "pending" and d["vivante"]
            and d["requester_sub"] != sub and GESTES[d["kind"]].peut_decider(sub, d))


def _servie(sub: str, d: dict, emails: dict, *, peut: Optional[bool] = None) -> dict:
    return {
        "id": d["id"], "kind": d["kind"], "status": _statut(d),
        "requester": d["requester_sub"], "requester_email": emails.get(d["requester_sub"]),
        "target": d["cible"], "target_label": GESTES[d["kind"]].libelle(sub, d),
        "asked": d.get("demande") or {}, "note": d.get("note"),
        "created_at": _iso(d.get("created_at")), "expires_at": _iso(d.get("expires_at")),
        "reminded_at": _iso(d.get("reminded_at")),
        "decided_by": d.get("decided_by"), "decided_by_email": emails.get(d.get("decided_by")),
        "decided_at": _iso(d.get("decided_at")), "decision": d.get("decision"),
        "decision_note": d.get("decision_note"),
        "mine": d["requester_sub"] == sub,
        "can_decide": _peut_decider(sub, d) if peut is None else peut,
    }


def _emails(demandes: list[dict]) -> dict:
    subs = {d["requester_sub"] for d in demandes} | {d["decided_by"] for d in demandes
                                                     if d.get("decided_by")}
    return db.emails_by_subs(list(subs)) if subs else {}


def _create(ctx: ResolvedCtx, inp: CreateRequestInput) -> dict:
    # Appels EXPLICITES (pas `GESTES[kind].poser`) : les refus déclarés de la capacité
    # se lisent sur ce chemin (`tests/_refus_atteignables.py` ne suit pas un dict).
    if inp.kind == "resource_access":
        cible, cle, demande = _ressource_poser(ctx, inp)
    elif inp.kind == "connector_activation":
        cible, cle, demande = _connecteur_poser(ctx, inp)
    elif inp.kind == "agent_access":
        cible, cle, demande = _agent_acces_poser(ctx, inp)
    elif inp.kind == "agent_tools":
        cible, cle, demande = _agent_poser(ctx, inp)
    else:
        cible, cle, demande = _equipe_poser(ctx, inp)
    try:
        d, creee = db.demandes_acces.creer(ctx.org_id, ctx.sub, inp.kind, cible, cle,
                                           demande, inp.note, plafond=EN_ATTENTE_MAX)
    except db.demandes_acces.PlafondAtteint:
        raise AuthzDenied(409, "too_many_pending_requests",
                          f"You already have {EN_ATTENTE_MAX} pending requests in this "
                          "organization: cancel one, or wait for a decision.") from None
    return {"request": _servie(ctx.sub, d, _emails([d]), peut=False), "created": creee}


def _list(ctx: ResolvedCtx, inp: ListRequestsInput) -> dict:
    if inp.box == "sent":
        demandes = db.demandes_acces.du_demandeur(ctx.sub, ctx.org_id)
        servies = [_servie(ctx.sub, d, {}, peut=False) for d in demandes]
        emails = _emails(demandes)
        for s, d in zip(servies, demandes):
            s["requester_email"] = emails.get(d["requester_sub"])
            s["decided_by_email"] = emails.get(d.get("decided_by"))
        return {"box": "sent", "requests": servies,
                "pending_count": sum(1 for s in servies if s["status"] == "pending")}
    # Une de plus que la borne : savoir s'il en reste au-delà, sans les compter.
    vivantes = db.demandes_acces.en_attente(ctx.org_id, limit=REVUE_MAX + 1)
    a_decider = [d for d in vivantes[:REVUE_MAX] if _peut_decider(ctx.sub, d)]
    emails = _emails(a_decider)
    return {"box": "review",
            "requests": [_servie(ctx.sub, d, emails, peut=True) for d in a_decider],
            "pending_count": len(a_decider), "truncated": len(vivantes) > REVUE_MAX}


def _de_l_org(ctx: ResolvedCtx, request_id: int) -> dict:
    d = db.demandes_acces.lire(request_id)
    if d is None or int(d["org_id"]) != int(ctx.org_id):
        raise AuthzDenied(404, "request_not_found", "No such request in this organization.")
    return d


def _a_decider(ctx: ResolvedCtx, request_id: int) -> dict:
    d = _de_l_org(ctx, request_id)
    if d["status"] != "pending" or not d["vivante"]:
        raise AuthzDenied(409, "request_closed", f"This request is {_statut(d)}.")
    if d["requester_sub"] == ctx.sub or not GESTES[d["kind"]].peut_decider(ctx.sub, d):
        raise AuthzDenied(403, "cannot_decide",
                          "Only someone who can grant this may decide the request.")
    return d


def _accorde(d: dict, inp: ApproveRequestInput) -> dict:
    """Ce qui sera accordé : le demandé, ajusté par qui décide."""
    demande = d.get("demande") or {}
    if d["kind"] == "resource_access":
        return {"role": inp.role or demande.get("role") or "viewer"}
    if d["kind"] == "agent_access":
        role = inp.role or demande.get("role") or "editor"
        if role not in ("viewer", "editor"):
            raise AuthzDenied(400, "invalid_role", "An agent is shared as `viewer` or `editor`.")
        return {"role": role}
    if d["kind"] == "agent_tools":
        voulus = list(demande.get("tools") or [])
        if inp.tools is None:
            return {"tools": voulus}
        hors = [t for t in inp.tools if t not in voulus]
        if hors:
            raise AuthzDenied(400, "tools_not_asked",
                              f"Not asked: {', '.join(hors)}. Allow only tools that were asked.")
        if not inp.tools:
            raise AuthzDenied(400, "missing_fields", "Allow at least one tool, or decline.")
        return {"tools": list(dict.fromkeys(inp.tools))}
    return {}


def _rejouer(ctx: ResolvedCtx, key: str, cible: BaseModel):
    """La capacité qui accorde, jouée comme si qui approuve l'appelait lui-même : sa
    règle d'autz, la garde d'org suspendue, son handler. Rend son résultat (une
    coroutine pour un handler asynchrone : à attendre hors de ce thread)."""
    from .. import org_suspension
    cap = by_key(key)
    tctx = dataclasses.replace(cap.authz(RawCtx(sub=ctx.sub), cible), channel=ctx.channel)
    org_suspension.garde_capacite(cap.key, tctx)
    return cap.handler(tctx, cible)


async def _approve(ctx: ResolvedCtx, inp: ApproveRequestInput) -> dict:
    # Async seulement pour le geste rejoué qui l'est (`runner.triggers`) ; tout le SQL
    # est hors de la boucle (`docs/event-loop-perf.md`).
    def _preparer():
        d = _a_decider(ctx, inp.request_id)
        # Le demandeur a pu quitter l'org depuis sa demande : rien ne s'accorde alors —
        # un partage de ressource sait viser une personne hors de l'org, et l'aurait
        # posé. Appartenance DIRECTE (`org_store`), pas un rôle effectif : c'est l'état
        # d'un tiers, pas celui de l'acteur courant (`docs/org-context.md`). La demande
        # reste en attente, déclinable, jusqu'à son échéance.
        if org_store.get_org_role(int(d["org_id"]), d["requester_sub"]) is None:
            raise AuthzDenied(409, "requester_left",
                              "The requester is no longer a member of this organization.")
        accorde = _accorde(d, inp)
        key, cible = GESTES[d["kind"]].accorder(d, accorde)
        return d, accorde, _rejouer(ctx, key, cible)

    d, accorde, issue = await run_in_threadpool(_preparer)
    if inspect.isawaitable(issue):
        await issue

    def _clore():
        fait = db.demandes_acces.decider(d["id"], "approved", decided_by=ctx.sub,
                                         decision=accorde, note=inp.note)
        # Deux approbations croisées : l'accès est accordé deux fois (idempotent), la
        # demande ne se clôt qu'une — on rend celle qui l'a close.
        fait = fait or db.demandes_acces.lire(d["id"])
        return _servie(ctx.sub, fait, _emails([fait]), peut=False)

    return await run_in_threadpool(_clore)


def _decline(ctx: ResolvedCtx, inp: DeclineRequestInput) -> dict:
    d = _a_decider(ctx, inp.request_id)
    fait = db.demandes_acces.decider(d["id"], "declined", decided_by=ctx.sub, note=inp.note)
    if fait is None:
        raise AuthzDenied(409, "request_closed", "This request was decided meanwhile.")
    fait = fait
    return _servie(ctx.sub, fait, _emails([fait]), peut=False)


def _a_moi(ctx: ResolvedCtx, request_id: int) -> dict:
    d = _de_l_org(ctx, request_id)
    if d["requester_sub"] != ctx.sub:
        raise AuthzDenied(403, "not_your_request", "Only its requester may do this.")
    if d["status"] != "pending" or not d["vivante"]:
        raise AuthzDenied(409, "request_closed", f"This request is {_statut(d)}.")
    return d


def _cancel(ctx: ResolvedCtx, inp: _UneDemande) -> dict:
    d = _a_moi(ctx, inp.request_id)
    fait = db.demandes_acces.decider(d["id"], "cancelled", decided_by=ctx.sub)
    if fait is None:
        raise AuthzDenied(409, "request_closed", "This request was decided meanwhile.")
    fait = fait
    return _servie(ctx.sub, fait, _emails([fait]), peut=False)


def _remind(ctx: ResolvedCtx, inp: _UneDemande) -> dict:
    d = _a_moi(ctx, inp.request_id)
    return {"reminded": db.demandes_acces.relancer(d["id"])}


_CHEMIN = "/api/me/access-requests"
_UNE = _CHEMIN + "/{request_id}"
_INCONNUE = DeclaredError(404, "request_not_found", "aucune demande de cet id dans l'org de l'appel")
_CLOSE = DeclaredError(409, "request_closed", "la demande n'est plus en attente (décidée, annulée ou échue)")

CAPABILITIES += [
    Capability(
        key="me.access_requests.create",
        handler=_create,
        Input=CreateRequestInput,
        Output=CreatedRequest,
        authz=ORG_MEMBER,
        rest=RestBinding(verb="POST", path=_CHEMIN, status=201, provisoire=True),
        errors=(
            DeclaredError(400, "missing_fields", "un champ que ce `kind` exige manque"),
            DeclaredError(400, "too_many_tools", f"`tools` de plus de {OUTILS_MAX} outils"),
            DeclaredError(400, "invalid_role", "`agent_access` : un rôle autre que viewer/editor"),
            DeclaredError(404, "target_not_found",
                          "la cible n'existe pas, ou ne vit pas dans l'org de l'appel"),
            DeclaredError(409, "already_granted", "l'appelant a déjà ce qu'il demande"),
            DeclaredError(409, "cut_above_org",
                          "`connector_activation` : connecteur fermé au-dessus de l'org "
                          "(plateforme ou hôte), que personne dans l'org ne rouvre"),
            DeclaredError(409, "you_can_grant",
                          "`agent_tools` : l'appelant peut changer lui-même les outils de cet agent"),
            DeclaredError(409, "too_many_pending_requests",
                          f"l'appelant a déjà {EN_ATTENTE_MAX} demandes en attente dans l'org"),
        ),
        description=(
            "Ask for access you are blocked on, in the active organization: a role on a "
            "project, table, procedure or page (`resource_access`), a connector turned on "
            "for the org (`connector_activation`), access to an agent (`agent_access`), "
            "tools for an agent (`agent_tools`), or "
            "a place in a team (`team_join`). The people who can grant it see it in their "
            "requests and approve it in one step. Asking again returns the pending one."
        ),
    ),
    Capability(
        key="me.access_requests.list",
        handler=_list,
        Input=ListRequestsInput,
        Output=RequestList,
        authz=ORG_MEMBER,
        rest=RestBinding(verb="GET", path=_CHEMIN, provisoire=True),
        description=(
            "The access requests of the active organization the caller can decide "
            "(`box=review`, newest first, with `pending_count` for a badge), or the "
            "caller's own (`box=sent`)."
        ),
    ),
    Capability(
        key="me.access_requests.approve",
        handler=_approve,
        Input=ApproveRequestInput,
        Output=AccessRequest,
        authz=ORG_MEMBER,
        rest=RestBinding(verb="POST", path=_UNE + "/approve", provisoire=True),
        errors=(
            _INCONNUE, _CLOSE,
            DeclaredError(403, "cannot_decide",
                          "l'appelant ne peut pas accorder ce geste (ou c'est sa propre demande)"),
            DeclaredError(400, "tools_not_asked", "`tools` nomme un outil qui n'était pas demandé"),
            DeclaredError(400, "missing_fields", "`tools` vide : accorder au moins un outil, ou refuser"),
            DeclaredError(400, "invalid_role", "`agent_access` : un rôle autre que viewer/editor"),
            DeclaredError(409, "requester_left",
                          "le demandeur n'est plus membre de l'org de la demande : rien n'est accordé"),
        ),
        description=(
            "Approve a pending request: grants it as the caller, through the same action "
            "they would use by hand (its refusals included). `role` / `tools` adjust what "
            "is granted."
        ),
    ),
    Capability(
        key="me.access_requests.decline",
        handler=_decline,
        Input=DeclineRequestInput,
        Output=AccessRequest,
        authz=ORG_MEMBER,
        rest=RestBinding(verb="POST", path=_UNE + "/decline", provisoire=True),
        errors=(_INCONNUE, _CLOSE,
                DeclaredError(403, "cannot_decide", "l'appelant ne peut pas accorder ce geste")),
        description="Decline a pending request, with an optional note for the requester.",
    ),
    Capability(
        key="me.access_requests.cancel",
        handler=_cancel,
        Input=_UneDemande,
        Output=AccessRequest,
        authz=ORG_MEMBER,
        rest=RestBinding(verb="POST", path=_UNE + "/cancel", provisoire=True),
        errors=(_INCONNUE, _CLOSE,
                DeclaredError(403, "not_your_request", "seul le demandeur annule sa demande")),
        description="Withdraw one of your pending requests.",
    ),
    Capability(
        key="me.access_requests.remind",
        handler=_remind,
        Input=_UneDemande,
        Output=Reminded,
        authz=ORG_MEMBER,
        rest=RestBinding(verb="POST", path=_UNE + "/remind", provisoire=True),
        errors=(_INCONNUE, _CLOSE,
                DeclaredError(403, "not_your_request", "seul le demandeur relance sa demande")),
        description="Remind the deciders of one of your pending requests (once a day at most).",
    ),
]
