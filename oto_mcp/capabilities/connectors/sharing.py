"""The RULES of an account (#1499, `docs/partage-de-compte.md` § Les règles de comptes) —
the write and read surface of `connector_account_rules`.

An account (`connector_instances`, `inst:<n>`) gives each person of its org a level —
`none < read < write < sensitive < manage` — computed by `access/niveaux.niveau` from its
rules: one per subject (a member, a team, the whole org), with an optional list of
services for a Google or Microsoft account. Here the rules are set, removed and read,
and the journal of every change is read (`connector_account_share_events`, written by
trigger, the author being the caller).

Reserved to whoever MANAGES the account (`niveau(...) == "manage"`): its owner for a
personal account (alone: no role reaches a person's own account), the team lead for a
team account, an org admin for the team and org accounts — and anyone a rule gives
`manage`. Two bounds on what is granted:

- `manage` is granted only by an org admin;
- a team lead who manages the account only as a team lead grants at most `write`.

A rule may name a person OUTSIDE the account's org (D1): designated by the email of an
existing oto account, never a team or an org of elsewhere. Such an account serves that
person only NAMED, at the rule's level; it is listed to her with the lending org's name
and nothing else of that org. Refusing an address says nothing of whether an oto account
carries it (`unknown_subject`, one code and one message for every subject not found).

A rule naming the owner of a personal account is refused before writing
(`invalid_arguments`, reason `owner_subject`): the owner always manages her account,
and the base refuses it too (`connector_account_rules_owner_subject`).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from ... import access, credentials_store, db, group_store, org_store, providers, roles
from ...connectors import services_accordes
from ...db import regles_de_comptes
from .._authz import SUB_ONLY
from .._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES

Niveau = Literal["none", "read", "write", "sensitive", "manage"]

#: What a team lead grants when she manages the account only as its team's lead.
_PLAFOND_CHEF_D_EQUIPE = "write"

_DOC_INSTANCE = ("the account's stable identifier, `inst:<n>` from oto_instance op=list "
                 "(or `<n>`)")
_DOC_TO = ("a person: a member of the account's org (email or sub), or a person outside "
           "it by the email of her oto account — instead of `to_group` and `to_org`")
_DOC_TO_GROUP = "id of a team of the account's org — instead of `to` and `to_org`"
_DOC_TO_ORG = "true = the whole org of the account — instead of `to` and `to_group`"


# ── Inputs ───────────────────────────────────────────────────────────────────────

class AccountInput(BaseModel):
    instance_id: int = Field(description="the account's stable identifier (the `<n>` of "
                                         "`inst:<n>`)")


class ShareEventsInput(AccountInput):
    limit: int = Field(100, ge=1, le=500, description="most recent events first")


class RuleSubjectInput(AccountInput):
    to: Optional[str] = Field(None, description=_DOC_TO)
    to_group: Optional[int] = Field(None, description=_DOC_TO_GROUP)
    to_org: bool = Field(False, description=_DOC_TO_ORG)


class SetRuleInput(RuleSubjectInput):
    level: Niveau = Field(description="the level the rule gives its subject: none < read "
                                      "< write < sensitive < manage")
    services: Optional[list[str]] = Field(
        None, description="Google or Microsoft account only: the services the rule covers "
                          "(each one authorized by the account); the account itself is "
                          "always covered. Omitted = every service")


# ── Outputs ──────────────────────────────────────────────────────────────────────

class AccountRule(BaseModel):
    subject: Literal["user", "group", "org"]
    user: Optional[str] = None              # subject=user: the sub
    group: Optional[int] = None             # subject=group: the team id
    label: str                              # the subject, as a person reads it
    level: Niveau
    # Google/Microsoft account: the services the rule covers; null = every service.
    services: Optional[list[str]] = None
    # `migrated` = taken over from the loans and defaults of before the rules;
    # `manual` = set by a person.
    origin: Literal["migrated", "manual"]
    set_by: Optional[str] = None
    updated_at: Optional[str] = None


class AccountRules(BaseModel):
    """The rules of one account, and the level the caller holds on it."""
    instance_id: int
    id: str                                 # `inst:<n>`
    connector: str
    account: str = ""
    owner_type: Literal["member", "group", "org"]
    managed_by: str                         # who manages it, in words
    your_level: Niveau
    rules: list[AccountRule]


class ShareEvent(BaseModel):
    # `set` = laid or changed (`previous_level`); `transferred` = the account changed
    # owner: the subject is the new owner, `owner_before` the previous one.
    action: Literal["set", "removed", "transferred"]
    subject: Literal["user", "group", "org"]
    subject_id: Optional[str] = None        # the sub or the team id; null for the org
    owner_before: Optional[str] = None      # `transferred`: `<member|group|org>:<id>`
    level: Optional[str] = None
    previous_level: Optional[str] = None
    services: Optional[list[str]] = None
    # The subject is a person outside the account's org (D1).
    outside_org: bool = False
    actor_sub: Optional[str] = None
    # How a person reads the subject and the actor (an e-mail, a team name, "the whole
    # org"): a member reads the journal without access to the members of another org,
    # nor to a person who left.
    subject_label: Optional[str] = None
    actor_label: Optional[str] = None
    at: str


class ShareEvents(BaseModel):
    instance_id: int
    events: list[ShareEvent]
    count: int


# ── The account and the right to manage it ───────────────────────────────────────

def appelant_de(ctx: ResolvedCtx):
    sub = ctx.sub
    return access.Appelant(sub, ctx.org_id if ctx.org_id is not None
                           else access.current_org(sub), lambda: access.current_group(sub))


def compte_par_id(instance_id: int):
    row = regles_de_comptes.compte_par_id(int(instance_id))
    if row is None:
        raise AuthzDenied(404, "unknown_instance",
                          f"No live account `inst:{int(instance_id)}`: take its `id` from "
                          "oto_instance op=list.")
    return access._compte(row)


def gere(ctx: ResolvedCtx, instance_id: int):
    """The account, and the caller who manages it — or a 403 naming who does."""
    appelant = appelant_de(ctx)
    compte = compte_par_id(instance_id)
    if not access.suffit(access.niveau(appelant, compte), "manage"):
        raise AuthzDenied(403, "insufficient_level",
                          f"You do not manage the account `{compte.libelle}` "
                          f"({compte.connector}): its rules are set by "
                          f"{access.qui_gere(compte)}. Nothing was done.")
    return appelant, compte


def _plafond(appelant, compte) -> str:
    """The highest level this manager grants: `manage` for an org admin; `write` for a
    team lead who manages the account only as its team's lead; `sensitive` otherwise
    (the owner, a person a rule makes manager)."""
    if compte.org_id is not None and appelant.admin_de_l_org(compte.org_id):
        return "manage"
    if compte.proprietaire == appelant.sub:
        return "sensitive"
    if compte.equipe is not None and appelant.chef_de_l_equipe(compte.equipe) \
            and not any(r.user == appelant.sub and r.level == "manage"
                        for r in compte.regles):
        return _PLAFOND_CHEF_D_EQUIPE
    return "sensitive"


def _refuser_au_dessus(appelant, compte, level: str, geste: str) -> None:
    plafond = _plafond(appelant, compte)
    if access.suffit(plafond, level):
        return
    if level != "manage":
        qui = f"as a team lead you grant at most `{plafond}`"
    elif geste == "set":
        qui = "only an admin of the org grants `manage`"
    elif compte.proprietaire == appelant.sub:
        # Only `change` reaches here: the owner REMOVES a `manage` rule (`_retirable`).
        qui = ("only an admin of the org grants `manage`; as the account's owner you can "
               "remove this rule, then set a new one at the level you want")
    elif compte.proprietaire is not None:
        # No admin manages a personal account (`niveau`): its owner alone removes it.
        qui = (f"on a personal account only its owner, "
               f"{access.libelle_de_personne(compte.proprietaire)}, removes a `manage` rule")
    else:
        nom = compte.org_nom or (org_store.get_org(compte.org_id) or {}).get("name")
        qui = (f"a `manage` rule is changed or removed only by an admin of the org "
               f"{nom or '#' + str(compte.org_id)} — ask one")
    raise AuthzDenied(403, "level_not_grantable",
                      f"You cannot {geste} a rule at `{level}` on `{compte.libelle}`: {qui}. "
                      "Nothing was done.")


# ── The subject of a rule ────────────────────────────────────────────────────────

def un_seul_sujet(inp) -> None:
    """Exactly one of `to`, `to_group`, `to_org` — the subject of a rule, or the new owner
    of an account (`capabilities/connectors/transfert`)."""
    if (inp.to is not None) + (inp.to_group is not None) + bool(inp.to_org) != 1:
        raise AuthzDenied(400, "one_subject",
                          "Name exactly one subject: `to` (a member's email or sub), "
                          "`to_group` (a team of the account's org) or `to_org=true`.")


def equipe_de_l_org(org: int, gid: int) -> int:
    g = group_store.get_group(int(gid))
    if g is None or int(g["org_id"]) != int(org):
        raise AuthzDenied(404, "unknown_group", f"No team #{gid} in the account's org.")
    return int(gid)


def personne_de_l_org(org: int, to: str, *, membre_exige: bool = True) -> str:
    """The sub `to` names in `org`: an email resolves among the org's members only; a sub
    must be a member unless `membre_exige=False` (the sub of a person who left)."""
    to = to.strip()
    if "@" in to:
        member = org_store.get_org_member_by_email(org, to)
        if member is None:
            raise AuthzDenied(404, "not_an_org_member",
                              f"`{to}` is not the email of a member of the account's org "
                              f"(org #{org}).")
        return member["sub"]
    if membre_exige and not roles.is_org_member(to, org):
        raise AuthzDenied(404, "not_an_org_member",
                          f"`{to}` is not a member of the account's org (org #{org}).")
    return to


def _sujet_introuvable() -> AuthzDenied:
    """THE refusal of a person who cannot be named — unknown address, an address no
    designable oto account carries, a sub outside the org: one code, one message, so that
    nothing tells whether an oto account exists under an address (D1)."""
    return AuthzDenied(404, "unknown_subject",
                       "No person can be given this account under that address. Nothing "
                       "was done.")


def personne_designable(org: int, to: str, *, regles=()) -> str:
    """The sub `to` names as the subject of a rule: a member of `org` (email or sub), a
    person OUTSIDE it by the email of her oto account (D1) — exactly one account of ours
    under that address, a third-party tenant's (`<tenant>:…`, ADR 0052) is not designable
    — or, to name an existing rule, the sub that rule names (a person who left, a person
    outside). Anything else: `_sujet_introuvable`, the same for every case."""
    to = to.strip()
    if "@" in to:
        member = org_store.get_org_member_by_email(org, to)
        if member is not None:
            return member["sub"]
        designables = [u for u in db.get_users_by_email(to) if ":" not in (u.get("sub") or "")]
        if len(designables) != 1:
            raise _sujet_introuvable()
        return designables[0]["sub"]
    if roles.is_org_member(to, org) or any(r.user == to for r in regles):
        return to
    raise _sujet_introuvable()


def _sujet(inp: RuleSubjectInput, compte) -> dict:
    """`{subject_user|subject_group|subject_org}` from the input: a team or the org of
    the account's org; a person of the org or outside it (`personne_designable`). An
    EXISTING rule is named by the sub it names — to change its level as to remove it —
    even when that person is outside the org or has left it; a NEW rule outside the org
    still names its person by e-mail only."""
    un_seul_sujet(inp)
    org = compte.org_id
    if inp.to_org:
        return {"subject_org": True}
    if inp.to_group is not None:
        return {"subject_group": equipe_de_l_org(org, inp.to_group)}
    to = inp.to.strip()
    sub = personne_designable(org, to, regles=compte.regles)
    if sub == compte.proprietaire:
        raise AuthzDenied(400, "invalid_arguments",
                          f"`{to}` owns the account `{compte.libelle}` and always manages "
                          "it: a rule cannot name its owner. Nothing was done.",
                          details={"reason": "owner_subject"})
    return {"subject_user": sub}


def _regle_du_sujet(compte, sujet: dict):
    for r in compte.regles:
        if (r.user == sujet.get("subject_user") and r.group == sujet.get("subject_group")
                and r.org == bool(sujet.get("subject_org"))):
            return r
    return None


def _services(inp: SetRuleInput, compte) -> Optional[list]:
    """The services the rule covers: only on a Google or Microsoft account, each one a
    service of that account that it has authorized."""
    if inp.services is None:
        return None
    porteur = compte.connector
    if porteur not in access.PORTEURS_A_SERVICES:
        raise AuthzDenied(400, "services_not_applicable",
                          f"`services` applies to a Google or Microsoft account only: a "
                          f"`{porteur}` rule covers the account whole. Nothing was done.")
    demandes = list(dict.fromkeys(inp.services))
    connus = services_accordes.services_du_porteur(porteur)
    inconnus = [x for x in demandes if x not in connus]
    if inconnus:
        raise AuthzDenied(400, "unknown_service",
                          f"{', '.join(f'`{x}`' for x in inconnus)}: not a service of a "
                          f"{providers.REGISTRY[porteur].label} account (one of "
                          f"{', '.join(connus)}). Nothing was done.")
    meta = next((r.get("meta") or {} for r in credentials_store.list_accounts(
        compte.owner_type, compte.owner_id, porteur) if (r["account"] or "") == compte.account),
        {})
    autorises = services_accordes.services_autorises(porteur, meta)
    manquants = [x for x in demandes if x not in autorises]
    if manquants:
        raise AuthzDenied(400, "service_not_authorized",
                          f"The account `{compte.libelle}` has not authorized "
                          f"{', '.join(f'`{x}`' for x in manquants)}: connect it from its "
                          "card first — a rule covers only what the account authorized. "
                          "Nothing was done.")
    return [x for x in connus if x in demandes]


# ── The projection ───────────────────────────────────────────────────────────────

def _iso(v) -> Optional[str]:
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v is not None else None)


def _etat(appelant, compte) -> dict:
    """The rules of the account as the base holds them now, labelled."""
    lignes = regles_de_comptes.regles_du_compte(compte.instance_id)
    groupes: dict = {}
    regles = []
    for r in lignes:
        if r["subject_user"] is not None:
            regle = {"subject": "user", "user": r["subject_user"],
                     "label": access.libelle_de_personne(r["subject_user"])}
        elif r["subject_group"] is not None:
            gid = int(r["subject_group"])
            if gid not in groupes:
                groupes[gid] = (group_store.get_group(gid) or {}).get("name") or f"team #{gid}"
            regle = {"subject": "group", "group": gid, "label": groupes[gid]}
        else:
            regle = {"subject": "org", "label": "the whole org"}
        regle.update(level=r["level"],
                     services=list(r["services"]) if r["services"] is not None else None,
                     origin=("migrated" if r["origine"] == regles_de_comptes.ORIGINE_REPRISE
                             else "manual"),
                     set_by=r.get("created_by"), updated_at=_iso(r.get("updated_at")))
        regles.append(regle)
    return {"instance_id": compte.instance_id, "id": f"inst:{compte.instance_id}",
            "connector": compte.connector, "account": compte.account,
            "owner_type": compte.owner_type, "managed_by": access.qui_gere(compte),
            "your_level": access.niveau(appelant, compte), "rules": regles}


# ── The handlers ─────────────────────────────────────────────────────────────────

def _list_rules(ctx: ResolvedCtx, inp: AccountInput) -> dict:
    appelant, compte = gere(ctx, inp.instance_id)
    return _etat(appelant, compte)


def _set_rule(ctx: ResolvedCtx, inp: SetRuleInput) -> dict:
    appelant, compte = gere(ctx, inp.instance_id)
    sujet = _sujet(inp, compte)
    _refuser_au_dessus(appelant, compte, inp.level, "set")
    actuelle = _regle_du_sujet(compte, sujet)
    if actuelle is not None:
        _refuser_au_dessus(appelant, compte, actuelle.level, "change")
    services = _services(inp, compte)
    regles_de_comptes.poser_regle(compte.org_id, compte.instance_id, **sujet,
                                  level=inp.level, services=services, acteur=ctx.sub)
    return _etat(appelant, compte)


def _retirable_par_le_proprietaire(appelant, compte, regle) -> bool:
    """The owner of a PERSONAL account removes a `manage` rule set on it — the one
    exception to `_plafond`, for the removal only: no admin manages a personal account,
    so nobody else could. Setting or changing a rule to or from `manage` stays an
    admin's act."""
    return (compte.owner_type == "member" and compte.proprietaire == appelant.sub
            and regle.level == "manage")


def _remove_rule(ctx: ResolvedCtx, inp: RuleSubjectInput) -> dict:
    appelant, compte = gere(ctx, inp.instance_id)
    sujet = _sujet(inp, compte)
    actuelle = _regle_du_sujet(compte, sujet)
    if actuelle is not None and not _retirable_par_le_proprietaire(appelant, compte,
                                                                   actuelle):
        _refuser_au_dessus(appelant, compte, actuelle.level, "remove")
    if not regles_de_comptes.retirer_regle(compte.instance_id, **sujet, acteur=ctx.sub):
        raise AuthzDenied(404, "no_rule",
                          f"No rule for this subject on `{compte.libelle}`. Nothing was done.")
    return _etat(appelant, compte)


def _libelles_de_personnes(subs) -> dict:
    """`{sub: label}` — the e-mails in ONE read; a sub without one is named as
    `libelle_de_personne` names it (its name, else the sub)."""
    subs = sorted({s for s in subs if s})
    emails = db.emails_by_subs(subs) if subs else {}
    return {s: emails.get(s) or access.libelle_de_personne(s) for s in subs}


def _share_events(ctx: ResolvedCtx, inp: ShareEventsInput) -> dict:
    _, compte = gere(ctx, inp.instance_id)
    journal = regles_de_comptes.journal_du_compte(compte.instance_id, inp.limit)
    personnes = _libelles_de_personnes(
        [e["actor_sub"] for e in journal]
        + [e["subject_id"] for e in journal if e["subject_kind"] == "user"])
    equipes: dict = {}

    def _sujet_lu(e) -> Optional[str]:
        if e["subject_kind"] == "user":
            return personnes.get(e["subject_id"])
        if e["subject_kind"] == "group":
            gid = int(e["subject_id"])
            if gid not in equipes:
                equipes[gid] = (group_store.get_group(gid) or {}).get("name") or f"team #{gid}"
            return equipes[gid]
        return "the whole org"
    events = [{"action": e["action"], "subject": e["subject_kind"],
               "subject_id": e["subject_id"],
               "level": e["level"], "previous_level": e["previous_level"],
               "services": list(e["services"]) if e["services"] is not None else None,
               "owner_before": e.get("owner_before"),
               "outside_org": bool(e.get("hors_org")),
               "actor_sub": e["actor_sub"], "subject_label": _sujet_lu(e),
               "actor_label": personnes.get(e["actor_sub"]), "at": _iso(e["at"])}
              for e in journal]
    return {"instance_id": compte.instance_id, "events": events, "count": len(events)}


_ROUTE = "/api/me/connector-instances/{instance_id}"

_E_COMPTE = (DeclaredError(404, "unknown_instance", "no live account has this identifier"),
             DeclaredError(403, "insufficient_level",
                           "you do not manage this account — the refusal names who does"))
_E_SUJET = (DeclaredError(400, "one_subject", "not exactly one of `to`, `to_group`, `to_org`"),
            DeclaredError(404, "unknown_group", "the team is not one of the account's org"),
            DeclaredError(404, "unknown_subject",
                          "no person can be named under that address — the same answer "
                          "whether or not an oto account carries it"),
            DeclaredError(400, "invalid_arguments",
                          "the rule names the owner of the personal account "
                          "(`details.reason = owner_subject`)"),
            DeclaredError(403, "level_not_grantable",
                          "`manage` granted, changed or removed by someone other than an "
                          "org admin (the owner of a personal account may remove one), or "
                          "above `write` by a team lead"))

CAPABILITIES += [
    Capability(
        key="connectors.instances.rules.list", handler=_list_rules, Input=AccountInput,
        Output=AccountRules, authz=SUB_ONLY, errors=_E_COMPTE,
        description=("The RULES of an account you manage: for each subject (a member, a "
                     "team, the whole org) the level it gives (none < read < write < "
                     "sensitive < manage) and, on a Google or Microsoft account, the "
                     "services it covers; plus who manages it and your own level. Without "
                     "a rule, a member of the account's team holds `sensitive` and a "
                     "member of its org `read` on an org account."),
        rest=RestBinding("GET", _ROUTE + "/rules"),
    ),
    Capability(
        key="connectors.instances.rules.set", handler=_set_rule, Input=SetRuleInput,
        Output=AccountRules, authz=SUB_ONLY,
        errors=_E_COMPTE + _E_SUJET + (
            DeclaredError(400, "services_not_applicable",
                          "`services` on an account other than Google or Microsoft"),
            DeclaredError(400, "unknown_service", "a service not of this account's kind"),
            DeclaredError(400, "service_not_authorized",
                          "a service the account has not authorized")),
        description=("Set (or replace) the rule of ONE subject on an account you manage — "
                     "`to` (a member's email or sub, or a person OUTSIDE the org by the "
                     "email of her oto account), `to_group` (a team of the account's org) "
                     "or `to_org=true` — at `level` (none|read|write|sensitive|manage); "
                     "`services` limits it to some services of a Google or Microsoft "
                     "account. A person outside the org uses the account only by naming "
                     "it. Only an org admin grants `manage`; a team lead grants at most "
                     "`write`. A rule never names the account's owner. "
                     "Journaled. Returns the account's rules afterwards."),
        rest=RestBinding("PUT", _ROUTE + "/rules"),
    ),
    Capability(
        key="connectors.instances.rules.remove", handler=_remove_rule, Input=RuleSubjectInput,
        Output=AccountRules, authz=SUB_ONLY,
        errors=_E_COMPTE + _E_SUJET + (
            DeclaredError(404, "no_rule", "the subject has no rule on this account"),),
        description=("Remove the rule of ONE subject on an account you manage (`to`, "
                     "`to_group` or `to_org=true`): the subject falls back to the default "
                     "level. A `manage` rule is removed by an org admin, or by the owner "
                     "of a personal account. Journaled. Returns the account's rules "
                     "afterwards."),
        rest=RestBinding("DELETE", _ROUTE + "/rules", reads_body=True),
    ),
    Capability(
        key="connectors.instances.share_events", handler=_share_events,
        Input=ShareEventsInput, Output=ShareEvents, authz=SUB_ONLY, errors=_E_COMPTE,
        description=("The journal of the rules of an account you manage, most recent "
                     "first: each rule set, changed or removed, with its level before and "
                     "after, who did it, and whether its subject is outside the org."),
        rest=RestBinding("GET", _ROUTE + "/share-events"),
    ),
]
