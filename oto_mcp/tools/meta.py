"""Deferred-mode meta-tools — reach a tool without loading its schema.

`oto_tool_schema` returns a tool's input schema by name, `oto_call` runs it
(universal dispatch, ADR 0036). They are MCP-only by nature: they act on the
FastMCP instance itself. The member's toolbox (catalog, hide/unhide a
tool) is no longer here: those are capabilities, `capabilities/tools_me.py` (#429).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from fastmcp import Context, FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .. import (access, call_axes, calllog, db, deprecations, error_taxonomy, geste,
                guide_run, outils_du_travail, outils_retires, output_projection,
                redaction, run_org,
                session_org, tool_alias, verrou_org)
from ..access import credits as budget
from ..auth.hooks import current_user_sub_from_token
from ..connectors import access_level_gate, activation_gate
from ..connectors import health as connector_health
from ..tool_visibility import namespace_of

# Meta/spine tools not dispatchable via `oto_call` (ADR 0036 §4): already always visible,
# no point going through dispatch, and anti-loop (`oto_call` on itself).
# Mirror of `middleware.field_redaction._SPINE_SERVICES`.
_NON_DISPATCHABLE: frozenset[str] = frozenset({"oto", "run", "feedback", "data"})


def _refuser_si_retire(name: str) -> None:
    """A RETIRED name (`outils_retires`) is refused here with the SAME text as a direct call:
    it names the gesture that works. `name` is already canonical (tenant prefix lifted)."""
    retire = outils_retires.retrait(name)
    if retire is not None:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=retire.message))

logger = logging.getLogger(__name__)


def _tool_prefix() -> str:
    """The current tenant's tool prefix (`""` = canonical names).

    These tools take a NAME as an argument (like `oto_disable_tool`/`oto_enable_tool`,
    `capabilities/tools_me.py`): they are the only places where a name crosses a
    HANDLER instead of the protocol edge, hence the only ones the `ToolAliasMiddleware`
    does not cover. Without this reminder, a third-party tenant account
    read `acme_doc` in its list and was answered "Unknown tool" when passing it
    to `oto_tool_schema` — the catalog and the dispatch would have spoken two
    languages."""
    # `prefix_for` does not raise (in-memory registry, fail-open logged on its side):
    # only the IDENTITY failure could land here, and it bubbles up — serving our
    # canonical names to an account whose identity we do not know would hide it (#464).
    return tool_alias.prefix_for(current_user_sub_from_token())


def _require_sub() -> str:
    # An identity failure BUBBLES UP (the seam logs it with its reason, #464): only
    # a call really without a token is "unauthenticated".
    sub = current_user_sub_from_token()
    if not sub:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message="Auth required — these tools only work on the authenticated HTTP transport.",
        ))
    return sub


async def resoudre_outil(fastmcp, name: str):
    """FastMCP Tool object by name (or None), **including hidden/disabled** — we
    enumerate the parent `Provider`'s RAW catalog ("including disabled ones",
    fastmcp docstring). ⚠️ `list_tools(run_middleware=False)` is NOT enough: it
    still applies `apply_session_transforms` + the `is_enabled` filter → a tool
    hidden by THE SESSION's visibility (connector not activated at handshake,
    not selected) was not found at dispatch — the `oto_call`/
    `oto_tool_schema` escape hatch answered "Unknown tool" (#186, regression from the switch to
    fastmcp native visibility)."""
    from fastmcp.server.providers.base import Provider
    tools = await Provider.list_tools(fastmcp)
    for t in tools:
        if t.name == name:
            return t
    return None


# The keys of the record that BILL: they go on the target's row, and on it
# alone. The envelope row (`tool='oto_call'`) must not carry them — the same
# consumption written twice would be billed twice the day a consumer
# stopped filtering by tool name.
# The `found_*` counters (contacts of a FullEnrich job where a value of each kind
# was found) also BILL: they carry the per-result price.
# `apify_run_id` (the Apify run whose final cost the call records) BILLS too: it is
# the job the biller counts once — on the envelope it would name a second row for it.
_BILLING_TRACE_KEYS = ("quantity", "key_mode",
                       "found_work_emails", "found_personal_emails", "found_phones",
                       "apify_run_id")
_UNSET = object()


async def _trace_target_call(sub: Optional[str], name: str, args: dict, ok: bool,
                             error: Optional[str], duration_ms: int, *,
                             trace: Optional[dict] = None,
                             org_id: object = _UNSET,
                             run_id: Optional[str] = None,
                             call_uid: Optional[str] = None) -> None:
    """Logs the dispatched call UNDER THE TARGET NAME (ADR 0036 §5 / 0017): without this
    only `oto_call` shows up in `tool_calls` and the usage inventory goes blind
    to the latent catalog. Best-effort — never blocking.

    ⚠️ **Same argument factory as the middleware** (`calllog.truncated_args`): this
    row is served by the SAME surfaces (a call's sheet, a run's timeline),
    which both announce truncated and masked arguments. It stored the raw
    dictionary until 2026-09-01 — 40,159 rows in the database, of which only the 111
    with a value exceeding the announced bound. The TARGET name is the one that declares its
    secrets: that is the one we pass, never `oto_call`."""
    try:
        session_id = None
        try:
            from fastmcp.server.dependencies import get_context
            c = get_context()
            session_id = c.session_id
            # Same source as the middleware sink: the `_run_id=` token first (read
            # by the caller BEFORE the axes reset), the session stack next.
            run_id = run_id or await guide_run.active_run_id(c)
        # noqa: SILENT — declared debt: the indirect call trace disappears (#424, verdict C)
        except Exception:
            pass
        row = {
            "server": "oto", "kind": "mcp", "sub": sub, "tool": name,
            "args": calllog.truncated_args(args, tool=name),
            "ok": ok, "error": error, "duration_ms": duration_ms,
            "session_id": session_id, "run_id": run_id,
            # The org UNDER WHICH THE TARGET RESOLVED, read by `oto_call` before undoing
            # its axes — not the home org we would re-read afterwards.
            "org_id": access.current_org(sub) if org_id is _UNSET else org_id,
            # The target's own call: what its credit hold is keyed by.
            "call_uid": call_uid,
        }
        # The declared issuer (client + named token), same rule as the envelope
        # row: without it, a dispatch's target would have no issuer.
        calllog.poser_emetteur(row)
        # The same rule as the middleware sink: without it, a dispatch's target
        # had neither `key_mode` nor `quantity`, hence was never billed.
        # The closed list of keys poured into `args` lives in `server` (imported
        # at call time: the module is already loaded in production, and the tool
        # must not copy a second version of it).
        from .. import server as _server
        calllog.apply_call_trace(row, trace, _server._TRACED_ARGS)
        await asyncio.to_thread(db.insert_tool_call, row)
    except Exception:
        logger.warning("tracing oto_call → %s failed (non-blocking)", name, exc_info=True)


async def _resolve_tool(ctx: Context, name: str):
    return await resoudre_outil(ctx.fastmcp, name)


async def _parameters_of(tool) -> Optional[dict]:
    return getattr(tool, "parameters", None)


@dataclass
class IssueCible:
    """What a tool run by `executer_cible` returned.

    `ok`: the SERVED result (redaction applied), as a direct call would have
    returned it. Otherwise `message` is the SCRUBBED text (`error_taxonomy`), `code` its class
    (`quota_exhausted`, `upstream_timeout`…) and `retryable` what it says about it — enough to
    decide without re-reading the text."""
    ok: bool
    result: object = None
    message: Optional[str] = None
    code: Optional[str] = None
    retryable: bool = False
    # The org's redaction policy WITHHELD the whole result: `result` is the
    # withholding message, not data. A recipe refuses the page rather than writing.
    retenu: bool = False
    # What the target BILLED (`quantity` of its record, that of its
    # `tool_calls` row); None if the tool declares none. A recipe's ceiling reads it.
    quantity: Optional[float] = None


async def executer_cible(tool, sub: Optional[str], name: str, demande: str,
                         args: dict) -> IssueCible:
    """Runs the tool `name` (CANONICAL, already judged dispatchable) like `oto_call`:
    same guards, same log, same redaction. Shared by `oto_call` and the recipes
    (`recipes/moteur.py`), which call tools from the server, outside the protocol.

    Raises `McpError` on an unknown tool, invalid arguments or a guard refusal
    (activation, axis membership): those are the CALLER's faults. The TARGET's
    failure, on the other hand, is a result: `IssueCible(ok=False, …)`.

    `tool` is the already-resolved object (`resoudre_outil`): the caller says
    "unknown" itself in its own words."""
    call_axes.reject_legacy_axis_names(args, getattr(tool, "parameters", None))
    undo: list = []
    try:
        # The TARGET is the tool being called, for the platform-role bound: a hosted
        # agent keeps its bearer's platform role only while one of the `oto_admin_*` its
        # agent lists runs (`verrou_org.outils_permis`) — never `oto_call` itself.
        undo.append((verrou_org.lever_outil, verrou_org.poser_outil(name)))
        # Off the loop: the axes list reads the database (`docs/event-loop-perf.md`).
        for axis in await run_in_threadpool(call_axes.axes_for_call, name):
            if axis.param in args:
                undo.extend(await axis.pin_for(args.pop(axis.param), name))
        # The RUN's org (#639), after the axes — same rule as the middleware:
        # without `_org=`, the target resolves in the run's org, membership guarded.
        undo.extend(await run_org.pin_for_call())
        # A hosted job's tool list, held by the server: the TARGET must be in it, whichever
        # tool dispatched it (`oto_call`, a recipe page, a code recipe's bridge) — same
        # guard, same place, as the context middleware for a direct call.
        await outils_du_travail.exiger(name)
        # Activation of the TARGET's connector, against the org and team the axes
        # just set — same guard, same place, as the context middleware for a direct call
        # (#1064). Here rather than at visibility: the latter is a display filter,
        # which `oto_call` crosses by construction.
        await activation_gate.require_active(name)
        # The access level of the TARGET's connector, on its arguments (its `op`) — same
        # guard, same place, as the context middleware (`access_level_denied`).
        await access_level_gate.require_covered(
            name, args, lambda: _parameters_of(tool))
    except BaseException:
        for _reset, _tok in reversed(undo):
            _reset(_tok)
        raise
    # A token passed for a tool that does NOT support it (e.g. `_instance` on data_*,
    # `_org` on a non-org-scopable tool) = context with no effect → dropped from the args,
    # so as not to break its validation. Safe because the tokens are `_`-prefixed:
    # a homonymous BUSINESS argument (aiark `account` = the company filter) does not carry
    # the prefix and is therefore never touched (issue #250).
    call_axes.strip_unconsumed_axes(args)
    # Record OWN to the target. Without it, what the target logs (`key_mode` at the
    # resolver, `quantity` at the point where N is known) fell into the ENVELOPE
    # request's record, hence onto the `tool='oto_call'` row — which the billing
    # lens, filtering by tool name, never reads. MUTABLE holder set
    # before `tool.run`: a sync handler runs in the threadpool on a copy of the
    # context, and it is the mutation of THIS dict that comes back up.
    outer_trace = session_org.current_call_trace()
    target_trace: dict = {}
    trace_tok = session_org.set_call_trace(target_trace)
    # The target's OWN `call_uid`, on its row and its credit hold: under one gesture (the
    # envelope's), a recipe runs several targets, each its own call.
    cible_uid = geste.nouvel_identifiant()
    budget.ouvrir_appel(name, args, call_uid=cible_uid)
    started = time.monotonic()
    ok, err = True, None
    echec: Optional[BaseException] = None
    try:
        # `Tool.run`: `ctx` injection, schema validation, execution — but
        # OUTSIDE the middleware chain (hence outside redaction): we re-apply it further
        # down. This is what makes it possible to reach a hidden tool (the visibility
        # denylist only blocks the `tools/call` protocol path).
        result = await tool.run(args)
    except ValidationError as e:
        ok, err, echec = False, "invalid_arguments", e
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=f"Invalid arguments for `{demande}` — see `input_schema`.",
            data={"input_schema": getattr(tool, "parameters", None),
                  "errors": e.errors()}))
    # noqa: SILENT — the called tool's failure is returned in ok/err to the requester
    except Exception as e:  # noqa: BLE001 — the target's error IS a result
        # Two audiences, two messages. The LOG keeps the raw one, truncated — same
        # convention as `calllog.py` (`str(e)[:MAX_ERROR_CHARS]`) for a
        # normal call: it is the operations trace, it serves debugging. The AGENT,
        # on the other hand, must only see the SCRUBBED message: outside the middleware
        # chain (see above), `ErrorEnvelopeMiddleware` does not clean this path, so
        # we replay its classification here — otherwise a raw exception (internal
        # path, upstream URL fragment, technical id) went up as is to the
        # agent, whereas the same tool called normally sees its message
        # scrubbed (oto-backend#566).
        ok, err, echec = False, str(e)[:calllog.MAX_ERROR_CHARS], e
        info = error_taxonomy.classify(e)
        message = info.message
        # Same health tracking as the envelope (`ErrorEnvelopeMiddleware`), which this
        # out-of-chain path does not cross: the key served to the TARGET is marked.
        if info.code == "quota_exhausted":
            await connector_health.suivre_appel(target_trace, message)
        elif error_taxonomy.credential_rejected_in_chain(e):
            await connector_health.suivre_appel(target_trace, None, rejet=message)
        return IssueCible(ok=False, message=message, code=info.code,
                          retryable=bool(getattr(info, "retryable", False)))
    finally:
        # The TARGET's org and run, read BEFORE undoing the axes: after the reset,
        # `current_org` returns the caller's home org, not the one where the target
        # resolved its credentials — the row went out under the wrong org.
        target_org: object = _UNSET
        try:
            target_org = await run_in_threadpool(access.current_org, sub)
        # noqa: SILENT — best-effort: `_trace_target_call` falls back on its own read
        except Exception:
            pass
        target_run = session_org.current_call_run()
        # The target's settlement (L8), under the target's context (its axes are undone
        # below): what it noted on ITS record, settled once — its failure too.
        if ok:
            await run_in_threadpool(access.regler_appel, sub, name, target_trace)
        elif echec is not None:
            await run_in_threadpool(access.regler_echec, sub, name, target_trace, echec)
        session_org.reset_call_trace(trace_tok)
        # The echo returned to the agent (`resolved_account`/`resolved_connector`, read by
        # `CallContextMiddleware` in the ENVELOPE record) must survive; only
        # the keys that bill stay on the target's row, and the keys of the settlement
        # stay with the target, which has just settled them.
        # `credential_row` also stays with the target: carried up into the envelope
        # record, it would make the envelope (which sees `oto_call`
        # succeed) CLEAR the "credits exhausted" mark the target just set.
        if outer_trace is not None:
            outer_trace.update({k: v for k, v in target_trace.items()
                                if k not in _BILLING_TRACE_KEYS
                                and k not in access.CLES_REGLEMENT
                                and k != "credential_row"})
        for _reset, _tok in reversed(undo):
            _reset(_tok)
        await _trace_target_call(sub, name, args, ok, err,
                                 int((time.monotonic() - started) * 1000),
                                 trace=target_trace, org_id=target_org,
                                 run_id=target_run, call_uid=cible_uid)

    # Target success: a "credits exhausted" mark on ITS key is lifted.
    await connector_health.suivre_appel(target_trace, None)

    # Redaction re-applied (ADR 0036 §2) via the SHARED fail-closed logic —
    # otherwise a PII connector surfaced by oto_call would leak (the middleware saw
    # the "oto" service, not the target namespace).
    service = namespace_of(name)
    quantite = target_trace.get("quantity")
    quantite = quantite if isinstance(quantite, (int, float)) \
        and not isinstance(quantite, bool) else None
    try:
        servi = await run_in_threadpool(redaction.redact_result, service, result)
    except redaction.RedactionWithheld:
        return IssueCible(ok=True, result=redaction.withheld_result(name), retenu=True,
                          quantity=quantite)
    return IssueCible(ok=True, result=servi, quantity=quantite)


def _selectionner(result, fields: list[str]):
    """`oto_call fields=` : le résultat SERVI (rédaction déjà appliquée par
    `executer_cible`) réduit aux champs demandés. Après la rédaction, jamais avant :
    le tri ne fait que retirer, il ne peut pas rendre un champ que la politique a masqué.

    Un résultat sans JSON (texte libre) repart tel quel — il n'a pas de clés. La notice
    va sous `selection`, ou `oto_call_selection` si la cible porte déjà ce nom : elle ne
    doit jamais écraser un champ de la réponse."""
    payload = redaction.extract_payload(result)
    if payload is None:
        return result
    out, notice = output_projection.select_fields(payload, fields)
    if notice is not None:
        if isinstance(out, dict):
            out = {**out, ("selection" if "selection" not in out
                           else "oto_call_selection"): notice}
        else:
            out = {"result": out, "selection": notice}
    if out == payload:
        return result
    return redaction.rebuild_result(result, out)


def register(mcp: FastMCP) -> None:
    # --- dispatch universel (ADR 0036) --------------------------------------

    @mcp.tool()
    async def oto_tool_schema(name: str, ctx: Context) -> dict:
        """Return the input JSON Schema of ANY oto tool by name — even one that is
        NOT currently listed (hidden by default, connector not installed, FOD…).

        Use this to learn the exact `arguments` shape before calling a latent tool
        with `oto_call`. Tool names come from `oto_list_my_tools`.

        Args:
            name: Exact tool name (e.g. `fr_ccn_search`, `foncier_dpe_adresse`).
        """
        _require_sub()
        prefix = _tool_prefix()
        demande, name = name, deprecations.tool_canonique(
            tool_alias.canonical(name, prefix))
        _refuser_si_retire(name)
        tool = await _resolve_tool(ctx, name)
        if tool is None:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"Unknown tool `{demande}`. Use oto_list_my_tools to see available names."))
        return {
            # Returned under the name the caller WILL SEE in its list, not the
            # internal name: it will copy it into `oto_call`.
            "name": tool_alias.public(name, prefix),
            "namespace": tool_alias.public_namespace(namespace_of(name), prefix),
            "description": (tool.description or "").strip(),
            "input_schema": getattr(tool, "parameters", None),
            "output_schema": getattr(tool, "output_schema", None),
        }

    @mcp.tool()
    async def oto_call(name: str, arguments: Optional[dict] = None,
                       _org: Optional[int] = None, _run_id: Optional[str] = None,
                       fields: Optional[list[str]] = None,
                       *, ctx: Context):
        """Call ANY oto tool by name — including one that is NOT listed (hidden by
        default, connector not installed, FOD…), for a single call, WITHOUT adding it
        durably to your toolbox.

        Use this when you need a tool that does not appear in your tool list. If the
        tool IS already visible, call it directly — don't wrap it in `oto_call`.
        Discover names and schemas with `oto_list_my_tools` / `oto_tool_schema`.
        META/SPINE tools (`oto_*`, `data_*`, `run_*`, `feedback`) are NOT routable
        here — they are always visible: call them directly.

        This bypasses only the DISPLAY filter, never access control: the target's
        call-time gates (connector activation for the org/team the call resolves
        under — refused `connector_disabled` —, credential, connector RBAC, admin
        authz) and the org field-redaction policy apply exactly as for a direct call
        (ADR 0036).

        Call-context tokens (ADR 0038) are PREFIXED `_` — `_group`, `_project`,
        `_instance`, `_account`, `_run_id` — and may be included INSIDE `arguments`:
        they route the CALL CONTEXT (which org/team/credential-instance the target
        resolves under), are guarded exactly like on a listed tool, and are stripped
        before the target sees them. E.g. reach a team-scoped connector via
        `arguments={..., "_group": 3}`, or pin an instance via
        `"_instance": "<ref from oto_instance>"`.

        The prefix keeps them out of the tools' own argument space: an unprefixed
        `account`/`org`/`project` in `arguments` is a BUSINESS argument of the target
        (e.g. `aiark_company_search(account=…)` is AI Ark's company filter) and is
        passed through untouched.

        Args:
            name: Exact target tool name (e.g. `fr_ccn_search`).
            arguments: Argument object passed to the target tool. `{}` if none.
            _org: run the target tool under THIS organization (id) — resolves its
                credentials/visibility/data for that org (ADR 0038 call token,
                same membership guard as the flat `_org=` axis). Omit for your
                current org.
            _run_id: correlate this call to an open run, exactly like `_run_id` on a
                listed tool. Accepted here as well as inside `arguments` — same
                token, same effect — so the instruction « pass it on every call »
                never costs a call.
            fields: keep ONLY these keys in each result row (or in the single
                object returned); `["*"]` = the whole response. Omit = unchanged.
        """
        # Ambient identity: the JWT's sub already carries the call (the target handler
        # resolves its own credentials on it). Soft — without a token (local dev) there is
        # no sub and the whole catalog is already accessible. An identity failure,
        # however, is not an absence of token: it bubbles up (#464).
        sub = current_user_sub_from_token()

        # The name comes from the catalog, hence possibly in the tenant's form. It
        # becomes canonical again BEFORE the meta/spine gate: without that `acme_doc` resolves an
        # unknown namespace, escapes `_NON_DISPATCHABLE`, and the anti-loop is skipped.
        demande, name = name, tool_alias.canonical(name, _tool_prefix())
        # A RETIRED name is refused BEFORE the meta/spine gate: `oto_kb` is an `oto_*` name, and
        # "call it directly" would send the agent to a name that no longer exists.
        # It is THIS path an agent takes when a procedure names a tool absent from
        # its list — the notice prescribes it.
        _refuser_si_retire(name)
        if namespace_of(name) in _NON_DISPATCHABLE:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"`{demande}` is a meta/spine tool — call it directly, "
                        "not via oto_call."))

        # `fields` is judged BEFORE the run: an empty or malformed selection must not
        # cost a billed call whose answer would then be thrown away.
        if fields is not None and (not fields or not all(
                isinstance(f, str) and f for f in fields)):
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message="`fields` must name at least one key (`[\"*\"]` = the whole "
                        "response), or be omitted."))

        args = arguments if isinstance(arguments, dict) else {}
        # Call-context axes (ADR 0038). oto_call runs OUTSIDE the middleware → the
        # axes of flat tools (org/group/project/instance/account/run_id) are not
        # set for us. WE replay the flat middleware's applies-gated loop
        # OURSELVES (`call_axes.axes_for_call` — the axes READ, not only the ANNOUNCED
        # ones: `_account=` is accepted on any multi-account connector even when the
        # schema does not advertise it, otherwise `strip_unconsumed_axes` swallows it and the call
        # goes to the default account, without error — review #399 F1;
        # AXES order → the most specific co-sets its org):
        # each axis present in `arguments` (or the top-level `_org=` param, folded
        # below) is GUARDED+SET then REMOVED from the args. Set BEFORE the run's try so
        # that a guard refusal PROPAGATES (McpError) instead of being captured as a target
        # error. Closes #228 (a connector's instance/group unreachable via
        # oto_call — only `_org=` was honored).
        if _org is not None:
            args.setdefault("_org", _org)
        # `_run_id=` passed AT oto_call's LEVEL rather than inside `arguments`: folded
        # like `_org`, for the same reason. The model sees `_org` at the top of the schema
        # and puts the sibling token in the same place; without this fold, the call failed
        # ("Unexpected keyword argument") whereas the notice asks it to carry
        # `_run_id` on EVERY call. `setdefault`: what is already in `arguments`
        # wins — it is the documented form, it must not get overwritten.
        if _run_id is not None:
            args.setdefault("_run_id", _run_id)
        tool = await _resolve_tool(ctx, name)
        if tool is None:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"Unknown tool `{demande}`. Use oto_list_my_tools to see available names."))
        issue = await executer_cible(tool, sub, name, demande, args)
        if not issue.ok:
            # `tool` takes the REQUESTED name: the agent re-reads it to retry, and a
            # name it never typed would make it doubt its own request.
            return {"tool": demande, "ok": False, "error": issue.message}
        if fields is None or issue.retenu:
            return issue.result
        return _selectionner(issue.result, fields)

    # --- admin: sensitive namespace grants ----------------------------------

    def _require_admin() -> str:
        sub = _require_sub()
        if not access.is_super_admin(sub):
            raise McpError(ErrorData(
                code=INVALID_PARAMS, message="Reserved to the super admin.",
            ))
        return sub

    # Namespace grants (user + org) merged into the MCP capability
    # `oto_admin_namespace_access` (capabilities/namespace_access.py).
    #
    # Platform keys (list/set) REMOVED from the MCP side (2026-06-25): setting a
    # raw key = a plaintext secret in the LLM context → dashboard-only. CRUD
    # served by the REST routes `/api/admin/platform-keys*` (api/routes.py).
