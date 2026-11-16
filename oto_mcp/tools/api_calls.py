"""`api` connector, third module: `api_act`, what ACTS on a custom connector (the
pair's convention: `api` reads, `api_act` acts — the tool an MCP client confirms).

- `op="call"` — one of its saved actions, by name. Without `_account`, the action is
  looked up across the org's custom connectors: a name that only one of them carries
  resolves alone, a shared name is refused with the candidates. An action may be a GET:
  the client cannot know what a described action does at the third party, so every
  call is an act.
- `op="request"` — a raw call, any verb, for what no action describes yet.
- `op="import"` (admin) — candidate actions from an OpenAPI/Swagger JSON document,
  NOT saved: it fetches a document the admin points at, and prepares a write.
- `op="set_definition"` (admin) — saves the connector's definition for every member.

The calls go through the same client as `api_get` (vault, egress guard, redirect
refusal, upstream-error translation), with the definition's constant headers; `call`
also fills an omitted argument from the definition's saved variables. Every verb is
carried: oto-core's `HttpConnectorClient.request` takes GET/POST/PUT/PATCH/DELETE —
except on a card in session mode (login and password), which only reads: a write verb
is refused by name before anything is sent, `dry_run` included (`api.refuse_write`) —
unless the saved action declares `class: read` (`custom_ops`), never for `request`.
"""
from __future__ import annotations

from typing import Literal, Optional

import requests
from fastmcp import FastMCP

from mcp.types import INVALID_PARAMS, ErrorData

from .. import custom_ops, custom_ops_output, session_org
from ..mcp_errors import McpError
from ..texte_tiers import cloture
from ..connectors import api_operations as store
from . import api as api_tool
from .api_ops import (_auth_header, _bad, _definition, _org_connectors, _require_admin,
                       _resolved, _sub, refuse_foreign)
from .lecture import ACTE

#: What each op reads; any other argument given is refused by name.
ARGS = {
    "call": ("operation", "args", "dry_run"),
    "request": ("method", "path", "params", "body", "dry_run"),
    "import": ("source", "url", "document"),
    "set_definition": ("operations", "label", "description", "logo_domain", "headers",
                       "variables"),
}


def _account_for(sub: str, operation: str) -> Optional[str]:
    """The account whose definition carries `operation`, when `_account` is not
    given — None to fall back on the call's default resolution."""
    if session_org.current_call_account() is not None:
        return None
    # One account at both scopes is ONE connector for the call: the cascade serves the
    # caller's own key first (`_account` resolves it), so it is not an ambiguity.
    par_compte: dict[str, dict] = {}
    for c in _org_connectors(sub):
        if custom_ops.find(c["operations"], operation):
            par_compte.setdefault(c["account"], c)
    porteurs = list(par_compte.values())
    if len(porteurs) > 1:
        raise _bad(f"action `{operation}` exists on several custom connectors: "
                   + ", ".join(f"`{c['account']}` ({c['label']})" for c in porteurs)
                   + " — pass `_account=` to choose.")
    return porteurs[0]["account"] if porteurs else None


def _row(rc) -> tuple[str, str, str]:
    return rc.entity_type, str(rc.entity_id), rc.account or ""


def _send(rc, headers: dict, plan: dict, action: Optional[dict] = None):
    """The answer of the call; for a saved `action`, read as it declares (`refusals`,
    `output`: `custom_ops_output.served`) — a JSON answer only, a success that is not
    JSON keeps its `non_json_body` form."""
    client = api_tool.client_from_fields(rc.fields, headers, row=_row(rc))
    try:
        answer = client.request(plan["method"], plan["path"],
                                params=plan["query"] or None, json=plan["body"])
    except requests.JSONDecodeError as e:
        return api_tool.non_json_success(e)
    except ValueError as e:
        raise api_tool._value_error(e)
    except requests.HTTPError as e:
        raise api_tool._upstream_error(e)
    if action is None:
        return answer
    try:
        return custom_ops_output.served(action, answer)
    except custom_ops_output.Refused as e:
        raise _declared_refusal(e, answer)


def _declared_refusal(e: "custom_ops_output.Refused", answer) -> McpError:
    """A refusal the action's definition names, carried by a success answer: its code
    and message, then the answer's excerpt, fenced as untrusted data — like the body of
    an upstream error (`api._upstream_error`)."""
    message = f"Refusal `{e.code}`: {e.message}"
    body = api_tool.excerpt_of(answer)
    if body:
        message += cloture("upstream-answer", body,
                           origine="Answer returned by the target API", lecture="a diagnostic")
    return McpError(ErrorData(code=INVALID_PARAMS, message=message,
                              data={"code": e.code, "retryable": e.retryable,
                                    "status": custom_ops_output.WHEN}))


def _action(sub: str, operation: Optional[str]):
    """`(row, definition, action)` of the saved action `operation` this call resolves
    to — refused (`McpError`) when there is none. Reads the database."""
    if not operation:
        raise _bad("`api_act(op=\"call\")` needs `operation` — the action's name, "
                   "from `api(op=\"list\")`.")
    rc = _resolved(_account_for(sub, operation))
    d = _definition(rc)
    found = custom_ops.find(d["operations"], operation)
    if found is None:
        names = ", ".join(o["name"] for o in d["operations"][:30]) or "none yet"
        raise _bad(f"no action `{operation}` on custom connector `{rc.account}` — "
                   f"available: {names}.")
    return rc, d, found


def action_jugee(operation: Optional[str]) -> dict:
    """The saved action `operation` (its `method`, its `class` when it declares one),
    resolved as `api_act(op="call")` resolves it (the call's `_account`, else the org's
    connector that carries it). Raises like the call itself — never a default.

    SYNCHRONOUS and it reads the database (`connector_instances.config`): from an
    `async def`, call it off the event loop (`await run_in_threadpool(...)`), never
    directly. It is what `tools/effets` judges `api_act(op="call")` on."""
    return _action(_sub(), operation)[2]


def _call(sub: str, operation: Optional[str], args: Optional[dict],
          dry_run: bool) -> dict:
    rc, d, found = _action(sub, operation)
    try:
        plan = custom_ops.plan_call(found, args, d["variables"])
    except custom_ops.CallRefused as e:
        raise _bad(str(e))
    api_tool.refuse_write(rc.fields, plan["method"], reads=custom_ops.reads(found))
    if dry_run:
        return {"dry_run": True, "account": rc.account, **plan}
    return _send(rc, d["headers"], plan, found)


def _request(method: Optional[str], path: Optional[str], params: Optional[dict],
             body, dry_run: bool) -> dict:
    verb = (method or "").strip().upper()
    if verb not in custom_ops.METHODS:
        raise _bad(f"`method` must be one of {', '.join(custom_ops.METHODS)}.")
    errs = custom_ops.path_errors("`path`", path)
    if errs:
        raise _bad("; ".join(errs))
    rc = _resolved()
    api_tool.refuse_write(rc.fields, verb)
    plan = {"method": verb, "path": path, "query": params or {}, "body": body}
    if dry_run:
        return {"dry_run": True, "account": rc.account, **plan}
    # A connector without a saved definition has `{}` headers; a MISSING instance
    # (the credential was removed meanwhile) is refused, as `op="call"` refuses it.
    return _send(rc, _definition(rc)["headers"], plan)


def _import(sub: str, source: Optional[str], url: Optional[str],
            document: Optional[dict]) -> dict:
    rc = _resolved()
    _require_admin(sub, rc)
    try:
        cands, skipped = store.import_candidates(
            source or "", url=url, document=document,
            fields=rc.fields if source == "doc_path" else None, row=_row(rc))
    except store.ImportFailed as e:
        raise _bad(f"import: {e}")
    return {"account": rc.account, "operations": cands, "skipped": skipped,
            "saved": False}


def _set_definition(sub: str, given: dict) -> dict:
    rc = _resolved()
    _require_admin(sub, rc)
    try:
        d = store.save_definition(rc.entity_type, str(rc.entity_id), rc.account,
                                  given, auth_header=_auth_header(rc))
    except custom_ops.OperationsInvalid as e:
        raise _bad("definition refused: " + " | ".join(e.errors[:20]))
    except store.InstanceMissing as e:
        raise _bad(f"`api` connector: {e}.")
    return {"account": rc.account, "saved": sorted(given),
            "label": d["label"], "actions": [o["name"] for o in d["operations"]]}


def register(mcp: FastMCP) -> None:
    @mcp.tool(name="api_act", annotations=ACTE)
    def api_act(op: Literal["call", "request", "import", "set_definition"],
                 operation: Optional[str] = None, args: Optional[dict] = None,
                 method: Optional[str] = None, path: Optional[str] = None,
                 params: Optional[dict] = None, body: Optional[dict | list] = None,
                 source: Optional[Literal["doc_path", "url", "inline"]] = None,
                 url: Optional[str] = None, document: Optional[dict] = None,
                 operations: Optional[list[dict]] = None, label: Optional[str] = None,
                 description: Optional[str] = None, logo_domain: Optional[str] = None,
                 headers: Optional[dict] = None, variables: Optional[dict] = None,
                 dry_run: bool = False) -> dict:
        """Act on a custom connector (Custom API) — ops: `call`, `request`, `import`,
        `set_definition`. Reads (the org's connectors, an action's input, the setup
        link): `api`.

        - `call`: an action by name (`operation`, `args`). `_account` is optional when
          only one of the org's custom connectors has this action. Arguments are
          checked against its input; an omitted one takes the connector's saved
          variable of the same name. Any verb; an action marked `class: read`
          only reads, even as a POST.
        - `request`: a raw call to the custom connector of this call (`_account` when
          there are several), any verb (`method`, `path`, `params`, `body`) — for what
          no saved action describes. Its auth and constant headers are applied.
          Prefer `call` when an action exists.
        - `import` (admin): turns an OpenAPI/Swagger JSON document into candidate
          actions WITHOUT saving them (`source`, `url` | `document`); review, then
          save with `set_definition`.
        - `set_definition` (admin): saves the definition of the custom connector of
          this call (`_account`), reused by every member and session. Only the parts
          given change. `operations` REPLACES the action list: each `name`
          (snake_case), `description`, `method` GET|POST|PUT|PATCH|DELETE, `path`
          relative with `{param}`, `input` (JSON schema object), optional `in`
          {arg: path|query|body}, optional `class: read` (a GET or POST that only
          reads), optional `output` {items, strip, projection} (what the answer
          gives) and `refusals` [{code, when: 200, body: {path, equals|contains},
          message}] (an error the service writes in a success body, served as that
          named refusal). Never a secret here: the key stays on the card.

        `dry_run=true` (`call`, `request`) returns {method, path, query, body} without
        sending it.

        Args:
            op: call | request | import | set_definition.
            operation: call — name of the action.
            args: call — its arguments, by name.
            method: request — GET | POST | PUT | PATCH | DELETE.
            path: request — relative to the connector's base URL, starts with /, no
                query.
            params: request — query parameters.
            body: request — JSON body.
            source: import — doc_path (the card's doc route, same auth), url (a public
                JSON document, no auth sent) or inline (`document`).
            url: import, source=url only.
            document: import, source=inline only — the OpenAPI JSON document.
            operations: set_definition — the full action list (100 max); [] clears it.
            label: set_definition — display name of the connector (e.g. "Acme
                Payroll").
            description: set_definition — what the tool is and what it holds, for the
                agent.
            logo_domain: set_definition — brand domain for the logo (e.g. acme.test).
            headers: set_definition — constant NON-secret headers (e.g. an API
                version).
            variables: set_definition — saved values {name: {value, description}} that
                fill an action argument of the same name when the call omits it.
            dry_run: call, request — validate and preview, send nothing.
        """
        given = {"operation": operation, "args": args, "method": method, "path": path,
                 "params": params, "body": body, "source": source, "url": url,
                 "document": document, "operations": operations, "label": label,
                 "description": description, "logo_domain": logo_domain,
                 "headers": headers, "variables": variables,
                 "dry_run": dry_run or None}
        refuse_foreign("api_act", op, given, ARGS[op])
        sub = _sub()
        if op == "call":
            return _call(sub, operation, args, dry_run)
        if op == "request":
            return _request(method, path, params, body, dry_run)
        if op == "import":
            return _import(sub, source, url, document)
        return _set_definition(sub, {k: given[k] for k in ARGS["set_definition"]
                                     if given[k] is not None})
