"""Where an access level BITES — call guard `access_level_denied`, hidden tools, served ops.

`connectors.access_levels` says what a level covers; this module applies it, and only
when the level in force RESTRICTS (`full` filters nothing — not one query beyond the
reading of the setting and of the key's owner, not one byte of schema changed):

- **visibility** (`hidden_tools`, a layer of `session_visibility`): a tool none of whose
  scopes or ops the level covers leaves the toolbox. Display governance, fail-open like
  the other layers — the guard below is the barrier;
- **served schema** (`restricted_ops` + `restrict_schema`, applied per caller by the
  context middleware's `on_list_tools`): the ops of a multiplexed tool that the level
  does not cover leave the `op` enum;
- **call guard** (`require_covered`), **fail-closed**, at the two places a tool is run:
  the context middleware (direct call) and `tools/meta.executer_cible` (`oto_call` and
  the recipes), right after the activation guard — same seam, same context: the org and
  the team the call resolves under, axes and the run's org included. It receives the
  arguments, so it judges the `op`; a call without `op` is judged as the tool's default.
  An undeclared tool or op of a leveled connector is refused, never let through. It
  judges EVERY context whose key the call can be served from (`_judged_contexts`): the
  call's own, plus the team a shared project lends by inheritance, the owner of an
  instance pinned (`_instance=`) or bound by the project, and for a LENT key (which serves
  only designated, `access/prets.py`) its lender's org and the team it is lent to — each
  must cover the call.

Without a sub (local stdio) nothing is guarded, like activation: only the multi-user
surface is targeted.
"""
from __future__ import annotations

import copy
from typing import Awaitable, Callable, Optional

from mcp.types import INVALID_PARAMS, ErrorData
from starlette.concurrency import run_in_threadpool

from .. import access, call_axes, credentials_store, group_store, providers, session_org
from ..access import heritage
from ..mcp_errors import McpError
from . import access_levels

CODE = "access_level_denied"


def _context(sub: str) -> tuple[Optional[int], Optional[int]]:
    return access.current_org(sub), access.current_group(sub)


def _owner_context(ref) -> Optional[tuple[Optional[int], Optional[int]]]:
    """`(org, team)` whose level governs the vault row an instance ref names: a team's
    key is governed by that team (under its org), an org's or a member's key by its org.
    Tenant and platform rows carry no org: None (the call's context alone judges)."""
    level = getattr(ref, "level", None)
    if level == "group" and ref.group_id is not None:
        g = group_store.get_group(int(ref.group_id))
        return (g.get("org_id") if g else None), int(ref.group_id)
    if level in ("org", "member") and ref.org_id is not None:
        return int(ref.org_id), None
    return None


def _loan_contexts(sub: str, connector: str, carrier: str,
                   ref) -> list[tuple[Optional[int], Optional[int]]]:
    """The `(org, team)` of a LOAN the call designates: the team a pinned member row of
    another person is lent to (none when it is lent to her by name or to the whole org:
    the owner's org already judges it), or, without a pin, the lender's org and the
    borrowing team of every loan that `_account=` (or the project's pinned identity)
    names — Microsoft and the cascade's team and org rungs serve a loan only so. Judged
    before the resolution: a loan named like an own account is judged too, which can
    only restrict."""
    if ref is not None:
        if getattr(ref, "level", None) != "member" or ref.sub == sub:
            return []
        equipe = access.equipe_du_pret(sub, (
            credentials_store.MEMBER, credentials_store.member_id(ref.org_id, ref.sub),
            carrier, ref.account or ""))
        return [(int(ref.org_id), equipe["id"])] if equipe else []
    nom = (session_org.current_call_account() or access.project_pinned_identity(connector)
           or access.project_pinned_identity(carrier))
    if not nom:
        return []
    out = []
    for c in access.prets_de(sub, carrier):
        if c["account"] == nom:
            out.append((c["lender_org"], None))
            if c.get("via_group"):
                out.append((c["lender_org"], c["via_group"]["id"]))
    return out


def _judged_contexts(sub: str, connector: str) -> list[tuple[Optional[int], Optional[int]]]:
    """Every `(org, team)` whose level this call must satisfy: the call's context, AND the
    context that OWNS a key the call can be served from outside it — the owning team a
    shared project lends by inheritance (`groupe_herite`, walked by the cascade after the
    active team), and the instance pinned by `_instance=` or bound by the project (which
    `resolve_credential` serves before any cascade), and a designated loan
    (`_loan_contexts`). Without them, a team's restriction was crossed by serving that
    team's key under a context judged `full`."""
    org, group = _context(sub)
    out = [(org, group)]
    cles = heritage.du_contexte(sub, org)
    if cles is not None and cles.groupe_herite is not None:
        out.append((cles.org, cles.groupe_herite))
    carrier = providers.credential_provider(connector)
    ref = session_org.current_call_instance()
    if ref is None or getattr(ref, "connector", None) != carrier:
        ref = access.project_pinned_instance(carrier)
    owner = _owner_context(ref) if ref is not None else None
    if owner is not None:
        out.append(owner)
    out += _loan_contexts(sub, connector, carrier, ref)
    return list(dict.fromkeys(out))


# --- the call guard --------------------------------------------------------------

def refusal(decl: access_levels.AccessLevels, level: str, source: str, tool: str,
            op: Optional[str], org: Optional[int], group: Optional[int]) -> ErrorData:
    """The named refusal: the level, the tool and the op, what they require, who sets it."""
    exige = decl.required(tool, op)
    cible = f"`{tool}` op `{op}`" if op is not None else f"`{tool}`"
    par = (f"your team #{group}" if source == "group" else f"organization #{org}")
    besoin = (f"it requires {', '.join(exige)}" if exige
              else "nothing is declared for it at this level")
    if source == "group":
        geste = (f"a team lead lifts it (oto_connector_access_level(op='clear', "
                 f"scope='group', group_id={group}, name='{decl.connector}'))")
    else:
        geste = (f"an org admin widens it (oto_connector_access_level(op='set', "
                 f"scope='org', org_id={org}, name='{decl.connector}', level='full'))")
    if source == "default":
        par = f"default for `{decl.connector}` (organization #{org} sets none)"
    suite = ("a wider level then needs a new consent (a token obtained for a narrower "
             "level lacks its scopes)" if decl.widening_needs_consent else
             "the next call then claims the wider scopes, no new consent")
    return ErrorData(
        code=INVALID_PARAMS,
        message=(f"Refusal `{CODE}`: the access level `{level}` set by {par} for "
                 f"`{decl.connector}` does not cover {cible} — {besoin}. The call is "
                 f"served neither directly nor through oto_call. To allow it, {geste}; "
                 f"{suite}."),
        data={"code": CODE, "retryable": False, "connector": decl.connector,
              "level": level, "tool": tool, "op": op,
              "required_scopes": list(exige or ()), "scope": source,
              "org_id": org, "group_id": group})


def level_of_the_call(connector: str) -> str:
    """Sync: the level in force for `connector` under the call's own org and active team
    (the default level without a sub) — what the library's engine receives
    (`access_level=`), so that a service account claims the scopes of that level. The
    guard above has already judged every context the key can be served from."""
    sub = call_axes.current_user_sub_from_token()
    org, group = _context(sub) if sub else (None, None)
    return access_levels.effective_level(connector, org, group)


def _restricting_contexts(connector: str) -> list[tuple]:
    """Sync (threadpool): `(org, team, level, source)` for every judged context whose
    level RESTRICTS — empty when every context is `full`, or without a sub. The identity
    is read here, not in the loop: canonicalizing it may touch the database (alias
    drain), like `activation_gate._refus`."""
    sub = call_axes.current_user_sub_from_token()
    if not sub:
        return []
    out = []
    for org, group in _judged_contexts(sub, connector):
        level, source = access_levels.resolve(connector, org, group)
        if access_levels.restricts(level):
            out.append((org, group, level, source))
    return out


def _verdict(decl: access_levels.AccessLevels, tool: str, op: Optional[str],
             restricting: list[tuple]) -> Optional[ErrorData]:
    """Pure: the refusal for this call under the restricting contexts, or None."""
    for org, group, level, source in restricting:
        if not decl.covers(level, tool, op):
            return refusal(decl, level, source, tool, op, org, group)
    return None


def default_op(parameters: Optional[dict]) -> Optional[str]:
    """The default of the `op` parameter in a served input schema, or None."""
    prop = ((parameters or {}).get("properties") or {}).get(access_levels.OP_PARAM) or {}
    return prop.get("default")


async def require_covered(tool: str, args: dict,
                          tool_schema: Callable[[], Awaitable[Optional[dict]]]) -> None:
    """Raises `access_level_denied` if the level in force does not cover this call.
    Nothing is read for a tool of an unleveled connector or without a sub; the
    settings, for a leveled one; `tool_schema` (the tool's input schema) is awaited
    only for a multiplexed tool called WITHOUT `op` under a restricting level (its
    default op is then judged)."""
    decl = access_levels.for_tool(tool)
    if decl is None:
        return
    restricting = await run_in_threadpool(_restricting_contexts, decl.connector)
    if not restricting:
        return
    op = None
    if decl.is_op_tool(tool):
        op = (args[access_levels.OP_PARAM] if access_levels.OP_PARAM in args
              else default_op(await tool_schema()))
    refus = _verdict(decl, tool, op, restricting)
    if refus is not None:
        raise McpError(refus)


# --- visibility and served schema -------------------------------------------------

def _restricting(org: Optional[int], group: Optional[int]) -> dict:
    """`{connector: level}` for every leveled connector whose level restricts."""
    if not access_levels.DECLARED:
        return {}
    valeurs = access_levels.settings(org, group)
    out = {}
    for nom in access_levels.DECLARED:
        level = access_levels.resolve(nom, org, group, valeurs)[0]
        if access_levels.restricts(level):
            out[nom] = level
    return out


def hidden_tools(all_names: set[str], org: Optional[int],
                 group: Optional[int]) -> set[str]:
    """The tools of `all_names` that the level in force covers in NOTHING: a non-op tool
    whose scopes it lacks, a multiplexed tool none of whose ops it covers, a tool of a
    leveled connector that declares nothing."""
    niveaux = _restricting(org, group)
    if not niveaux:
        return set()
    caches = set()
    for n in all_names:
        decl = access_levels.for_tool(n)
        if decl is None or decl.connector not in niveaux:
            continue
        level = niveaux[decl.connector]
        if decl.is_op_tool(n):
            if not decl.covered_ops(level, n):
                caches.add(n)
        elif not decl.covers(level, n):
            caches.add(n)
    return caches


def restricted_ops(sub: Optional[str], names: set[str]) -> dict[str, frozenset[str]]:
    """`{tool: covered ops}` for the multiplexed tools of `names` whose level restricts,
    under the caller's org and active team. Sync (threadpool). Reads nothing without a
    sub or when `names` holds no multiplexed tool of a leveled connector — the listing
    of a toolbox without one stays free of any query."""
    if not sub:
        return {}
    concernes = {nom: [t for t in decl.tools if t in names and decl.is_op_tool(t)]
                 for nom, decl in access_levels.DECLARED.items()}
    concernes = {nom: outils for nom, outils in concernes.items() if outils}
    if not concernes:
        return {}
    niveaux = _restricting(*_context(sub))
    return {tool: access_levels.DECLARED[nom].covered_ops(niveaux[nom], tool)
            for nom, outils in concernes.items() if nom in niveaux
            for tool in outils}


def restrict_schema(parameters: Optional[dict], ops: frozenset[str]) -> dict:
    """A copy of the input schema whose `op` enum keeps only `ops`. A default outside
    `ops` is dropped and `op` becomes required: a call without it would otherwise run
    an op the level refuses."""
    params = copy.deepcopy(parameters) if isinstance(parameters, dict) else {}
    prop = (params.get("properties") or {}).get(access_levels.OP_PARAM)
    if not isinstance(prop, dict):
        return params
    for branche in [prop, *(prop.get("anyOf") or [])]:
        if isinstance(branche, dict) and isinstance(branche.get("enum"), list):
            branche["enum"] = [v for v in branche["enum"] if v in ops]
    if "default" in prop and prop["default"] not in ops:
        del prop["default"]
        requis = params.setdefault("required", [])
        if access_levels.OP_PARAM not in requis:
            requis.append(access_levels.OP_PARAM)
    return params
