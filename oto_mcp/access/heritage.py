"""The keys of a SHARED project: what its beneficiary can reach (#480).

**The rule — Alexis's ruling of 2026-09-23 (option A).** Someone a project is
shared with works in it **with their own keys**. The owner's keys
(those of their org, and of their team for a team project) are lent to them only
if the sharer **explicitly granted it**, at share time, and never beyond the sharer's
own rights. "Iso whether the share goes to a member, a team or an org": the
same rule, the same parameter (`credentials`) and the same behavior for all three
beneficiary types — nothing here looks at which type of grant the caller came in through.

**The hole it closes.** `_project=` co-sets the owning org as the call's context
(`call_axes._pin_project`), and the ORG rung of the cascade was guarded by no
membership: a beneficiary outside that org acted under its org keys,
without anyone having decided it. The team rung was already guarded
(`can_read_group` at pin time); the org one was not.

**What "their own keys" means**, under `_project=` of an org they are not a
member of:
- their member key in the project's org, if they knowingly set it there;
- otherwise their personal key set in another of their orgs — the personal org first,
  else the most recent (the cross-org instance of #172, extended here to every
  personal-key connector, multi-account included: same `sub`, zero impersonation);
- their tenant and their own platform accesses — never those the platform grants
  to the project's org (`org:<id>`), which are that org's rights.

**Inheritance** (`credentials="inherit"` at share time) is an edge of the grants
chain (ADR 0053) — `grants.resource_kind = 'project_credentials'`, issued by the
sharer (`grantor = user:<sub>`), received by the share's principal. It opens the
org rung (and the owning team's rung) **as long as the sharer can reach them
themselves**: the bound is re-read on every call, it does not freeze at share time.
Revoking = archiving the edge (`credentials="own"`, or `unshare`). The loan does not survive
the expiry of the share that carries it (otomata-tech/oto#39): the edge is only read for
a principal whose project share is alive.

**Cost.** The verdict is computed ONCE, when `_project=` is set (threadpool,
`call_axes`), and travels in a contextvar. The walker only reads it: zero queries
on the hot path, and a call outside a shared project pays nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .. import db, group_store, org_store, session_org

OWN = "own"
INHERIT = "inherit"
MODES = (OWN, INHERIT)

#: `grants.resource_kind` of inheritance — the extension point planned by 0053 §4.
RESOURCE_KIND = "project_credentials"


def ref_projet(project_id: int) -> str:
    """`grants.resource_id` of a project's inheritance. Prefixed: the other readers
    of `grants` filter by prefix (`platform:`, `tenant:`) and never cross it."""
    return f"project:{int(project_id)}"


@dataclass(frozen=True)
class ClesDuProjet:
    """What a caller reaches of the SHARED keys of a project's owner.

    Only exists when there is something to say: a caller who is an org member and,
    for a team project, a reader of the team, already reaches everything — no verdict."""
    sub: str
    projet: int
    org: int                          # project's credentials org (owner)
    membre: bool                      # the caller is a member of this org
    org_heritee: bool                 # org rung lent by inheritance
    groupe_herite: Optional[int]      # owning team's rung lent by inheritance


def contexte_du_projet(project_id: int) -> Optional[tuple[Optional[int], Optional[int]]]:
    """`(org, team)` whose keys a project resolves — None if the project does not exist.

    Org project: the org. Team project: its parent org + the team. Personal project:
    its context org (`context_org_id`), falling back to the owner's personal org for
    a legacy personal project without context (never `int(sub)`). SINGLE source: the
    `_project=` pin and the sharer's bound read the same one."""
    from .. import ownership
    owner = ownership.owner_of("project", str(project_id))
    if owner is None:
        return None
    owner_type, owner_id = owner
    if owner_type == "org":
        return int(owner_id), None
    if owner_type == "group":
        g = group_store.get_group(int(owner_id))
        return (g.get("org_id") if g else None), int(owner_id)
    if owner_type == "user":
        row = db.get_project_by_id(int(project_id))
        ctx = row.get("context_org_id") if row else None
        return (int(ctx) if ctx is not None else org_store.home_org(owner_id)), None
    return None, None


def evaluer(sub: str, project_id: int, org: Optional[int], groupe_proprio: Optional[int],
            groupe_pose: Optional[int]) -> Optional[ClesDuProjet]:
    """The caller's verdict on the project's keys (DB path, threadpool).

    `groupe_pose` = the team that the `_project=` pin co-set (None if the caller
    cannot read the owning team). Inheritance is read from the live edges that
    target one of the caller's principals — themselves, their orgs, their teams: this
    is what makes the rule iso across the three beneficiary types."""
    from .. import ownership, roles
    if org is None:
        return None
    membre = roles.is_org_member(sub, int(org))
    manque_groupe = groupe_proprio is not None and groupe_pose is None
    if membre and not manque_groupe:
        return None
    from ..db import grants as db_grants
    pairs = ownership.accessor_scope(sub).principal_pairs()
    aretes = [e for e in db_grants.edges_for(ref_projet(project_id), pairs)
              if e.get("revoked_at") is None and e.get("resource_kind") == RESOURCE_KIND]
    # The loan lives on a `grants` edge, which does not know the share's expiry
    # (otomata-tech/oto#39): it only holds for a principal whose project share is
    # STILL alive. Once expired, the share opens nothing — neither do the keys.
    # Read only if there is a loan to bound: without an edge, the pin pays nothing extra.
    vivants = (db.principals_with_live_grant("project", str(int(project_id)))
               if aretes else set())
    org_heritee, groupe_herite = False, None
    for e in aretes:
        if (e.get("grantee_kind"), str(e.get("grantee_id"))) not in vivants:
            continue
        partageur = e.get("grantor_id") if e.get("grantor_kind") == "user" else None
        if not partageur:
            continue
        # The bound, re-read on every pin: the sharer only lent what they reach
        # TODAY. If they have since left the org, their loan lapses without rewriting anything.
        if not membre and roles.is_org_member(partageur, int(org)):
            org_heritee = True
        if manque_groupe and roles.can_read_group(partageur, int(groupe_proprio)):
            groupe_herite = int(groupe_proprio)
    return ClesDuProjet(sub=sub, projet=int(project_id), org=int(org), membre=membre,
                        org_heritee=org_heritee, groupe_herite=groupe_herite)


def du_contexte(sub: Optional[str], org: Optional[int]) -> Optional[ClesDuProjet]:
    """The verdict of the CURRENT call, if it concerns THIS org and THIS sub — else None
    (call outside a shared project, anonymous, or walker queried for another context)."""
    v = session_org.current_call_cles()
    if v is None or sub is None or org is None or v.sub != sub or v.org != int(org):
        return None
    return v


def hors_org(cles: Optional[ClesDuProjet]) -> bool:
    """Is the caller acting in a project of an org they are NOT a member of?"""
    return cles is not None and not cles.membre


def org_partagee(org: Optional[int], cles: Optional[ClesDuProjet]) -> Optional[int]:
    """The org whose SHARED rights the caller can consume (org key, platform
    access granted to the org, the org's plan) — the context org, except for a
    beneficiary outside the org to whom nothing was lent: None."""
    if hors_org(cles) and not cles.org_heritee:  # type: ignore[union-attr]
        return None
    return org


def paliers_partages(org: Optional[int], groupe: Optional[int],
                     cles: Optional[ClesDuProjet]) -> list[tuple[str, int]]:
    """The shared tiers whose OWN accounts serve a member, nearest first — the cascade's
    rungs: the active team (`groupe`), the owning team a shared project lends, then the
    org (`org_partagee`). Pure: the caller reads the active team and the verdict."""
    out = [("group", int(groupe))] if groupe is not None else []
    herite = cles.groupe_herite if cles is not None else None
    if herite is not None and herite != groupe:
        out.append(("group", int(herite)))
    org_cles = org_partagee(org, cles)
    if org_cles is not None:
        out.append(("org", int(org_cles)))
    return out


def org_des_cles(sub: Optional[str], org: Optional[int]) -> Optional[int]:
    """`org_partagee` for THIS call's verdict: the org whose shared rights `sub`
    consumes in the context `org` — None for a beneficiary outside it, nothing lent."""
    return org_partagee(org, du_contexte(sub, org))


def org_du_perimetre(sub: Optional[str], org: Optional[int]) -> Optional[int]:
    """The org `sub` is a PRINCIPAL of in this call's context — the pendant of
    `org_partagee` for SCOPE (lists, resolving a table, access by id): the context
    org, except for a beneficiary OUTSIDE it (shared project, `_project=`): None.

    `_project=` co-sets the project's org as the call's org, without membership —
    on purpose (slots and the project's pinned identity resolve there). But every
    scope seam read that org as one of the caller's own: a non-member listed the
    org's whole table catalogue and read its tables by number. Inheritance
    (`org_heritee`) changes nothing here: it lends KEYS, never membership."""
    return None if hors_org(du_contexte(sub, org)) else org


def org_du_lien(sub: str, org: Optional[int]) -> Optional[int]:
    """The org for the "where to set your key" link of a refusal: not that of a shared
    project the caller is not a member of (a page they cannot open) — None targets their account."""
    return None if hors_org(du_contexte(sub, org)) else org


def instance_heritee(sub: str, ref) -> bool:
    """Is the instance BOUND by the project a key that inheritance lends to
    the caller? Otherwise the ordinary membership guard applies."""
    v = session_org.current_call_cles()
    if v is None or v.sub != sub:
        return False
    level = getattr(ref, "level", None)
    if level == "org":
        return v.org_heritee and getattr(ref, "org_id", None) == v.org
    if level == "group":
        return v.groupe_herite is not None and getattr(ref, "group_id", None) == v.groupe_herite
    return False


def indice_refus(sub: str, org: Optional[int], provider: str) -> str:
    """Suffix of a "no key" refusal when the caller works in a shared project
    whose owner HOLDS that key without lending it to them: tell them the two
    ways out, without letting them believe the key does not exist."""
    v = du_contexte(sub, org)
    if v is None:
        return ""
    proprio_org = (not v.membre and not v.org_heritee
                   and org_store.has_org_secret(v.org, provider))
    proprio_groupe = False
    if v.groupe_herite is None:
        groupe = (contexte_du_projet(v.projet) or (None, None))[1]
        proprio_groupe = groupe is not None and group_store.has_group_secret(groupe, provider)
    if not (proprio_org or proprio_groupe):
        return ""
    return (
        f"\nYou are working in project #{v.projet}, which is shared with you: its "
        f"`{provider}` keys belong to its owner and are not lent to you. Two "
        f"ways out — set your own `{provider}` key in your account (it follows you into "
        f"this project), or "
        f"ask the sharer to grant you inheritance: oto_resource(op='share', "
        f"resource_type='project', resource_id='{v.projet}', <your email|org_id|group_id>, "
        f"credentials='inherit')."
    )


# ── Writing: the share declares, the read shows ──────────────────────────

def peut_accorder(partageur: str, project_id: int) -> bool:
    """The bound at declaration: you only lend what you reach yourself — being
    a member of the org whose keys the project resolves."""
    from .. import roles
    ctx = contexte_du_projet(project_id)
    return bool(ctx and ctx[0] is not None and roles.is_org_member(partageur, int(ctx[0])))


def mode_de(project_id: int, principal_type: str, principal_id: str) -> str:
    """`inherit` if a LIVE edge lends the project's keys to this principal."""
    from ..db import grants as db_grants
    for e in db_grants.edges_for(ref_projet(project_id), [(principal_type, principal_id)]):
        if e.get("revoked_at") is None and e.get("resource_kind") == RESOURCE_KIND:
            return INHERIT
    return OWN


def modes_du_projet(project_id: int) -> dict[tuple[str, str], str]:
    """`{(principal_type, principal_id): 'inherit'}` of the live edges — the share
    read (`op=get`) in one query."""
    from ..db import grants as db_grants
    return {(e["grantee_kind"], str(e["grantee_id"])): INHERIT
            for e in db_grants.live_edges_for_resource(ref_projet(project_id))
            if e.get("resource_kind") == RESOURCE_KIND}


def declarer(project_id: int, principal_type: str, principal_id: str, mode: str,
             partageur: str) -> None:
    """Sets (`inherit`) or removes (`own`) the key loan to this principal. Idempotent:
    the previous live edge is archived before a new one is set — it is the CURRENT
    sharer who bounds the loan."""
    from ..db import grants as db_grants
    ref = ref_projet(project_id)
    db_grants.revoke_edges(ref, principal_type, str(principal_id))
    if mode == INHERIT:
        db_grants.insert_grant(
            resource_id=ref, resource_kind=RESOURCE_KIND,
            grantor_kind="user", grantor_id=partageur,
            grantee_kind=principal_type, grantee_id=str(principal_id),
            source="manual", created_by=partageur)
