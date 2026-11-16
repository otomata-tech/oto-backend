"""Operations of the `api` connector described in DATA — validation and call plan.

An org connects a tool we do not ship (a payroll or ERP SaaS with an API key) through
the generic `api` connector. `api_get`/`api_post` reach it, but the agent has to
guess paths and parameters each time. An org can instead DESCRIBE the operations it
uses — name, description, method, path template, input schema — and the agent calls
them by name (`api_act(op="call")`). The list lives in `connector_instances.config["operations"]`
of the credential's instance (`connectors/api_operations.py`); this module is PURE:
no I/O, no DB, testable on its own.

The format is a subset of oto-core's `connectors/connector.schema.json` (`function`:
`name`, `description`, `input`, `call.method`, `call.path`), flattened — an org writes
one list, not a connector file:

    {"name": "list_collaborators", "description": "…", "method": "GET",
     "path": "/organizations/{organizationId}/collaborators",
     "input": {"type": "object",
               "properties": {"organizationId": {"type": "string"},
                              "page": {"type": "integer"}},
               "required": ["organizationId"]},
     "in": {"page": "query"}}          # optional

`in` routes an argument: `path` when the template cites it, otherwise `query` for
GET/DELETE and `body` for the other methods.

`class: read` (optional, oto-core's word: `class` of a described function) says the
action only READS although its verb is a POST — a search sent as a JSON body, an RPC
endpoint. It is the only value accepted here: without it an action is judged by its
verb (`tools/effets`: POST sends), and `write`/`sensitive` would only restate or
weaken that. It changes two things, for THIS action only: its effect (`lire`), and a
card in session mode lets it through (`tools/api.refuse_write`). A raw call
(`api_act(op="request")`) never carries it. Only a GET or a POST may declare it — a
PUT, PATCH or DELETE that reads is not an API anyone describes.

`output` and `refusals` (optional, oto-core's words too): what the agent receives of the
answer, and a refusal the service writes in the body of a success — `custom_ops_output`.

⚠️ A path parameter is the one place where an argument value reaches the URL PATH.
It is percent-encoded whole (`quote(safe="")`) and a value that is `.`/`..` or carries
a `/` — raw or once decoded — is refused: without that, `{id}` = `../admin` walks out
of the template the org wrote, to any route its key opens.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import quote, unquote

from . import custom_ops_output as output

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
MAX_OPERATIONS = 100
# The whole list, serialized: it rides in a JSONB column read on every `api_act(op="call")`.
MAX_BYTES = 256 * 1024
MAX_DESCRIPTION = 1000
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
# An argument name: what an API calls its parameter (`organizationId`, `page[size]`).
ARG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-\[\]]{0,63}$")
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
JSON_TYPES = ("string", "integer", "number", "boolean", "array", "object", "null")
LOCATIONS = ("path", "query", "body")
_OP_KEYS = {"name", "description", "method", "path", "input", "in", "class", "output",
            "refusals"}
#: The `class` an action may declare (oto-core's vocabulary is read|write|sensitive).
READ = "read"
_READ_METHODS = ("GET", "POST")


class OperationsInvalid(ValueError):
    """The list is refused; `errors` names every problem, one line each."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors[:5]) + (" …" if len(errors) > 5 else ""))
        self.errors = errors


class CallRefused(ValueError):
    """The arguments of an `api_act(op="call")` do not fit the operation."""


def placeholders(path: str) -> list[str]:
    return _PLACEHOLDER.findall(path or "")


def _path_errors(where: str, path: Any) -> list[str]:
    if not isinstance(path, str) or not path.startswith("/"):
        return [f"{where}: `path` must be a string starting with `/` (relative to base_url)"]
    errs = []
    if path.startswith("//") or "://" in path:
        errs.append(f"{where}: `path` is relative to base_url — no scheme, no host")
    if any(c in path for c in "?#") or any(c.isspace() for c in path):
        errs.append(f"{where}: `path` carries no `?`, `#` or whitespace — query "
                    "parameters are declared in `input`")
    if any(seg in (".", "..") for seg in path.split("/")) or ".." in unquote(path):
        errs.append(f"{where}: `path` must not contain `..`")
    if any(not ARG_RE.match(p) for p in placeholders(path)):
        errs.append(f"{where}: every `{{param}}` of `path` must be an argument name")
    return errs


path_errors = _path_errors     # the raw call (`api_act(op="request")`) checks a path the same way


def _type_errors(where: str, schema: Any) -> list[str]:
    if not isinstance(schema, dict):
        return [f"{where}: must be an object (a JSON schema)"]
    t = schema.get("type")
    types = t if isinstance(t, list) else ([t] if t is not None else [])
    if any(x not in JSON_TYPES for x in types):
        return [f"{where}: `type` must be one of {', '.join(JSON_TYPES)}"]
    d = schema.get("description")
    if d is not None and not isinstance(d, str):
        return [f"{where}: `description` must be a string"]
    return []


def _input_errors(where: str, inp: Any) -> list[str]:
    if inp is None:
        return []
    if not isinstance(inp, dict) or inp.get("type", "object") != "object":
        return [f"{where}: `input` must be a JSON schema of type object"]
    props = inp.get("properties", {})
    if not isinstance(props, dict):
        return [f"{where}: `input.properties` must be an object"]
    errs = []
    for arg, schema in props.items():
        if not isinstance(arg, str) or not ARG_RE.match(arg):
            errs.append(f"{where}: argument name `{arg}` is invalid (letters, digits, "
                        "`_ . - [ ]`, starting with a letter, 64 max)")
        errs += _type_errors(f"{where}: argument `{arg}`", schema)
    req = inp.get("required", [])
    if not isinstance(req, list) or any(r not in props for r in req):
        errs.append(f"{where}: `input.required` must list arguments of `input.properties`")
    return errs


def _class_errors(where: str, op: dict, method: Any) -> list[str]:
    if "class" not in op:
        return []
    if op["class"] != READ:
        return [f"{where}: `class` accepts `{READ}` only — an action that writes is "
                "judged by its method, declare nothing"]
    if isinstance(method, str) and method.upper() not in _READ_METHODS:
        return [f"{where}: `class: {READ}` is declared on a GET or a POST only, "
                f"not {method.upper()}"]
    return []


def reads(op: dict) -> bool:
    """The action declares `class: read`: it only reads, whatever its verb."""
    return op.get("class") == READ


def _validate_one(i: int, op: Any) -> list[str]:
    if not isinstance(op, dict):
        return [f"operation #{i}: must be an object"]
    name = op.get("name")
    where = f"operation `{name}`" if isinstance(name, str) else f"operation #{i}"
    errs = [f"{where}: unknown key `{k}` (accepted: {', '.join(sorted(_OP_KEYS))})"
            for k in op if k not in _OP_KEYS]
    if not isinstance(name, str) or not NAME_RE.match(name):
        errs.append(f"{where}: `name` must match ^[a-z][a-z0-9_]{{0,62}}$ (snake_case)")
    desc = op.get("description")
    if not isinstance(desc, str) or not desc.strip():
        errs.append(f"{where}: `description` is required — it is what the agent reads "
                    "to choose the operation")
    elif len(desc) > MAX_DESCRIPTION:
        errs.append(f"{where}: `description` is {len(desc)} characters, "
                    f"{MAX_DESCRIPTION} max")
    method = op.get("method")
    if not isinstance(method, str) or method.upper() not in METHODS:
        errs.append(f"{where}: `method` must be one of {', '.join(METHODS)}")
    errs += _path_errors(where, op.get("path"))
    errs += _input_errors(where, op.get("input"))
    errs += _class_errors(where, op, method)
    errs += output.output_errors(where, op.get("output"))
    errs += output.refusals_errors(where, op.get("refusals"))
    if errs:
        return errs
    props = (op.get("input") or {}).get("properties", {})
    required = set((op.get("input") or {}).get("required", []))
    in_map = op.get("in") or {}
    if not isinstance(in_map, dict):
        return [f"{where}: `in` must map argument names to path|query|body"]
    tpl = placeholders(op["path"])
    for p in tpl:
        if p not in props:
            errs.append(f"{where}: `{{{p}}}` in `path` is not an argument of `input`")
        elif p not in required:
            errs.append(f"{where}: path argument `{p}` must be listed in `input.required`")
        elif in_map.get(p, "path") != "path":
            errs.append(f"{where}: `{p}` is in the path template, its `in` must be `path`")
    for arg, loc in in_map.items():
        if arg not in props:
            errs.append(f"{where}: `in` names `{arg}`, which is not an argument of `input`")
        elif loc not in LOCATIONS:
            errs.append(f"{where}: `in.{arg}` must be one of {', '.join(LOCATIONS)}")
        elif loc == "path" and arg not in tpl:
            errs.append(f"{where}: `in.{arg}` is `path` but `path` has no `{{{arg}}}`")
    if method.upper() == "GET" and any(
            location(op, a) == "body" for a in props):
        errs.append(f"{where}: a GET carries no body — route its arguments to `query`")
    return errs


def validate_operations(ops: Any) -> list[dict]:
    """The list, normalized (method upper-cased, empty `in` dropped), or
    `OperationsInvalid` naming every problem. Replaces, never merges."""
    if not isinstance(ops, list):
        raise OperationsInvalid(["`operations` must be a list"])
    errors: list[str] = []
    if len(ops) > MAX_OPERATIONS:
        errors.append(f"{len(ops)} operations, {MAX_OPERATIONS} max")
    seen: set[str] = set()
    for i, op in enumerate(ops):
        errors += _validate_one(i, op)
        name = op.get("name") if isinstance(op, dict) else None
        if isinstance(name, str):
            if name in seen:
                errors.append(f"operation `{name}`: duplicate name")
            seen.add(name)
    if not errors and len(json.dumps(ops)) > MAX_BYTES:
        errors.append(f"the list weighs more than {MAX_BYTES // 1024} KB — shorten "
                      "descriptions or split the API's surface")
    if errors:
        raise OperationsInvalid(errors)
    out = []
    for op in ops:
        n = {"name": op["name"], "description": op["description"].strip(),
             "method": op["method"].upper(), "path": op["path"],
             "input": op.get("input") or {"type": "object", "properties": {}}}
        if op.get("in"):
            n["in"] = dict(op["in"])
        if reads(op):
            n["class"] = READ
        n.update(output.normalized(op))
        out.append(n)
    return out


def location(op: dict, arg: str) -> str:
    """Where `arg` goes: its `in`, else `path` if the template cites it, else `query`
    for GET/DELETE and `body` for the rest."""
    loc = (op.get("in") or {}).get(arg)
    if loc:
        return loc
    if arg in placeholders(op.get("path") or ""):
        return "path"
    return "query" if (op.get("method") or "").upper() in ("GET", "DELETE") else "body"


def find(ops: list[dict], name: str) -> Optional[dict]:
    return next((op for op in ops if op.get("name") == name), None)


def _fits(value: Any, t: str) -> bool:
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "array":
        return isinstance(value, list)
    if t == "object":
        return isinstance(value, dict)
    return value is None


def _path_value(arg: str, value: Any) -> str:
    """A path argument, percent-encoded whole — or refused if it would leave its
    segment (`/`, `.`, `..`, raw or once decoded)."""
    if isinstance(value, bool):
        raw = "true" if value else "false"
    elif isinstance(value, (str, int, float)):
        raw = str(value)
    else:
        raise CallRefused(f"argument `{arg}` goes in the path: it must be a string "
                          "or a number")
    for v in (raw, unquote(raw)):
        if v in ("", ".", "..") or "/" in v or "\\" in v:
            raise CallRefused(f"argument `{arg}` goes in the path and must be a single "
                              f"segment: `{raw}` is empty, `.`/`..` or carries a `/`")
    return quote(raw, safe="")


def _variable_value(value: str, schema: dict) -> Any:
    """A saved variable is text; an argument typed integer/number/boolean takes it
    converted — when it converts, otherwise as is (the type check then names it)."""
    t = (schema or {}).get("type")
    try:
        if t == "integer":
            return int(value)
        if t == "number":
            return float(value)
    except (TypeError, ValueError):
        return value
    if t == "boolean" and str(value).lower() in ("true", "false"):
        return str(value).lower() == "true"
    return value


def plan_call(op: dict, args: Optional[dict],
              variables: Optional[dict] = None) -> dict:
    """`{method, path, query, body}` for this operation and these arguments, after
    checking them against `input` (unknown, missing required, basic JSON types).
    `None` = argument omitted. `variables` = the connector's saved non-secret values
    (`{name: {"value", "description"}}`): an argument of the same name that the
    caller does not pass takes it."""
    args = {k: v for k, v in (args or {}).items() if v is not None}
    inp = op.get("input") or {}
    props = inp.get("properties") or {}
    for name, var in (variables or {}).items():
        if name in props and name not in args and isinstance(var, dict) \
                and var.get("value") not in (None, ""):
            args[name] = _variable_value(var["value"], props[name])
    unknown = sorted(a for a in args if a not in props)
    if unknown:
        raise CallRefused(f"unknown argument(s) {', '.join(unknown)} for `{op['name']}` "
                          f"— accepted: {', '.join(props) or 'none'}")
    missing = [r for r in inp.get("required") or [] if r not in args]
    if missing:
        raise CallRefused(f"`{op['name']}` requires {', '.join(missing)}")
    for arg, value in args.items():
        t = (props.get(arg) or {}).get("type")
        types = t if isinstance(t, list) else ([t] if t else [])
        if types and not any(_fits(value, x) for x in types):
            raise CallRefused(f"argument `{arg}` must be of type {'|'.join(types)}")
    path = op["path"]
    query: dict = {}
    body: dict = {}
    for arg, value in args.items():
        loc = location(op, arg)
        if loc == "path":
            path = path.replace("{" + arg + "}", _path_value(arg, value))
        elif loc == "query":
            if isinstance(value, dict):
                raise CallRefused(f"argument `{arg}` goes in the query string: an "
                                  "object cannot — declare it `in: body`")
            query[arg] = value
        else:
            body[arg] = value
    return {"method": op["method"], "path": path, "query": query,
            "body": body if body or op["method"] not in ("GET", "DELETE") else None}


def summary(op: dict) -> dict:
    """What `api(op="list")` lists for one operation."""
    out = {"name": op["name"], "description": op["description"],
           "method": op["method"], "path": op["path"], "input": op.get("input")}
    if reads(op):
        out["class"] = READ
    out.update(output.normalized(op))
    return out
