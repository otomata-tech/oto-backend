"""Instance SHARING capability (ADR 0044) — WRITE surface of `share_side`.

`share_side` = EXTENSION: a member lends THEIR instance (their key in the current org)
to a named peer, to a team of that org, or to the whole of that org. A lent account
serves only when the call NAMES it (`access/prets.py`): a peer pins the instance
(`_instance=`), the members of a team or of the org name the account (`_account=`, the
team or org rung of the cascade) or pin it. For a peer, the guard
`access.guard_instance_access` authorizes the beneficiary (borrows the key, keeps
their OWN org context — cross-org OK, the named loan is the consent). Refused at the
pose: a team or an org nothing would serve it to, and a personal key the org forbids
(#1239). Every effective pose and withdrawal is journaled (`db/loan_events`), in the
transaction that writes the `share_side`.
Owner-scoped: `SUB_ONLY`, the handler touches ONLY the caller's vault row
(`member_id(current org, sub)`) → you only ever lend your own key.

⚠️ `share_down` (RESTRICTING an org/team shared key) is the OPPOSITE axis (deny-by-
default) — distinct write surface, not exposed here.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from ... import access, credentials_store, db, group_store, instance_refs, org_store, providers
from ...connectors import cle_perso, services_accordes
from ...db import loan_events
from .._authz import ORG_ADMIN_OF, SUB_ONLY
from .._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES


class LendInstanceInput(BaseModel):
    connector: str = Field(description="the connector — a service card (`outlook`, "
                                       "`gmail`…) lends the account it borrows")
    to: Optional[str] = Field(None, description="the email or the sub of the peer to "
                                                "lend to (or to revoke the loan from) — an "
                                                "email must be a member of your current "
                                                "organization")
    to_group: Optional[int] = Field(None, description="id of a team of your current org "
                                                      "to lend to (or to revoke the loan "
                                                      "from) — its members use the key "
                                                      "only by naming it (`_account=`)")
    to_org: bool = Field(False, description="true = lend to (or revoke the loan from) "
                                            "every member of your current org — they use "
                                            "the key only by naming it (`_account=`); "
                                            "instead of `to` and `to_group`")
    account: str = Field("", description="which of your accounts — optional when you "
                                         "have only one")
    services: Optional[list[str]] = Field(
        None, description="Google or Microsoft account only: the services lent to this "
                          "borrower, replacing the list they had (extend or reduce a "
                          "loan this way). Default: the service card named in "
                          "`connector`, or, for the account itself, the services it "
                          "authorized today — never one it authorizes later")
    revoke: bool = False


class LoanStateInput(BaseModel):
    connector: str = Field(description="the connector — a service card (`outlook`, "
                                       "`gmail`…) reads the loans of the account it "
                                       "borrows")
    account: str = Field("", description="which of your accounts — optional when you "
                                         "have only one")


class LoanState(BaseModel):
    """Who one of MY accounts is lent to, and for which services."""
    # The connector whose vault row is lent: the CARRIER when a service card was named
    # (`outlook` → `microsoft`), since a service owns no instance (`credential_of`).
    connector: str
    account: str = ""                       # the account lent (`""` = single-key connector)
    # ⚠️ The COMPLETE lists of borrowers, not just one: after a `revoke`, non-empty
    # lists if other loans remained.
    lent_to: list[str]                      # borrowers' subs (`user:` entries)
    lent_to_groups: list[int] = []          # borrowing teams (`group:` entries)
    lent_to_org: bool = False               # lent to the whole current org (`org:` entry)
    # Google/Microsoft account: the services lent, per borrower (`user:<sub>`,
    # `group:<id>`, `org:<id>`; a borrower without a list is lent none), and those the
    # account authorized — what a loan may lend. Null for a key lent whole.
    services_by_borrower: Optional[dict[str, list[str]]] = None
    services_lendable: Optional[list[str]] = None
    # How a person reads each service of this account (`gmail` → `Gmail`).
    service_labels: Optional[dict[str, str]] = None


class LendInstanceResult(LoanState):
    """State of the loan AFTER the operation — not just an acknowledgment of the action."""
    ok: bool
    # Present when the loan reaches beyond the named card: lending a carrier's account
    # lends every service that account authorized — said, never implied.
    note: Optional[str] = None
    revoked: bool                           # echo of the intent (`revoke` from the input)
    # Google/Microsoft account: the services lent to THIS borrower after the operation
    # (`[]` after a revoke); null for a key that has no services of its own.
    services: Optional[list[str]] = None


_ACCEPTED_TO = "`to` = the email or the sub of a member of your organization"


def _borrower_sub(org: int, to: str) -> str:
    """The borrower's sub. An EMAIL resolves among the members of the lender's CURRENT
    org only — never through the platform directory: an email outside the org gets
    the same refusal whether or not an oto account carries it elsewhere. A sub is
    taken as is (a named loan may cross orgs, ADR 0044)."""
    to = (to or "").strip()
    if "@" in to:
        member = org_store.get_org_member_by_email(org, to)
        if member is None:
            raise AuthzDenied(404, "not_an_org_member",
                              f"`{to}` is not the email of a member of your organization "
                              f"(org #{org}): only a member can borrow by email — "
                              f"{_ACCEPTED_TO}.")
        return member["sub"]
    if db.get_user(to) is None:
        raise AuthzDenied(404, "unknown_user", f"Unknown user `{to}`: {_ACCEPTED_TO}.")
    return to


def _equipes_a_cle(org: int, sub: str, connector: str) -> list[dict]:
    """The caller's teams in `org` whose OWN row holds `connector` (a loan to the team
    is not its key) — what the caller may take for « my instance » (#1343)."""
    return [g for g in group_store.list_groups_for_user(sub, org)
            if credentials_store.list_accounts("group", str(g["group_id"]), connector)]


def _lent_account(org: int, sub: str, connector: str, account: str) -> str:
    """The account of the lender's row to lend: the one named, otherwise the only one.
    Several and none named = a refusal that lists them; none at all = no instance —
    or, when a team of the caller holds the key, a refusal that says only a personal
    key lends and how that team key reaches another team (#1343)."""
    eid = credentials_store.member_id(org, sub)
    accounts = [r["account"] for r in credentials_store.list_accounts(
        credentials_store.MEMBER, eid, connector)]
    if account and account in accounts:
        return account
    if not accounts:
        equipes = _equipes_a_cle(org, sub, connector)
        if equipes:
            noms = ", ".join(f"#{g['group_id']} \"{g['name']}\"" for g in equipes)
            raise AuthzDenied(400, "team_key_not_lendable",
                              f"The `{connector}` key you see is a TEAM key (team {noms}), "
                              "and only a personal key is lent: a team key is not lent to "
                              "another team. To serve another team, set the key in that "
                              "team too (by one of its team admins), or once for the "
                              "whole org (by an org admin). Nothing was lent.")
        raise AuthzDenied(404, "no_instance",
                          f"No `{connector}` instance set in this org — nothing to lend "
                          f"(configure your key first).")
    listed = ", ".join(f"`{a}`" for a in accounts)
    if account:
        raise AuthzDenied(404, "unknown_account",
                          f"You have no `{connector}` account `{account}` in this org: "
                          f"pass `account=`: one of {listed}.")
    if len(accounts) == 1:
        return accounts[0]
    raise AuthzDenied(400, "account_required",
                      f"You have several `{connector}` accounts: pass `account=`: one of "
                      f"{listed}.")


def _shared_loan_served(carrier: str) -> bool:
    """Does anything SERVE a loan of this carrier to a team or to the whole org? The
    team and org rungs of the cascade, for an org-shareable carrier (`walk_cascade`);
    the loan resolver of the Microsoft account (`auth/microsoft._lent_for_call`, on an
    explicit `_account=`). Otherwise the loan would be written and never served —
    an "ok" for an inert loan: refused at the pose instead."""
    from ...auth import microsoft as ms_auth
    return providers.is_org_shareable(carrier) or carrier == ms_auth.CONNECTOR


def _designation(carrier: str, eid: str, account: str, partage: bool) -> str:
    """How the borrower names the loan: `_account=` where a name is served (the team
    and org rungs, the Microsoft resolver), otherwise the instance ref (`_instance=`)."""
    from ...auth import microsoft as ms_auth
    if account and (partage or carrier == ms_auth.CONNECTOR):
        return f"`_account={account}`"
    ref = instance_refs.ref_for_credential(credentials_store.MEMBER, eid, carrier, account)
    return f"`_instance={ref}`"


def _services_autorises(carrier: str, eid: str, account: str) -> list:
    """The services the lender's Google or Microsoft account has authorized."""
    meta = next((r.get("meta") or {} for r in credentials_store.list_accounts(
        credentials_store.MEMBER, eid, carrier) if r["account"] == account), {})
    return services_accordes.services_autorises(carrier, meta)


def _services_du_pret(inp: LendInstanceInput, carrier: str, eid: str, account: str) -> list:
    """The services a loan of a Google or Microsoft account lends: `services` as given,
    otherwise the service card named, otherwise those the account authorized TODAY —
    each one a service of this account that it has authorized. Frozen in the loan: a
    service authorized later is not lent until the lender says so."""
    autorises = _services_autorises(carrier, eid, account)
    label = providers.REGISTRY[carrier].label
    if inp.services is not None:
        demandes = list(dict.fromkeys(inp.services))
    elif inp.connector != carrier:
        demandes = [inp.connector]
    else:
        demandes = autorises
    if not demandes:
        raise AuthzDenied(400, "no_service_to_lend",
                          f"Your {label} `{account}` lends no service this way: name the "
                          "services in `services` (or revoke=true to take the loan back). "
                          "Nothing was lent.")
    connus = services_accordes.services_du_porteur(carrier)
    inconnus = [x for x in demandes if x not in connus]
    if inconnus:
        raise AuthzDenied(400, "unknown_service",
                          f"{', '.join(f'`{x}`' for x in inconnus)}: not a service of a "
                          f"{label} account (one of {', '.join(connus)}). Nothing was lent.")
    manquants = [x for x in demandes if x not in autorises]
    if manquants:
        raise AuthzDenied(400, "service_not_authorized",
                          f"Your {label} `{account}` has not authorized "
                          f"{', '.join(f'`{x}`' for x in manquants)}: connect it from its "
                          "card first — a loan lends only what the account authorized. "
                          "Nothing was lent.")
    return [x for x in connus if x in demandes]


def _etat_du_pret(org: int, connector: str, eid: str, account: str, side: list,
                  par_cible: dict) -> dict:
    """The loan state of a row (`LoanState`) — what the lend gesture returns and what
    reading it serves: one projection, so the screen reads the same thing both ways."""
    etat = {"connector": connector, "account": account,
            "lent_to": [s[len("user:"):] for s in side if s.startswith("user:")],
            "lent_to_groups": [int(s[len("group:"):]) for s in side
                               if s.startswith("group:") and s[len("group:"):].isdigit()],
            "lent_to_org": f"org:{org}" in side}
    if connector in access.PORTEURS_A_SERVICES:
        etat["services_by_borrower"] = {s: list(par_cible.get(s, [])) for s in side}
        etat["services_lendable"] = _services_autorises(connector, eid, account)
        etat["service_labels"] = {
            x: providers.REGISTRY[x].label or x
            for x in services_accordes.services_du_porteur(connector)}
    return etat


def _active_org(ctx: ResolvedCtx, what: str) -> int:
    org = access.current_org(ctx.sub)
    if org is None:
        raise AuthzDenied(400, "no_active_org", f"No active org — unable to {what}.")
    return org


def _get_loan_state(ctx: ResolvedCtx, inp: LoanStateInput) -> dict:
    if providers.connector_for_provider(inp.connector) is None:
        raise AuthzDenied(400, "unknown_connector", f"Unknown connector `{inp.connector}`."
                          + providers.refus_renomme(inp.connector))
    connector = providers.credential_provider(inp.connector)
    org = _active_org(ctx, "read a loan")
    eid = credentials_store.member_id(org, ctx.sub)
    account = _lent_account(org, ctx.sub, connector, inp.account)
    _, side = credentials_store.get_instance_sharing(
        credentials_store.MEMBER, eid, connector, account)
    par_cible = (credentials_store.services_pretes(
        credentials_store.MEMBER, eid, connector, account)
        if connector in access.PORTEURS_A_SERVICES else {})
    return _etat_du_pret(org, connector, eid, account, list(side or []), par_cible)


def _lend_instance(ctx: ResolvedCtx, inp: LendInstanceInput) -> dict:
    if providers.connector_for_provider(inp.connector) is None:
        raise AuthzDenied(400, "unknown_connector", f"Unknown connector `{inp.connector}`."
                          + providers.refus_renomme(inp.connector))
    if (inp.to is not None) + (inp.to_group is not None) + bool(inp.to_org) != 1:
        raise AuthzDenied(400, "one_borrower",
                          "Name exactly one borrower: `to` (a member's email or sub), "
                          "`to_group` (a team of your current org) or `to_org=true` (your "
                          "whole current org).")
    # A service card owns no vault row: the loan is the carrier's (`credential_of`).
    connector = providers.credential_provider(inp.connector)
    org = _active_org(ctx, "lend an instance")
    if not inp.revoke and cle_perso.interdite(org, connector):
        raise AuthzDenied(403, cle_perso.CODE, cle_perso.texte_refus(
            org, connector, cle_perso.AU_PRET))
    if inp.to is not None:
        to = _borrower_sub(org, inp.to)
        if to == ctx.sub:
            raise AuthzDenied(400, "self_lend", "Lending to yourself makes no sense.")
        entry = f"user:{to}"
    elif inp.to_org:
        if not inp.revoke and not _shared_loan_served(connector):
            raise AuthzDenied(400, "org_loan_not_served",
                              f"A `{connector}` instance cannot be lent to the whole org: "
                              "nothing would serve it to its members. Lend it to each "
                              "member with `to` (they use it with `_instance=`). Nothing "
                              "was lent.")
        entry = f"org:{org}"
    else:
        if not inp.revoke and not _shared_loan_served(connector):
            raise AuthzDenied(400, "team_loan_not_served",
                              f"A `{connector}` instance cannot be lent to a team: nothing "
                              "would serve it to the team's members. Lend it to each "
                              "member with `to` (they use it with `_instance=`). Nothing "
                              "was lent.")
        # A loan to a team never crosses orgs: the team must belong to the key's org.
        g = group_store.get_group(inp.to_group)
        if g is None or int(g["org_id"]) != int(org):
            raise AuthzDenied(404, "unknown_group",
                              f"No team #{inp.to_group} in your current org.")
        entry = f"group:{inp.to_group}"
    eid = credentials_store.member_id(org, ctx.sub)
    account = _lent_account(org, ctx.sub, connector, inp.account)
    _, side = credentials_store.get_instance_sharing(
        credentials_store.MEMBER, eid, connector, account)
    side = list(side or [])
    avant = list(side)
    a_services = connector in access.PORTEURS_A_SERVICES
    if inp.services is not None and not a_services:
        raise AuthzDenied(400, "services_not_applicable",
                          f"`services` applies to a Google or Microsoft account only: a "
                          f"`{connector}` key is lent whole. Nothing was lent.")
    par_cible = (credentials_store.services_pretes(
        credentials_store.MEMBER, eid, connector, account) if a_services else {})
    services_avant = dict(par_cible)
    if inp.revoke:
        side = [s for s in side if s != entry]
        par_cible.pop(entry, None)
    else:
        if entry not in side:
            side.append(entry)
        if a_services:
            par_cible[entry] = _services_du_pret(inp, connector, eid, account)
    # Only an effective change is journaled: a repeated gesture changes nobody's access.
    # Changing the services of a loan in place is a new lend to that borrower.
    journal = ([{"actor_sub": ctx.sub, "org_id": org, "target": entry,
                 "action": loan_events.REVOKE if inp.revoke else loan_events.LEND}]
               if side != avant or par_cible != services_avant else [])
    ok = credentials_store.set_instance_sharing(
        credentials_store.MEMBER, eid, connector, account, share_side=side, journal=journal,
        **({"share_services": par_cible} if a_services else {}))
    if not ok:   # removed between the listing and this write
        raise AuthzDenied(404, "no_instance",
                          f"No `{connector}` instance set in this org — "
                          f"nothing to lend (configure your key first).")
    services = par_cible.get(entry, []) if a_services else None
    note = None
    if not inp.revoke and (a_services or services_accordes.services_du_porteur(connector)):
        label = providers.REGISTRY[connector].label
        note = (f"This lends your {label} `{account}` for {', '.join(services)} only: a "
                "service it authorizes later is not lent until you add it (`services`)."
                if a_services else
                f"This lends your {label} `{account}` whole, with every card that borrows "
                f"it ({', '.join(services_accordes.services_du_porteur(connector))}).")
    if not inp.revoke:
        note = (f"{note} " if note else "") + (
            "The borrower uses it only by naming it: "
            f"{_designation(connector, eid, account, inp.to is None)}.")
    return {**_etat_du_pret(org, connector, eid, account, side, par_cible),
            "ok": True, "note": note, "revoked": inp.revoke, "services": services}


CAPABILITIES += [
    Capability(
        key="connectors.lend_instance", handler=_lend_instance, Input=LendInstanceInput,
        authz=SUB_ONLY, Output=LendInstanceResult,
        description=("Lend YOUR connector instance (your key in your current org) to a "
                     "peer, who uses it by pinning it (instance=) — `to` = the email or the "
                     "sub of a member of your organization — or to a team of your org — "
                     "`to_group` = its id — or to your whole org — `to_org=true`; their "
                     "members then use it only by naming it (`_account=`), never by "
                     "default (a connector nothing serves to a team or to the org is "
                     "refused, and so is a personal key your org forbids for this "
                     "connector). `account` is optional "
                     "when you have only one. A Google or Microsoft account is lent "
                     "for a LIST of services (`services`; default: the card named, e.g. "
                     "`google_ads`, or what the account authorized today) — a service "
                     "authorized later is never lent implicitly; lend again with "
                     "`services` to extend or reduce; "
                     "`revoke=true` takes it back. The response is the loan's complete state "
                     "afterwards (`lent_to`, `lent_to_groups`, `lent_to_org`); every lend "
                     "and revoke is journaled. You only ever share your OWN key; the "
                     "borrower operates under THEIR own org context (ADR 0044 share_side)."),
        rest=RestBinding("POST", "/api/me/connectors/{connector}/lend", {"connector": "connector"}),
    ),
    Capability(
        key="connectors.lend_state", handler=_get_loan_state, Input=LoanStateInput,
        authz=SUB_ONLY, Output=LoanState,
        description=("Who YOUR connector instance (your key in your current org) is lent "
                     "to — members (`lent_to`), teams (`lent_to_groups`), the whole org "
                     "(`lent_to_org`) — and, for a Google or Microsoft account, the "
                     "services lent to each (`services_by_borrower`) and those the "
                     "account authorized, which a loan may lend (`services_lendable`), "
                     "with their names (`service_labels`). "
                     "The state the lend gesture returns, read without changing it."),
        rest=RestBinding("GET", "/api/me/connectors/{connector}/lend", {"connector": "connector"}),
    ),
]


# ── The loans of an org, seen by its admin (signal 1397) ─────────────────────
class OrgLoansInput(BaseModel):
    org_id: int


class OrgLoan(BaseModel):
    """One ACTIVE loan: an account a member of the org lends, and to whom."""
    lender_sub: str
    lender: str                             # their e-mail (else their name)
    connector: str                          # the vault row lent — the carrier (`google`)
    account: str = ""                       # `""` = single-key connector
    to: Literal["user", "group", "org"]
    to_sub: Optional[str] = None            # to=user
    to_group: Optional[int] = None          # to=group
    to_label: str                           # the borrower, as a person reads it
    # Google/Microsoft account: the services lent to this borrower; null for a key that
    # has no services of its own (lent whole).
    services: Optional[list[str]] = None
    # ⚠️ The date of the LAST journaled lend to this borrower; null = lent before the
    # loan journal existed, its date is not known — never "today", never a guess.
    since: Optional[str] = None


class OrgLoans(BaseModel):
    org_id: int
    loans: list[OrgLoan]
    count: int


def _list_org_loans(ctx: ResolvedCtx, inp: OrgLoansInput) -> dict:
    org = int(inp.org_id)
    rows = credentials_store.prets_de_l_org(org)
    lignes = [(credentials_store.MEMBER, r["entity_id"], r["connector"], r["account"] or "")
              for r in rows]
    poses = loan_events.dernieres_poses(lignes)
    entries = [(ligne, r["entity_id"].split(":", 1)[1], cible, r["share_services"] or {})
               for ligne, r in zip(lignes, rows) for cible in (r["share_side"] or [])]
    libelles = access.libelles_preteurs(
        [preteur for _, preteur, _, _ in entries]
        + [c[len("user:"):] for _, _, c, _ in entries if c.startswith("user:")])
    groupes: dict = {}
    loans = []
    for ligne, preteur, cible, par_cible in entries:
        kind, _, ident = cible.partition(":")
        loan = {"lender_sub": preteur, "lender": libelles[preteur], "connector": ligne[2],
                "account": ligne[3], "to": kind,
                "services": (list(par_cible.get(cible, []))
                             if ligne[2] in access.PORTEURS_A_SERVICES else None)}
        if kind == "user":
            loan.update(to_sub=ident, to_label=libelles[ident])
        elif kind == "group":
            if ident not in groupes:
                groupes[ident] = group_store.get_group(int(ident)) or {}
            loan.update(to_group=int(ident),
                        to_label=groupes[ident].get("name") or f"team #{ident}")
        else:
            loan.update(to_label="the whole org")
        at = poses.get(ligne + (cible,))
        loan["since"] = at.isoformat() if at is not None else None
        loans.append(loan)
    return {"org_id": org, "loans": loans, "count": len(loans)}


CAPABILITIES += [
    Capability(
        key="org.loans.list", handler=_list_org_loans, Input=OrgLoansInput,
        authz=ORG_ADMIN_OF("org_id"), Output=OrgLoans,
        description=("The ACTIVE loans of an org you administer: which member lends "
                     "which account, to whom (a member, a team, the whole org), since "
                     "when (`since` null = lent before the loan journal existed), and for "
                     "a Google or Microsoft account the services lent."),
        rest=RestBinding("GET", "/api/orgs/{id}/loans", {"id": "org_id"}),
    ),
]
