"""What an action of the `api` connector (Custom API) gives the agent of its answer, and
the business refusal its answer may carry — declared on the action, in oto-core's words.

Two optional keys of an action (`custom_ops`), the same as a described function's in the
lib, read with its semantics and served by ITS code (`oto.connectors.serve`), never a copy:

- `output` (docs/conception/sortie.md of the lib): `items`, the dotted path of the list to
  unwrap; `strip`, dotted paths of keys the answer loses before reaching the agent (a
  list met on the way is traversed, from each element of `items` when declared);
  `projection`, the first-level keys kept. `strip` applies before `projection`. A
  declared `items` the answer does not carry is refused (`api_output_items_missing`),
  never read as empty. `schema` and `sample` are not taken: the first describes, the
  second only serves the lib's factory.
- `refusals` (docs/conception/signal-de-refus.md of the lib): a refusal the service
  writes in the BODY of a success (`{"hasError": true, "errorCode": …}` under a 200),
  `{code, when: 200, body: {path, equals | contains}, message, retryable?}`. The answer
  is judged on the raw body, before `output`; the first entry whose condition holds is
  served as a named refusal (`code`), never as a success. `when` is 200 only: an error
  status already reaches the agent as `Target API: HTTP <status>` with its body, and the
  client does not give the exact 2xx of a success — every success answer is judged as
  the 200 it declares. `body` is required: without it every success would be refused.
  `marks_account` is not taken (no card goes red on a declared business refusal).

An action that declares neither answers exactly what it answered before.

PURE: no I/O, the caller translates `Refused` for its face.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from oto.connectors import serve

OUTPUT_KEYS = ("items", "projection", "strip")
REFUSAL_KEYS = {"code", "when", "body", "message", "retryable"}
BODY_KEYS = {"path", "equals", "contains"}
#: The status a declared refusal is judged under: the success answer.
WHEN = 200
CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
MAX_MESSAGE = 500
MAX_REFUSALS = 20
#: What the reading of the answer refuses, served by name.
ITEMS_MISSING = "api_output_items_missing"


class Refused(ValueError):
    """The answer is refused: a declared business refusal, or an answer without the
    list `output.items` declares. `code`, `message`, `retryable` are what is served."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def _path_ok(path: Any) -> bool:
    return isinstance(path, str) and bool(path) and all(path.split("."))


def _names(where: str, key: str, value: Any, *, paths: bool) -> list[str]:
    what = "dotted paths" if paths else "key names"
    if not isinstance(value, list) or not value:
        return [f"{where}: `output.{key}` must be a non-empty list of {what}"]
    if any(not _path_ok(v) or (not paths and "." in v) for v in value):
        return [f"{where}: `output.{key}` holds an empty, malformed or non-text entry "
                f"({what}{'' if paths else ', no dot'})"]
    if len(set(value)) != len(value):
        return [f"{where}: `output.{key}` names an entry twice"]
    if paths:
        under = sorted(p for p in value for q in value if p.startswith(q + "."))
        if under:
            return [f"{where}: `output.strip` path `{under[0]}` is under another path "
                    "of the list, which already removes it whole"]
    return []


def output_errors(where: str, output: Any) -> list[str]:
    if output is None:
        return []
    if not isinstance(output, dict) or not output:
        return [f"{where}: `output` must be an object with {', '.join(OUTPUT_KEYS)}"]
    errs = [f"{where}: `output` has unknown key `{k}` (accepted: {', '.join(OUTPUT_KEYS)})"
            for k in output if k not in OUTPUT_KEYS]
    if "items" in output and not _path_ok(output["items"]):
        errs.append(f"{where}: `output.items` must be the dotted path of a list")
    if "strip" in output:
        errs += _names(where, "strip", output["strip"], paths=True)
    if "projection" in output:
        errs += _names(where, "projection", output["projection"], paths=False)
    return errs


def _body_errors(where: str, body: Any) -> list[str]:
    if not isinstance(body, dict):
        return [f"{where}: `body` is required — a condition on the answer, "
                "{path, equals | contains}; without it every success is refused"]
    errs = [f"{where}: `body` has unknown key `{k}` (accepted: path, equals, contains)"
            for k in body if k not in BODY_KEYS]
    if not _path_ok(body.get("path")):
        errs.append(f"{where}: `body.path` must be a dotted path (a list index as digits)")
    ops = [k for k in ("equals", "contains") if k in body]
    if len(ops) != 1:
        return errs + [f"{where}: `body` takes exactly one of `equals`, `contains`"]
    values = body[ops[0]]
    if not isinstance(values, list) or not values:
        return errs + [f"{where}: `body.{ops[0]}` must be a non-empty list"]
    if ops[0] == "equals":
        ok = all(isinstance(v, (str, int, bool)) for v in values)
        kind = "texts, integers or booleans"
    else:
        ok = all(isinstance(v, str) and v for v in values)
        kind = "non-empty texts"
    if not ok:
        errs.append(f"{where}: `body.{ops[0]}` holds {kind} only")
    elif len({(type(v), v) for v in values}) != len(values):
        errs.append(f"{where}: `body.{ops[0]}` names a value twice")
    return errs


def _refusal_errors(where: str, r: Any) -> list[str]:
    if not isinstance(r, dict):
        return [f"{where}: must be an object"]
    errs = [f"{where}: unknown key `{k}` (accepted: {', '.join(sorted(REFUSAL_KEYS))})"
            for k in r if k not in REFUSAL_KEYS]
    if not isinstance(r.get("code"), str) or not CODE_RE.match(r["code"]):
        errs.append(f"{where}: `code` must match ^[a-z][a-z0-9_]{{0,62}}$ (the refusal's name)")
    when = r.get("when")
    if not isinstance(when, int) or isinstance(when, bool) or when != WHEN:
        errs.append(f"{where}: `when` must be {WHEN} — a refusal carried by a success "
                    "answer; an error status already reaches the agent with its body")
    msg = r.get("message")
    if not isinstance(msg, str) or not msg.strip() or len(msg) > MAX_MESSAGE:
        errs.append(f"{where}: `message` is required, {MAX_MESSAGE} characters max — "
                    "what the agent reads")
    if "retryable" in r and not isinstance(r["retryable"], bool):
        errs.append(f"{where}: `retryable` must be a boolean")
    return errs + _body_errors(where, r.get("body"))


def refusals_errors(where: str, refusals: Any) -> list[str]:
    if refusals is None:
        return []
    if not isinstance(refusals, list) or not refusals or len(refusals) > MAX_REFUSALS:
        return [f"{where}: `refusals` must be a non-empty list, {MAX_REFUSALS} max"]
    errs: list[str] = []
    for i, r in enumerate(refusals):
        errs += _refusal_errors(f"{where}: refusal #{i}", r)
    return errs


def normalized(op: dict) -> dict:
    """The declared keys of `op` this module reads, normalized (absent ones absent)."""
    out: dict = {}
    if op.get("output"):
        out["output"] = {k: op["output"][k] for k in OUTPUT_KEYS if k in op["output"]}
    if op.get("refusals"):
        out["refusals"] = [{k: r[k] for k in ("code", "when", "body", "message", "retryable")
                            if k in r} for r in op["refusals"]]
    return out


def declares(op: dict) -> bool:
    return bool(op.get("output") or op.get("refusals"))


def served(op: dict, answer: Any) -> Any:
    """What the agent receives of `answer` (the parsed JSON of a success): `Refused` for
    a declared refusal whose condition holds, then the answer shaped by `output`
    (`serve.result`); `answer` itself when the action declares neither."""
    if not declares(op):
        return answer
    fn = {"name": op["name"], "refusals": op.get("refusals", [])}
    hit = serve.refusal({"connector": {"errors": []}}, fn, WHEN, answer)
    if hit is not None:
        raise Refused(hit["code"], hit["message"], hit["retryable"])
    if not op.get("output"):
        return answer
    try:
        return serve.result({**fn, "output": op["output"]}, [answer])
    except serve.ServeError:
        raise Refused(ITEMS_MISSING, (
            f"The answer of `{op['name']}` does not carry the list its definition "
            f"declares (`output.items` = `{op['output']['items']}`): the service "
            "answered something else. Nothing is read as empty."))
