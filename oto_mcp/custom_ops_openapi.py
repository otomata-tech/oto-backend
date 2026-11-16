"""An OpenAPI 3 (or Swagger 2) JSON document → CANDIDATE operations for `custom_ops`.

PURE: the document arrives already parsed, the candidates leave unsaved — the org
reviews them and saves what it keeps (`PUT …/connectors/api/operations`). What
cannot be expressed in the format is SKIPPED with its reason, never approximated:
a header or cookie parameter (the connector's auth owns the headers), a form body,
a non-object JSON body, a path the validator refuses.

Paths are taken as written in the document: they are relative to the API's server
URL, which is what the org enters as `base_url` (e.g. `https://api.acme.test/v1`).
Only LOCAL `$ref`s are followed (`#/components/…`, `#/parameters/…`,
`#/definitions/…`), to a bounded depth — a document is data, not a program.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from . import custom_ops

_METHODS = ("get", "post", "put", "patch", "delete")
_IGNORED_METHODS = ("head", "options", "trace")
_MAX_REF_DEPTH = 8


class NotAnOpenApiDocument(ValueError):
    """Neither `openapi: 3.x` nor `swagger: 2.0`."""


def _deref(doc: dict, node: Any, depth: int = 0) -> Any:
    if not isinstance(node, dict) or "$ref" not in node:
        return node
    ref = node["$ref"]
    if depth >= _MAX_REF_DEPTH or not isinstance(ref, str) or not ref.startswith("#/"):
        return {}
    cur: Any = doc
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(cur, dict) or part not in cur:
            return {}
        cur = cur[part]
    return _deref(doc, cur, depth + 1)


def snake_case(text: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text or "")
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    if not s or not s[0].isalpha():
        s = "op_" + s
    return s[:63].rstrip("_")


def _fallback_name(method: str, path: str) -> str:
    parts = [p for p in path.split("/") if p and not p.startswith("{")]
    return snake_case(f"{method}_{'_'.join(parts) or 'root'}")


def _prop(doc: dict, schema: Any, description: Optional[str] = None) -> dict:
    """A parameter schema reduced to what the format checks (type, description) plus
    `enum` and `items.type` when present — enough for the agent to fill it."""
    schema = _deref(doc, schema) or {}
    out: dict = {}
    t = schema.get("type")
    if isinstance(t, list):
        t = [x for x in t if x in custom_ops.JSON_TYPES] or None
    elif t not in custom_ops.JSON_TYPES:
        t = None
    if t:
        out["type"] = t
    desc = description or schema.get("description") or schema.get("title")
    if isinstance(desc, str) and desc.strip():
        out["description"] = desc.strip()[:500]
    if isinstance(schema.get("enum"), list):
        out["enum"] = schema["enum"][:50]
    items = _deref(doc, schema.get("items"))
    if t == "array" and isinstance(items, dict) and items.get("type") in custom_ops.JSON_TYPES:
        out["items"] = {"type": items["type"]}
    return out


def _json_body_schema(doc: dict, op: dict, swagger: bool) -> tuple[Optional[dict], Optional[str]]:
    """The JSON body's object schema, `(None, None)` if there is no body, or
    `(None, reason)` if it cannot be expressed."""
    if swagger:
        for p in op.get("parameters") or []:
            p = _deref(doc, p)
            if p.get("in") == "formData":
                return None, "form-data body is not supported"
            if p.get("in") == "body":
                schema = _deref(doc, p.get("schema"))
                break
        else:
            return None, None
    else:
        rb = _deref(doc, op.get("requestBody"))
        if not rb:
            return None, None
        content = rb.get("content") or {}
        media = next((m for m in content if m.split(";")[0].strip() == "application/json"
                      or m.endswith("+json")), None)
        if media is None:
            return None, (f"request body is {', '.join(content) or 'without content'}"
                          " — only application/json is supported")
        schema = _deref(doc, content[media].get("schema"))
    if not isinstance(schema, dict):
        return None, "request body has no schema"
    if schema.get("allOf"):
        merged: dict = {"type": "object", "properties": {}, "required": []}
        for part in schema["allOf"]:
            part = _deref(doc, part)
            merged["properties"].update(part.get("properties") or {})
            merged["required"] += part.get("required") or []
        schema = merged
    if schema.get("type", "object") != "object" or not isinstance(
            schema.get("properties"), dict):
        return None, "request body is not a JSON object with declared properties"
    return schema, None


def _convert(doc: dict, path: str, method: str, op: dict, path_params: list,
             swagger: bool) -> tuple[Optional[dict], Optional[str]]:
    props: dict = {}
    required: list = []
    in_map: dict = {}
    params = {}
    for p in [*path_params, *(op.get("parameters") or [])]:
        p = _deref(doc, p)
        if isinstance(p, dict) and p.get("name"):
            params[(p.get("in"), p["name"])] = p     # operation-level overrides path-level
    for (loc, name), p in params.items():
        if loc in ("body", "formData"):
            continue
        if loc in ("header", "cookie"):
            if p.get("required"):
                return None, f"required {loc} parameter `{name}` is not supported"
            continue
        if loc not in ("path", "query"):
            return None, f"parameter `{name}` in `{loc}` is not supported"
        schema = p.get("schema") if not swagger else {
            k: p[k] for k in ("type", "enum", "items") if k in p}
        props[name] = _prop(doc, schema, p.get("description"))
        if loc == "path" or p.get("required"):
            required.append(name)
        if loc == "query" and method in ("post", "put", "patch"):
            in_map[name] = "query"
    body, reason = _json_body_schema(doc, op, swagger)
    if reason:
        return None, reason
    if body is not None:
        for name, schema in body["properties"].items():
            if name in props:
                return None, f"body property `{name}` collides with a parameter"
            props[name] = _prop(doc, schema)
            if method in ("get", "delete"):
                in_map[name] = "body"
        required += [r for r in body.get("required") or [] if r in body["properties"]]
    desc_parts = [x.strip() for x in (op.get("summary"), op.get("description"))
                  if isinstance(x, str) and x.strip()]
    description = " — ".join(dict.fromkeys(desc_parts)) or f"{method.upper()} {path}"
    if len(description) > custom_ops.MAX_DESCRIPTION:
        description = description[:custom_ops.MAX_DESCRIPTION - 1].rstrip() + "…"
    candidate = {
        "name": snake_case(op["operationId"]) if isinstance(op.get("operationId"), str)
        else _fallback_name(method, path),
        "description": description, "method": method.upper(), "path": path,
        "input": {"type": "object", "properties": props,
                  "required": list(dict.fromkeys(required))},
    }
    if in_map:
        candidate["in"] = in_map
    return candidate, None


def from_openapi(doc: Any) -> tuple[list[dict], list[dict]]:
    """`(candidates, skipped)` — candidates already validated one by one by
    `custom_ops`, names de-duplicated, capped at `custom_ops.MAX_OPERATIONS`;
    `skipped` = `[{path, method, reason}]`."""
    if not isinstance(doc, dict):
        raise NotAnOpenApiDocument("the document is not a JSON object")
    swagger = str(doc.get("swagger", "")).startswith("2")
    if not swagger and not str(doc.get("openapi", "")).startswith("3"):
        raise NotAnOpenApiDocument(
            "not an OpenAPI 3 nor a Swagger 2 document (no `openapi: 3.x` / "
            "`swagger: 2.0` key) — only JSON is read, not YAML")
    candidates: list[dict] = []
    skipped: list[dict] = []
    names: set[str] = set()
    for path, item in (doc.get("paths") or {}).items():
        item = _deref(doc, item)
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method in ("parameters", "summary", "description", "servers") or \
                    method.startswith("x-"):
                continue
            if method in _IGNORED_METHODS or method not in _METHODS:
                skipped.append({"path": path, "method": method.upper(),
                                "reason": f"method {method.upper()} is not supported"})
                continue
            if not isinstance(op, dict):
                continue
            cand, reason = _convert(doc, path, method, op, item.get("parameters") or [],
                                    swagger)
            if cand is not None:
                base, n = cand["name"], 2
                while cand["name"] in names:
                    cand["name"] = f"{base[:60]}_{n}"
                    n += 1
                try:
                    cand = custom_ops.validate_operations([cand])[0]
                except custom_ops.OperationsInvalid as e:
                    cand, reason = None, "; ".join(e.errors)
            if cand is None:
                skipped.append({"path": path, "method": method.upper(), "reason": reason})
            elif len(candidates) >= custom_ops.MAX_OPERATIONS:
                skipped.append({"path": path, "method": method.upper(), "reason": (
                    f"over the {custom_ops.MAX_OPERATIONS}-operation limit")})
            else:
                names.add(cand["name"])
                candidates.append(cand)
    return candidates, skipped
