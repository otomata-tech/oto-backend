"""The SAVED DEFINITION of a custom connector (the `api` connector, "Custom API") —
everything non-secret an org configures ONCE and every member and session reuses.

It lives in `connector_instances.config` of the credential's instance, next to
`operations` (`custom_ops`), one key per part:

    {"label": "Acme Payroll",               # the connector's display name
     "description": "…",                     # what the tool is — read by the agent
     "logo_domain": "acme.test",             # brand domain for the logo, or null
     "headers": {"X-Api-Version": "2"},      # constant, NON-secret headers
     "variables": {"organizationId": {"value": "org_1", "description": "…"}},
     "operations": [...]}

PURE: validation only. `variables` fill an operation's argument of the same name
when the caller omits it (`custom_ops.plan_call`). A header that authenticates is
refused: the key lives in the vault, never in this clear-text definition.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from . import custom_ops

KEYS = ("label", "description", "logo_domain", "headers", "variables", "operations")
MAX_LABEL = 80
MAX_DESCRIPTION = 2000
MAX_HEADERS = 20
MAX_VARIABLES = 50
# A header name is an RFC 9110 token — `api_key` (underscore) is one, and real APIs use it.
_HEADER_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$")
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
# Headers that carry identity or routing: never a constant of a definition.
_FORBIDDEN_HEADERS = {"authorization", "cookie", "set-cookie", "host",
                      "proxy-authorization", "x-api-key", "api-key"}


def _header_errors(headers: Any, auth_header: Optional[str]) -> list[str]:
    if not isinstance(headers, dict):
        return ["`headers` must map header names to constant values"]
    errs = []
    if len(headers) > MAX_HEADERS:
        errs.append(f"`headers`: {len(headers)} headers, {MAX_HEADERS} max")
    for name, value in headers.items():
        low = str(name).lower()
        if not isinstance(name, str) or not _HEADER_RE.match(name):
            errs.append(f"header `{name}`: invalid name (an HTTP token: letters, digits, `-_.`…)")
        elif low in _FORBIDDEN_HEADERS or low.startswith("proxy-") or (
                auth_header and low == auth_header.lower()):
            errs.append(f"header `{name}` authenticates or routes the call — the key "
                        "lives in the vault, never in a constant header")
        if not isinstance(value, str) or len(value) > 500 or any(c in value for c in "\r\n"):
            errs.append(f"header `{name}`: the value must be a single-line string "
                        "(500 characters max)")
    return errs


def _variable_errors(variables: Any) -> list[str]:
    if not isinstance(variables, dict):
        return ["`variables` must map names to {value, description}"]
    errs = []
    if len(variables) > MAX_VARIABLES:
        errs.append(f"`variables`: {len(variables)} variables, {MAX_VARIABLES} max")
    for name, var in variables.items():
        if not isinstance(name, str) or not custom_ops.ARG_RE.match(name):
            errs.append(f"variable `{name}`: invalid name (an argument name)")
            continue
        if not isinstance(var, dict) or set(var) - {"value", "description"}:
            errs.append(f"variable `{name}`: must be {{value, description}}")
            continue
        if not isinstance(var.get("value"), str) or len(var["value"]) > 500:
            errs.append(f"variable `{name}`: `value` must be a string (500 max)")
        d = var.get("description", "")
        if not isinstance(d, str) or len(d) > 500:
            errs.append(f"variable `{name}`: `description` must be a string (500 max)")
    return errs


def validate_definition(definition: Any, *, auth_header: Optional[str] = None) -> dict:
    """The definition normalized — only the keys it carries, so a partial save
    leaves the others as they are — or `custom_ops.OperationsInvalid` naming every
    problem. `auth_header` = the card's `header_name` (header mode): refused as a
    constant header."""
    if not isinstance(definition, dict):
        raise custom_ops.OperationsInvalid(["the definition must be an object"])
    errs = [f"unknown key `{k}` (accepted: {', '.join(KEYS)})"
            for k in definition if k not in KEYS]
    out: dict = {}
    if "label" in definition:
        label = definition["label"]
        if label is not None and (not isinstance(label, str) or len(label.strip()) > MAX_LABEL):
            errs.append(f"`label` must be a string ({MAX_LABEL} max)")
        else:
            out["label"] = (label or "").strip() or None
    if "description" in definition:
        d = definition["description"]
        if d is not None and (not isinstance(d, str) or len(d) > MAX_DESCRIPTION):
            errs.append(f"`description` must be a string ({MAX_DESCRIPTION} max)")
        else:
            out["description"] = (d or "").strip() or None
    if "logo_domain" in definition:
        dom = definition["logo_domain"]
        if dom is not None and (not isinstance(dom, str)
                                or not _DOMAIN_RE.match(dom.strip().lower())):
            errs.append("`logo_domain` must be a bare domain (e.g. acme.test) or null")
        else:
            out["logo_domain"] = dom.strip().lower() if dom else None
    if "headers" in definition:
        errs += _header_errors(definition["headers"] or {}, auth_header)
        out["headers"] = dict(definition["headers"] or {})
    if "variables" in definition:
        errs += _variable_errors(definition["variables"] or {})
        out["variables"] = {k: {"value": v.get("value", ""),
                                "description": v.get("description", "")}
                            for k, v in (definition["variables"] or {}).items()
                            if isinstance(v, dict)}
    if "operations" in definition:
        try:
            out["operations"] = custom_ops.validate_operations(definition["operations"])
        except custom_ops.OperationsInvalid as e:
            errs += e.errors
    if errs:
        raise custom_ops.OperationsInvalid(errs)
    return out
