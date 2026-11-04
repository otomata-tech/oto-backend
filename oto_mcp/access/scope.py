"""Who acts, and in what context (ADR 0023/0038) — the LOW layer of the package.

Three questions, one place for each:

- **the platform role** of the sub (`member` < `admin` < `super_admin`);
- **the call context** — EFFECTIVE org, team and project, resolved
  `call token ?? view ?? home`; `current_org` is the single seam
  through which everything that scopes an action passes (credentials, visibility,
  entitlements, redaction);
- **what the active project PINS** — identity, instance, table slot.

This module depends on no other `access` submodule: all the others
start from it. A function that needs to know "for whom, under which
org" calls it — it never re-reads the home (`org_store.get_active_org`)
directly, cf. `tests/test_org_seam_tripwire.py`.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import db, group_store, org_store, session_org
from ..auth.hooks import current_user_sub_from_token

logger = logging.getLogger(__name__)


# Platform roles, from weakest to strongest: `member` (non-admin default) <
# `admin` (operator: supervision without mass escalation) < `super_admin`
# (all-powerful: org/group escalation, roles, keys, tokens, third-party orgs).
# `guest` removed (2026-06-15) — it was an alias with no effect, migrated to `member`.
MEMBER = "member"
ADMIN = "admin"
SUPER_ADMIN = "super_admin"
ROLES = (MEMBER, ADMIN, SUPER_ADMIN)


def role_effectif(sub: Optional[str], role: Optional[str]) -> str:
    """THE platform role of `sub`, from a `users.role` value ALREADY READ — the single
    point every platform predicate derives from (`get_user_role`, `is_operator_role`,
    and a caller that read the column in its own SQL, e.g. `run_thread`).

    Order: `OTO_MCP_ADMIN_SUB` override, then validation against `ROLES`, then the
    delegation bound (`verrou_org.role_plateforme`): under a runner job's delegation
    token the bearer is not the operator — `admin`/`super_admin` read `member` in
    `enforce` (`OTO_VERROU_ROLES_PLATEFORME`). The bound comes LAST, so the bootstrap
    sub does not escape it."""
    from .. import verrou_org
    admin_sub = os.environ.get("OTO_MCP_ADMIN_SUB")
    if admin_sub and sub == admin_sub:
        role = SUPER_ADMIN
    role = role if role in ROLES else MEMBER
    return verrou_org.role_plateforme(sub, role)


def get_user_role(sub: str) -> str:
    """Effective role of the user — env override > DB > default member, bounded under
    a delegation token (`role_effectif`).

    The `OTO_MCP_ADMIN_SUB` bootstrap forces **super_admin** (the all-powerful)
    — it is the platform owner's sub."""
    admin_sub = os.environ.get("OTO_MCP_ADMIN_SUB")
    if admin_sub and sub == admin_sub:
        return role_effectif(sub, SUPER_ADMIN)
    user = db.get_user(sub)
    return role_effectif(sub, (user or {}).get("role") or MEMBER)


def is_super_admin(sub: str) -> bool:
    """All-powerful: org/group escalation, platform roles, keys, tokens,
    writes on third-party orgs."""
    return get_user_role(sub) == SUPER_ADMIN


def is_platform_operator(sub: str) -> bool:
    """Platform operator = `admin` (supervision) OR `super_admin`. A notch of
    visibility/supervision, WITHOUT the mass escalation reserved for super_admin."""
    return get_user_role(sub) in (ADMIN, SUPER_ADMIN)


def is_operator_role(sub: str, role: Optional[str]) -> bool:
    """SAME predicate as `is_platform_operator`, on a `role` ALREADY IN HAND (column
    `users.role`) — for a caller that has already read the row (e.g. an org's member
    list, `capabilities/orgs/reads.py::_members`, oto#270 follow-up) and must not
    redo a `db.get_user` per member for this one verdict. Same
    `OTO_MCP_ADMIN_SUB` override, same fallback on `role` (no validation against `ROLES`:
    a role outside the enumeration matches neither `ADMIN` nor `SUPER_ADMIN`, hence `False`,
    like `get_user_role` which would bring it back to `MEMBER`)."""
    return role_effectif(sub, role) in (ADMIN, SUPER_ADMIN)


def current_org(sub: str | None) -> Optional[int]:
    """Org under which Claude ACTS for the current `sub` — the **single seam** of
    org resolution (ADR 0023, amends 0015).

    Passage point for EVERYTHING that scopes an action on the org (credentials,
    visibility, entitlements, redaction). Today (rung R0) =
    the persisted org (`org_store.get_active_org`, which becomes the "home org").

    Resolves `call token ?? run org ?? view ?? home` (ADR 0038, amends
    0023; "run org" stage added on 30/08/2026, #639):
    - **call token** (MCP) — `_org=`/`_project=`/`_group=` set already guarded by
      the axes/adapters (per-request contextvar); NO session state;
    - **run org** (MCP) — without a token, a call carrying `_run_id=` resolves into
      `runs.org_id`, set already guarded (membership) by the middleware
      (`run_org.pin_for_call`); an unknown run sets nothing;
    - **view org** (REST) — dashboard view-as, per-request contextvar
      set AFTER membership validation by the REST adapter;
    - otherwise → fall back to the persisted **home** (`org_store.get_active_org`).

    Token and view never coexist (token = MCP only, view =
    REST only). Keep this seam narrow: candidate credentials broker (ADR 0004)."""
    if sub is None:
        # ANONYMOUS MCP endpoint (`<slug>.mcp.oto.cx`, ADR 0032): no sub, but the project's
        # OWNER org is the resolution context (credentials/redaction).
        from .. import subdomain_project
        return subdomain_project.current_anon_org()
    # DELEGATION token (`verrou_org.py`): the org of its work, before everything
    # else — neither a call token, nor a run's org, nor the bearer's home. For the
    # bearer alone: a THIRD PARTY's org resolves through its ordinary path.
    from .. import verrou_org
    if (verrou := verrou_org.borne(sub, route="current_org", ecart=True)) is not None:
        return verrou.org_id
    # Subdomain-scoped endpoint ("1 oto per org"): pins the connection's org
    # BEFORE anything. Membership guard here (known sub) → a non-member
    # is ignored (home fallback, zero leak). Precedence ⇒ hard-lock: `oto_use_org`
    # (session override) cannot leave the subdomain's org.
    cand = session_org.current_subdomain_candidate()
    if cand is not None:
        from .. import roles
        if roles.is_org_member(sub, cand):
            return cand
    # Explicit call token (`_org=`, stateless-session model): set by the
    # capability adapter AFTER membership validation → returned as is. Takes precedence
    # over the session override (which does not survive stateless claude.ai).
    call = session_org.current_call_org()
    if call is not None:
        return call
    # The RUN's org (#639, 30/08/2026): without `_org=`, a call made INSIDE a run
    # resolves into the run's org (`runs.org_id`), not the sub's home — this is what
    # refused "unknown namespace" to 82 `data_write` over seven days and stamped
    # the journal outside the work's org (#630/#631). Set by the middleware (one
    # read per run, membership guarded, named refusal otherwise) — never re-read here:
    # the seam stays query-free.
    run_org = session_org.current_call_run_org()
    if run_org is not None:
        return run_org
    # The session WRISTBAND (`oto_use_org`, dict keyed by Mcp-Session-Id) is NO LONGER read
    # (ADR 0038 B3): claude.ai renews the session_id on every call (never re-read)
    # and a session_id recycled across accounts leaked the scope (#108). The scope
    # is carried by the call (`_org=`/`_project=`/`_group=`, above) or falls back to home.
    view = session_org.current_view_org()
    if view is not None:
        return None if view == 0 else view
    return org_store.get_active_org(sub)


# "Param not provided" sentinel — distinguishes "org=None" (personal, legitimate value)
# from "no explicit org → resolve via current_org". Used to compute a THIRD PARTY's
# state (admin sheet) against THEIR persisted org, without leaking the REQUESTER's
# view-as/session context (bug 2026-06-24: has_option(target) read the requester's
# org). The self path (/api/me) passes nothing → behaviour unchanged.
_UNSET: object = object()


# Tenants whose team members are never "teamless" (OPT-IN rule,
# declared by the instance): when nothing designates a team, `current_group` returns
# the sub's team in the resolved org instead of the org level. Off the list: unchanged.
ENV_EQUIPE_PAR_DEFAUT = "OTO_EQUIPE_PAR_DEFAUT_TENANTS"


def _tenants_equipe_par_defaut() -> frozenset[str]:
    brut = os.environ.get(ENV_EQUIPE_PAR_DEFAUT, "")
    return frozenset(t.strip() for t in brut.split(",") if t.strip())


def _equipe_par_defaut(sub: str, org: Optional[int]) -> Optional[int]:
    """The sub's team in `org` if the org belongs to an opt-in tenant, otherwise None
    (org level, the behaviour of old)."""
    if not org:
        return None
    tenants = _tenants_equipe_par_defaut()
    if not tenants or db.org_tenant_slug(org) not in tenants:
        return None
    return group_store.default_group_in_org(sub, org)


def current_group(sub: str | None) -> Optional[int]:
    """EFFECTIVE team (group) — mirror of `current_org` for the group axis
    (ADR 0038). Resolves `call token ?? view ?? home` while HOLDING
    the invariant "group ⊂ org": an ORG token/view **without** an explicit
    group ⇒ org level (None), never another org's home_group.

    Opt-in tenant (`OTO_EQUIPE_PAR_DEFAUT_TENANTS`): where the org level would be
    returned for lack of a designated team, we return the sub's team IN the resolved org
    (`_equipe_par_defaut`). `X-Oto-Group: 0` always keeps the org level.

    DELEGATION token (`verrou_org.py`): a team outside the work's org is
    never returned (org level), whatever its source — home or view.
    Without this, the key cascade (`resolve._group_fetch`) took the team key of another
    org of the bearer."""
    g = _current_group(sub)
    if g is None or sub is None:
        return g
    from .. import verrou_org
    v = verrou_org.courant()
    if v is None or str(sub) != v.sub:
        return g
    org_g = (group_store.get_group(g) or {}).get("org_id")
    hors = org_g is None or v.org_id is None or int(org_g) != int(v.org_id)
    if hors and verrou_org.borne(sub, route="current_group", ecart=True) is not None:
        return None
    return g


def _current_group(sub: str | None) -> Optional[int]:
    if sub is None:
        return None
    # Under a subdomain org lock: the group is returned ONLY if it ⊂ the pinned
    # org (otherwise None = org level) — hard-lock consistent with current_org.
    cand = session_org.current_subdomain_candidate()
    if cand is not None:
        from .. import roles
        if not roles.is_org_member(sub, cand):
            return None
        ag = group_store.get_active_group(sub)
        if ag is not None and (group_store.get_group(ag) or {}).get("org_id") == cand:
            return ag
        return _equipe_par_defaut(sub, cand)
    # `_group=` call token: already guarded when set (can_read_group + org co-set
    # by the axis, invariant by construction) → returned as is. The session WRISTBAND
    # (`oto_use_group`) is no longer read (ADR 0038 B3, same reason as current_org).
    call_g = session_org.current_call_group()
    if call_g is not None:
        return call_g
    vg = session_org.current_view_group()
    if vg is not None:
        return None if vg == 0 else vg
    view_org = session_org.current_view_org()
    if view_org is not None:
        return _equipe_par_defaut(sub, view_org)  # org view without a group
    ag = group_store.get_active_group(sub)  # home
    call_org = session_org.current_call_org()
    if call_org is None:
        call_org = session_org.current_call_run_org()
    if ag is None:
        return _equipe_par_defaut(sub, call_org if call_org is not None
                                  else current_org(sub))
    # Org token (`_org=`/`_project=`) — or the RUN's org (#639) — WITHOUT a group: the
    # home_group is returned only if it belongs to the pinned org (group ⊂
    # org invariant — never the home_group of ANOTHER org under a token or run org).
    if call_org is not None:
        g = group_store.get_group(ag)
        if not g or g.get("org_id") != call_org:
            return _equipe_par_defaut(sub, call_org)
    return ag


def current_project() -> Optional[int]:
    """Project of the current CALL (ADR 0038) = `_project=` token — set already guarded
    (`can_access` + derived org co-set) by the call axis. The session WRISTBAND
    (`oto_use_project`) is no longer read (B3b — same reason as org/group: claude.ai
    renews the session_id on every call, and a session_id recycled across accounts
    made the context inherited, #108). No "home" project: no token ⇒
    None (outside a project). Serves the project's PREMADE connector override, the slots
    (ADR 0035) and the `runs.project_id` freeze."""
    return session_org.current_call_project()


def current_user_sub_or_raise() -> str:
    sub = current_user_sub_from_token()
    if not sub:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message="Unauthenticated — no user identity on the request.",
        ))
    return sub


def project_pinned_identity(connector: str, project_id: Optional[int] = None) -> Optional[str]:
    """Identity (account) PINNED by the active project for `connector`, or None ⇒ resolution
    falls back to the user default. Reads the BINDING key `project_links.identity_ref`
    (ADR 0032 §4 amended, #57). Multiplicity: **a single** binding with an identity ⇒ it is pinned;
    **several** ⇒ None (ambiguous → the agent must specify `_account=` on the call). `project_id`
    omitted ⇒ session project (`current_project`). **Fail-soft**: any error ⇒ None
    (never crash a tool's resolution on this path)."""
    pid = current_project() if project_id is None else project_id
    if pid is None:
        return None
    try:
        pinned = [link.get("identity_ref")
                  for link in db.list_project_links(int(pid))
                  if link.get("target_type") == "connecteur"
                  and link.get("target_ref") == connector and link.get("identity_ref")]
        return pinned[0] if len(pinned) == 1 else None
    except Exception as e:
        logger.warning("project_pinned_identity fail-soft %s/%s: %s", pid, connector, e)
    return None


def project_declared_identities(connector: str, project_id: int) -> list[str]:
    """ALL the identities declared by a project for `connector` (ADR 0032 §4).

    Counterpart of `project_pinned_identity`, for the **`sub`-less** path (published
    MCP endpoint, ADR 0032): there, there is no one whose default account can be taken,
    and "ambiguous ⇒ None" is not workable — a project that declares LinkedIn AND
    WhatsApp under the same `unipile` connector is not ambiguous, it declares two channels.
    We therefore return the list, and it is up to the connector's module to choose on a criterion
    only it knows (the channel, here) — the specificity stays in its module.

    ⚠️ The result is NOT an authorization: these refs come from a project link,
    hence from what a member wrote. On a SHARED key (the platform's Unipile subscription
    addresses all accounts of all tenants), serving them as is
    would let a link name someone else's account. The caller MUST cross-check against the
    accounts actually attached to the owner org. Fail-soft: error ⇒ []."""
    try:
        return [str(link["identity_ref"])
                for link in db.list_project_links(int(project_id))
                if link.get("target_type") == "connecteur"
                and link.get("target_ref") == connector and link.get("identity_ref")]
    except Exception as e:  # noqa: BLE001
        logger.warning("project_declared_identities fail-soft %s/%s: %s",
                       project_id, connector, e)
        return []


def project_pinned_instance(provider: str, project_id: Optional[int] = None):
    """Connector instance BOUND by the call's project for `provider`
    (`project_links.config.instance_ref`, ADR 0038 B5), or None ⇒ normal cascade.
    **A single** binding with an instance ⇒ its ref (parsed); **several** ⇒ actionable
    McpError (acting identity at stake — never a silent choice: the agent
    specifies `_instance=`). Link reads are fail-soft (DB hiccup ⇒ None, like
    `project_pinned_identity`); a STORED ref that cannot be parsed raises (validated at link time —
    corruption = error, not a silent fallback)."""
    pid = current_project() if project_id is None else project_id
    if pid is None:
        return None
    try:
        refs = [(link.get("config") or {}).get("instance_ref")
                for link in db.list_project_links(int(pid))
                if link.get("target_type") == "connecteur"
                and link.get("target_ref") == provider
                and (link.get("config") or {}).get("instance_ref")]
    except Exception as e:
        logger.warning("project_pinned_instance fail-soft %s/%s: %s", pid, provider, e)
        return None
    if not refs:
        return None
    if len(refs) > 1:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"Project #{pid} binds SEVERAL `{provider}` instances — "
                     f"specify which one with `_instance=` ({', '.join(refs)}).")))
    from .. import instance_refs
    return instance_refs.parse_ref(refs[0])


# Slot addressing prefix (ADR 0035 B3): `slot:<name>` in a `namespace` argument
# of the data_* tools = "the table bound under this name by the active project".
SLOT_PREFIX = "slot:"


def resolve_datastore_ref(namespace: str) -> str:
    """Resolves a table reference: `slot:<name>` → the IDENTIFIER of the table
    bound by the active project; a bare name passes through unchanged (zero magic on literal
    names).

    The SINGLE source of this resolution, called by the `data_*` tools as by the
    datastore capabilities. It first lived only in `tools/datastore.py`,
    and that is what made the hole: a datastore capability received `slot:vivier`
    as a literal name and answered "table not found". On a destructive verb
    (`data_drop_column`), the failure is a happy one — but an agent working with slots
    sees sixteen refusals without understanding why, and would believe a table already clean
    if the refusal were not there."""
    if (isinstance(namespace, str)
            and namespace.strip().lower().startswith(SLOT_PREFIX)):
        return resolve_slot_tableau(namespace.strip()[len(SLOT_PREFIX):])
    return namespace


def resolve_slot_tableau(name: str) -> str:
    """Resolves a `tableau` slot against the ACTIVE project's bindings (ADR 0035 B3) →
    the IDENTIFIER of the bound table (#365 — never its name). **Server enforcement, never
    a fallback**: no active project, unbound slot, or dangling binding ⇒
    ACTIONABLE `McpError` — we never interpret `slot:x` as a literal name
    and we "never take the first table that comes along"."""
    from .. import slots as slots_mod
    try:
        name = slots_mod.normalize_name(name)
    except ValueError as e:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=f"invalid slot: {e}"))
    pid = current_project()
    if pid is None:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"`slot:{name}` requires a PROJECT (the name→instance binding lives "
                     "in the project, ADR 0035). Pass `project=<id>` on THIS call "
                     "(list: `oto_project op=list`) — or create a project and bind the slot "
                     f"(`oto_project op=link target_type=tableau … slot='{name}'`), or "
                     "pass an explicit `namespace`.")))
    links = db.list_project_links(int(pid))
    match = [l for l in links
             if l.get("target_type") == "tableau" and l.get("slot") == name]
    if not match:
        bound = sorted(l["slot"] for l in links
                       if l.get("target_type") == "tableau" and l.get("slot"))
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"the active project (#{pid}) binds no table slot `{name}`. "
                     + (f"Bound slots: {', '.join(bound)}. " if bound else
                        "No table slot bound in this project. ")
                     + f"Bind it: `oto_project op=link project_id={pid} "
                       f"target_type=tableau target_ref=<id> slot='{name}'`.")))
    # ⚠️ **The IDENTIFIER, never the name (#365).** `slot:` is how
    # procedures address "this project's table" without a hard-coded name: everything goes through
    # here. We used to return the NAME of the bound table, which the store then resolved in the
    # CALLER's scope — their personal "vivier" thus took precedence over the "vivier" the
    # project bound, and the procedure wrote to the namesake, without an error. The
    # binding designates a precise table: we return its identifier (`datastore_id`,
    # resolved in the PROJECT OWNER's scope, `db.list_project_links`).
    lien = match[0]
    if lien.get("datastore_ambigu"):
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"slot `{name}` of project #{pid} designates its table by the NAME "
                     f"“{lien.get('target_ref')}”, which several tables carry in "
                     "the project's scope — nothing is served. Re-bind it by identifier "
                     f"(`oto_project op=link project_id={pid} target_type=tableau "
                     f"target_ref=<id> slot='{name}'`).")))
    ns_id = lien.get("datastore_id")
    if ns_id is None:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"slot `{name}` of project #{pid} points to a table that no longer "
                     f"resolves (ref `{lien.get('target_ref')}`) — re-bind it to an "
                     "existing table (`oto_project op=link`).")))
    return str(int(ns_id))


def org_d_appartenance(sub: Optional[str], org: Optional[int]) -> Optional[int]:
    """The org of the call whose MEMBER `sub` is: the call's org, except when `_project=`
    co-set the org of a shared project she is not a member of (`session_org.
    current_call_hors_org`) — then None. What reads it: the org rung of the cascade and
    the org's platform rights (#1499, D1: no account nor right of another org), the
    scope seams of lists (`ownership.owner_de_portee`). Zero queries."""
    hors = session_org.current_call_hors_org()
    if hors is not None and sub is not None and org is not None and hors == (sub, int(org)):
        return None
    return org

