"""Change the OWNER of an account (#1499, oto-core's rule: « changing its owner requires
managing it before and after; making it personal is reserved to the administrator »).

The account stays the same object (`db/transfert_de_compte.transferer`: its vault row
changes entity in place, the secret is never read, the instance keeps its id). The RIGHTS
are judged here, before anything is written:

1. the caller manages the account now (`niveau(...) == "manage"`);
2. towards a team or the org: the caller would still manage it afterwards — the level
   computed on the account as it would be (new owner, same rules);
3. towards a PERSON (from a team or the org, or from one person to another): an admin of
   the account's org only (`owner_change_forbidden`). That is the whole condition: no role
   reaches a person's own account, so the admin who gives it no longer manages it — the
   new owner does;
4. the new owner is in the account's org (a member, a team of the org, the org itself);
5. towards a team or the org, only a connector the cascade reads at those rungs
   (`providers.is_org_shareable`, else `not_shareable`).
"""
from __future__ import annotations

from dataclasses import replace
from pydantic import BaseModel

from ... import access, credentials_store, providers
from ...db import transfert_de_compte
from .._authz import SUB_ONLY
from .._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES
from . import sharing

class TransferInput(sharing.RuleSubjectInput):
    """The account (`instance_id`) and its new owner: exactly one of `to`, `to_group`,
    `to_org`."""


class TransferResult(BaseModel):
    instance_id: int
    id: str                                 # `inst:<n>`, unchanged
    owner_before: str                       # `<member|group|org>:<id>`
    owner_after: str
    rules_removed: int                      # dropped because the account became personal
    default_dropped: bool                   # the new owner already had its default


def _nouveau_proprietaire(inp: TransferInput, compte) -> tuple[str, str]:
    """`(owner_type, owner_id)` of the new owner, inside the account's org."""
    sharing.un_seul_sujet(inp)
    org = int(compte.org_id)
    if inp.to_org:
        return "org", str(org)
    if inp.to_group is not None:
        return "group", str(sharing.equipe_de_l_org(org, inp.to_group))
    sub = sharing.personne_de_l_org(org, inp.to)
    return credentials_store.MEMBER, credentials_store.member_id(org, sub)


def _apres(compte, vers_type: str, vers_id: str):
    """The account as a transfer to a team or to the org leaves it: its new owner, its
    rules unchanged (only an account that becomes personal loses some)."""
    return replace(compte, owner_type=vers_type, owner_id=vers_id)


def _transfer(ctx: ResolvedCtx, inp: TransferInput) -> dict:
    appelant, compte = sharing.gere(ctx, inp.instance_id)
    vers_type, vers_id = _nouveau_proprietaire(inp, compte)
    if vers_type != credentials_store.MEMBER and not providers.is_org_shareable(
            compte.connector):
        raise AuthzDenied(400, "not_shareable",
                          f"A `{compte.connector}` account cannot belong to a team or to the "
                          "org: nothing would serve it there. Nothing was done.")
    if vers_type == credentials_store.MEMBER and not appelant.admin_de_l_org(compte.org_id):
        raise AuthzDenied(403, "owner_change_forbidden",
                          f"Only an admin of the org gives the account `{compte.libelle}` "
                          "to a person. Nothing was done.")
    if vers_type != credentials_store.MEMBER and not access.suffit(
            access.niveau(appelant, _apres(compte, vers_type, vers_id)), "manage"):
        raise AuthzDenied(403, "insufficient_level",
                          f"You would no longer manage the account `{compte.libelle}` "
                          "once transferred there: its owner can change only between "
                          "owners you manage. Nothing was done.")
    try:
        out = transfert_de_compte.transferer(compte.instance_id, vers_type=vers_type,
                                             vers_id=vers_id, acteur=ctx.sub)
    except transfert_de_compte.TransfertRefuse as e:
        raise _refus_de_la_base(e) from e
    return {**out, "id": f"inst:{out['instance_id']}"}


def _refus_de_la_base(e: "transfert_de_compte.TransfertRefuse") -> AuthzDenied:
    """A refusal of the base, served under its declared code. An unknown code is a defect
    of the base module: it raises, never a status guessed."""
    message = f"{e} Nothing was done."
    if e.code == "not_found":
        return AuthzDenied(404, "not_found", message)
    if e.code == "not_transferable":
        return AuthzDenied(400, "not_transferable", message)
    if e.code == "same_owner":
        return AuthzDenied(409, "same_owner", message)
    if e.code == "account_name_taken":
        return AuthzDenied(409, "account_name_taken", message)
    raise RuntimeError(f"transfer refused with an undeclared code {e.code!r}") from e


CAPABILITIES += [
    Capability(
        key="connectors.instances.transfer", handler=_transfer, Input=TransferInput,
        Output=TransferResult, authz=SUB_ONLY,
        errors=sharing._E_COMPTE + (
            DeclaredError(400, "one_subject", "not exactly one of `to`, `to_group`, `to_org`"),
            DeclaredError(404, "unknown_group", "the team is not one of the account's org"),
            DeclaredError(404, "not_an_org_member",
                          "the person is not a member of the account's org"),
            DeclaredError(400, "not_shareable",
                          "towards a team or the org, a connector those rungs never read"),
            DeclaredError(403, "owner_change_forbidden",
                          "towards a person, by someone who is not an admin of the org"),
            DeclaredError(404, "not_found", "the account disappeared meanwhile"),
            DeclaredError(400, "not_transferable",
                          "a hosted messaging account, which has no key of its own"),
            DeclaredError(409, "same_owner", "the account already belongs there"),
            DeclaredError(409, "account_name_taken",
                          "the new owner already has an account of that name")),
        description=("Change the OWNER of an account you manage (`instance_id`): a member "
                     "(`to`, email or sub), a team (`to_group`) or the whole org "
                     "(`to_org=true`) of the account's org. Towards a team or the org you "
                     "must still manage it afterwards; giving it to a person is reserved to "
                     "an org admin (the person then manages it). The "
                     "account keeps its id, its secret and its rules — except, when it "
                     "becomes personal, the org's rule, the rule naming its new owner and "
                     "every `manage` rule (each removal journaled). "
                     "Journaled."),
        rest=RestBinding("POST", sharing._ROUTE + "/transfer"),
    ),
]
