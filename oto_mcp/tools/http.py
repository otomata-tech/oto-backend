"""`http` connector — generic multi-auth HTTP client (secret IN the oto vault).

To be distinguished from the bridge (`tools/remote.py`, ADR 0034): the bridge forwards to a
remote service that HOLDS the credential (custody outside the platform, M2M token);
here oto holds the target API's secret (AES-encrypted vault, byo_org) and calls
the API **directly**. The org configures on the HTTP card: `base_url`, `auth_mode`
(bearer/header/query/basic/oauth2/none) + the mode's secret(s).

Thin adapter (ADR 0037): the engine (auth + forward) lives in oto-core
(`oto.tools.http`); here we resolve the org credential and translate errors
into McpError. Three tools: `http_get` (read), `http_post` (POST with a JSON
body — paginated search, writes), `http_doc` (the API's contract, if
the operator filled in `doc_path` on the card — e.g. `/openapi.json` for a
bridge that exposes it behind the same auth as the rest).
It is an "HTTP node" (like n8n/Zapier), but the destination is controlled:
`oto_mcp/egress.py` refuses a `base_url` (or a `token_url`) that RESOLVES to an
internal address, except for a named exception declared at deployment. What a POST is
allowed to do is up to the target API (and, behind a bridge, ITS own
allowlist). Being ordinary MCP tools,
the result goes back through field redaction (FieldRedactionMiddleware).
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit, urlunsplit

import requests
from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS
from oto.tools.http import HttpConnectorClient

from .. import access, egress
from ..connectors import http_operations
from ..texte_tiers import cloture
from .lecture import LECTURE
from ..auth.hooks import current_user_sub_from_token

log = logging.getLogger("oto_mcp.tools.http")
TIMEOUT = 45

# Excerpt of the upstream error body surfaced to the agent (oto-backend#449). 500
# characters: enough for an API's message ("authorization expired, retry
# in a minute"), too short to copy an entire HTML error page
# into the model's context.
BODY_EXCERPT = 500

# Statuses that say "retry" and not "it's dead". DERIVED from the code alone, never
# from the body's prose. 502/504 are deliberately absent: a gateway can be
# durably down, and an agent that insists on a dead bridge costs more
# than an agent that hands control back.
RETRYABLE_STATUSES = frozenset({429, 503})


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="http_get",
        annotations=LECTURE,
        description=(
            "Read-only HTTP GET call to the API configured for your org "
            "(`http` connector). `path` = path relative to the base_url (starts "
            "with /). `params` = optional query params. The configured auth (bearer, "
            "API key, basic, oauth2) is injected automatically."
        ),
    )
    def http_get(path: str, params: dict | None = None) -> dict:
        if not isinstance(path, str) or not path.startswith("/"):
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message="`path` must start with / (path relative to base_url).",
            ))
        client = _client()
        try:
            return client.get(path, params)
        except requests.JSONDecodeError as e:
            return non_json_success(e)
        except ValueError as e:
            raise _value_error(e)
        except requests.HTTPError as e:
            raise _upstream_error(e)

    @mcp.tool(
        name="http_post",
        description=(
            "HTTP POST call to the API configured for your org (`http` connector). "
            "`path` = path relative to the base_url (starts with /). `body` = JSON "
            "body (dict/list). `params` = optional query params. The configured auth "
            "is injected automatically. Use for endpoints that require "
            "a POST (paginated search, write operations); what the POST is "
            "allowed to do depends on the target API."
        ),
    )
    def http_post(path: str, body: dict | list | None = None,
                  params: dict | None = None) -> dict:
        if not isinstance(path, str) or not path.startswith("/"):
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message="`path` must start with / (path relative to base_url).",
            ))
        client = _client()
        try:
            return client.post(path, json=body, params=params)
        except requests.JSONDecodeError as e:
            return non_json_success(e)
        except ValueError as e:
            raise _value_error(e)
        except requests.HTTPError as e:
            raise _upstream_error(e)

    @mcp.tool(
        name="http_doc",
        description=(
            "Fetches the documentation of the API configured for your org "
            "(`http` connector), if its operator filled in a `doc_path` route "
            "on the HTTP card (e.g. an OpenAPI contract in JSON). No parameter: "
            "the route comes from the config, never from the caller. Actionable error "
            "if `doc_path` is not set."
        ),
    )
    def http_doc() -> dict:
        client = _client()
        doc_path = _require_doc_path(_resolve_fields())
        try:
            return client.get(doc_path)
        except requests.JSONDecodeError as e:
            return non_json_success(e)
        except ValueError as e:
            raise _value_error(e)
        except requests.HTTPError as e:
            raise _upstream_error(e)


def _resolve_fields() -> dict:
    """Raw fields of the org's `http` credential — same resolution as `_client()`,
    split out so `http_doc` can read `doc_path` without rebuilding the client."""
    try:
        return access.resolve_credential_fields("http")
    # noqa: SILENT — same declared debt as _client() (#424, verdict C)
    except Exception:
        return {}


def _require_doc_path(fields: dict) -> str:
    """The configured `doc_path`, or an actionable McpError — split out of `http_doc`
    to stay testable without an MCP context (same pattern as `_excerpt`)."""
    doc_path = (fields.get("doc_path") or "").strip()
    if not doc_path:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                "http connector: no doc route configured for your org — "
                "set `doc_path` on the dashboard's HTTP card (e.g. /openapi.json)."
            ),
        ))
    return doc_path


def _client() -> HttpConnectorClient:
    """Resolve the org's `http` credential and instantiate the oto-core client.

    Raises an actionable McpError if the org has not configured its connector or if
    the config is invalid (non-http(s) scheme, unknown mode, missing mode field).

    ⚠️ This docstring once announced a "non-public host anti-SSRF" guard that did not
    exist (fixed on 2026-08-27, oto-backend#449), then claimed the absence of a
    guard was intentional and compensated by the platform's egress filtering.
    That filtering only blocks one range (link-local): loopback and private
    ranges remained reachable from an org `base_url`. The guard now
    exists, in `oto_mcp/egress.py` — it refuses an undeclared internal
    destination, `base_url` as well as `token_url` (oauth2 mode)."""
    sub = current_user_sub_from_token()
    if sub is None:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message="http connector unavailable in local stdio (org credential required).",
        ))
    return client_from_fields(_resolve_fields())


def client_for(fields: dict, headers: dict | None = None) -> HttpConnectorClient:
    """THE oto-core client for these credential fields — the only place one is built,
    for the agent's calls (`client_from_fields`) as for the REST import of a `doc_path`
    (`connectors/http_operations.fetch_doc_path_document`). The TWO destinations a
    card carries are guarded: the called base, and — in oauth2 mode — the token
    server, which goes out via `requests.post` from oto-core without passing through
    `base_url`. Guarding only the first would leave an entire outbound path outside the
    guard. `headers` = the saved definition's constant headers. Raises
    `egress.EgressRefused` or `ValueError` (incomplete or invalid config): each caller
    translates them for its face."""
    base_url = (fields.get("base_url") or "").strip()
    mode = (fields.get("auth_mode") or "").strip()
    if not base_url or not mode:
        raise ValueError("http connector not configured: `base_url` and `auth_mode` "
                         "are required on the Custom API card")
    egress.check_url(base_url, connector="http", field="base_url")
    token_url = (fields.get("token_url") or "").strip()
    if mode.lower() == "oauth2" and token_url:
        egress.check_url(token_url, connector="http", field="token_url")
    return http_operations.with_constant_headers(
        HttpConnectorClient(base_url, mode, fields, timeout=TIMEOUT), headers)


def client_from_fields(f: dict, headers: dict | None = None) -> HttpConnectorClient:
    """The client for these credential fields, refusals translated into actionable
    McpErrors — `_client()` for the call's credential, `http_act` for the row it
    resolved. Built by the single constructor, `client_for`; only the translation
    lives here."""
    if not (f.get("base_url") or "").strip() or not (f.get("auth_mode") or "").strip():
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                "http connector not configured for your org: set `base_url` + "
                "`auth_mode` (+ the mode's secret) on the dashboard's Custom API card."
            ),
        ))
    try:
        return client_for(f, headers)
    except egress.EgressRefused as e:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
    except ValueError as e:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=f"http connector: {e}"))


# A non-JSON success body, returned as text up to this size.
NON_JSON_MAX = 2000


def non_json_success(e: "requests.JSONDecodeError") -> dict:
    """A 2xx whose body is not JSON (e.g. `"Pet deleted"` under a JSON content type,
    seen live on a public API): the call SUCCEEDED upstream — said as such, never as a
    failure, or the agent retries a write that already went through. A JSON body keeps
    its shape untouched; only this case returns `{ok, non_json_body, truncated}`."""
    texte = e.doc or ""
    return {"ok": True, "non_json_body": texte[:NON_JSON_MAX],
            "truncated": len(texte) > NON_JSON_MAX}


def _excerpt(response) -> str:
    """The first characters of the upstream error body, cleanly truncated.

    No attempt to guess the SHAPE of the body: `http` is BYO — the org calls
    the API it chose and no error schema is known. Extracting
    `error.message` would work for one family of APIs and discard the reason for
    all the others; we return the text as is, bounded."""
    if response is None:
        return ""
    try:
        text = (response.text or "").strip()
    except Exception:  # noqa: SILENT — the body is a BONUS, the status is the contract: an undecodable body (broken encoding, cut stream) must neither raise nor make noise, it fades away and the error goes out with its status alone
        return ""
    if len(text) > BODY_EXCERPT:
        text = text[:BODY_EXCERPT].rstrip() + "…"
    return text


def _value_error(e: ValueError) -> McpError:
    """A refusal raised by the client before or instead of a response. A redirect
    (`RedirectRefused`, which carries `location`) is never followed — the injected
    auth would leave with it — so say where the API pointed and how to go there."""
    location = getattr(e, "location", None)
    if location is not None:
        return McpError(ErrorData(code=INVALID_PARAMS, message=(
            f"The API redirected ({getattr(e, 'status', '3xx')}) to "
            f"{_cible_sure(location) or 'an unnamed target'} — redirects are never "
            "followed, the connector's auth would go with them. If that target is "
            "intended, call its path directly.")))
    return McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))


def _cible_sure(location: str) -> str:
    """Scheme://host[:port]/path only: a 3xx often keeps the original query, so the
    key this connector injects as a parameter (or a userinfo) would otherwise reach
    the agent and `tool_calls.error`. A relative target keeps its path alone."""
    p = urlsplit(location or "")
    host = p.hostname or ""
    if host and p.port:
        host = f"{host}:{p.port}"
    return urlunsplit((p.scheme, host, p.path, "", ""))


def _upstream_error(e: requests.HTTPError) -> McpError:
    """Translate a target-API failure into a DIAGNOSTIC McpError: status, body
    excerpt, and structured `retryable`.

    Until 2026-08-27 this translation kept ONLY the status. A customer bridge
    down since the summer only ever returned "Target API: HTTP 502" — indistinguishable
    from a network outage, a service shut off or a right revoked on the customer side;
    it took opening a session on the box and reading `upstream=401` in the service's
    logs, which an agent cannot do (oto-backend#449).

    ⚠️ A third-party API's body is DATA, never an instruction: it
    reaches the agent in a labelled block, same pattern as a routine's
    payload (`routine_fire`). The risk "this body may carry an identifier or
    personal data" is ACCEPTED: this body is the org's data, which chose
    the API; an agent durably unable to tell "retry" from
    "it's dead" costs more. The status is never lost in favour of the body."""
    status = e.response.status_code if e.response is not None else 502
    retryable = status in RETRYABLE_STATUSES
    message = f"Target API: HTTP {status}"
    if retryable:
        message += " — temporary status, retrying is legitimate"
    body = _excerpt(e.response)
    if body:
        message += cloture("upstream-error-body", body,
                           origine="Body returned by the target API", lecture="a diagnostic")
    return McpError(ErrorData(code=INVALID_PARAMS, message=message,
                              data={"status": status, "retryable": retryable}))
