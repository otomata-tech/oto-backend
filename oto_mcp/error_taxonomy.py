"""Tool error taxonomy — shared classification + scrub (D2, oto-backend#124).

Single place that CLASSIFIES a tool exception surfaced by fastmcp (machine category
`code` + `retryable`) and SCRUBS its message for the agent. Reused by:

- `sentry_setup`: decide whether an error is a backend bug (report) or handled (drop) —
  the `_is_*` predicates below;
- `ErrorEnvelopeMiddleware` (`middleware/error_envelope.py`): hand the agent an error with a
  **uniform contract** `{code, retryable, hint}`, without stacktrace / internal route /
  technical id (`classify` + `scrub`).

fastmcp wraps a tool's error in a `ToolError` → all the predicates **walk up the
chain** `__cause__`/`__context__` to the original exception.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Iterator, Optional

from fastmcp.exceptions import NotFoundError
from . import outils_retires
from .bornes_processus import DelaiOutilDepasse
from .mcp_errors import McpError
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS, INVALID_REQUEST
from pydantic import ValidationError

# JSON-RPC codes for user INPUT/CONFIG errors (native counterpart of an upstream 4xx):
# "set your key", "connect your account", invalid param/org. Raised
# intentionally by the tools/capabilities, not backend bugs.
_USER_INPUT_CODES = {INVALID_PARAMS, INVALID_REQUEST}


def _chain(exc) -> Iterator[BaseException]:
    """The exception and its cause chain (`__cause__` then `__context__`), without cycles."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _upstream_status(exc) -> Optional[int]:
    """Upstream HTTP code carried by ONE exception, else None.

    Covers `UpstreamHTTPError` (oto-core, `.status_code`), `httpx`/`requests`
    HTTPError (`.response.status_code`) and our own typed connector errors
    (`.status`, e.g. `NinjaError`).
    """
    for attr in ("status_code", "status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    v = getattr(getattr(exc, "response", None), "status_code", None)
    return v if isinstance(v, int) else None


def _upstream_retryable(exc) -> Optional[bool]:
    """Retry semantics DECLARED by the upstream connector, else None.

    The HTTP status alone lies with some providers: Hunter returns 429 for
    "plan credits exhausted" (nothing to retry) and 403 for the rate limit
    (transient) — the opposite of the convention. Only the connector module
    knows; it says so via a `retryable` attribute on its exception, and the taxonomy
    honors it. Generic seam, specifics INSIDE the module (never an `if hunter`).
    """
    for e in _chain(exc):
        v = getattr(e, "retryable", None)
        if isinstance(v, bool):
            return v
    return None


def upstream_status_in_chain(exc) -> Optional[int]:
    """First upstream HTTP code found walking up the chain, else None."""
    for e in _chain(exc):
        sc = _upstream_status(e)
        if sc is not None:
            return sc
    return None


def credential_rejected_in_chain(exc) -> bool:
    """True if the upstream refused the KEY itself — what marks the served key red at
    call time (`connectors.health.suivre_appel`).

    A connector that knows says so on its exception (`credential_rejected: bool`), and
    its verdict wins — same seam as `retryable`, the specifics stay inside the module.
    Otherwise only a 401 anywhere in the chain counts, including under the curated
    `McpError` a tool raises in its `except`: a 401 is the key failing to
    authenticate. A 403 alone does NOT: it is as often a resource the key may not read
    (a private file, a profile out of reach) or a provider's rate limit (Hunter) —
    painting the key red for it would send people replacing a key that works."""
    for e in _chain(exc):
        v = getattr(e, "credential_rejected", None)
        if isinstance(v, bool):
            return v
    return upstream_status_in_chain(exc) == 401


def _is_managed_connector_error(exc) -> bool:
    """True if the chain carries an upstream client refusal (4xx) — a handled
    connector error, not a backend bug."""
    for e in _chain(exc):
        sc = _upstream_status(e)
        if sc is not None and 400 <= sc < 500:
            return True
    return False


def _is_user_input_error(exc) -> bool:
    """True if the chain carries an `McpError` with a user input/config code
    (INVALID_PARAMS / INVALID_REQUEST) — explicit refusal, not a backend bug."""
    for e in _chain(exc):
        if isinstance(e, McpError) and getattr(e.error, "code", None) in _USER_INPUT_CODES:
            return True
    return False


def _is_arg_validation_error(exc) -> bool:
    """True if the chain carries a pydantic `ValidationError` (args rejected)."""
    for e in _chain(exc):
        if isinstance(e, ValidationError):
            return True
    return False


# Pydantic types of a SIGNATURE error (hand-written tool, validated by FastMCP 3),
# and its title: `call[data_write]` — of which we only serve the tool name (oto#135).
_TYPES_DE_SIGNATURE = frozenset({"unexpected_keyword_argument", "missing_argument",
                                 "unexpected_positional_argument", "multiple_argument_values"})
_TITRE_D_OUTIL = re.compile(r"^(?:call\[)?([A-Za-z_]\w*)\]?$")


def outil_de_signature(exc) -> Optional[str]:
    """The name of the tool whose SIGNATURE refused the arguments, or `None`.

    FastMCP 3 titles the error `call[data_write]`: the name is READ from the title. The title
    of a MODEL error names a class, never served as a tool name."""
    err = next((e for e in _chain(exc) if isinstance(e, ValidationError)), None)
    if err is None:
        return None
    try:
        signature = any((d.get("type") or "") in _TYPES_DE_SIGNATURE for d in err.errors())
    # noqa: SILENT — unexpected pydantic shape: no tool name, the message stays
    except Exception:  # noqa: BLE001
        return None
    m = _TITRE_D_OUTIL.match(err.title or "") if signature else None
    return m.group(1) if m else None


def _op_deplacee(exc) -> Optional["outils_retires.Retrait"]:
    """The WRITTEN refusal of an `op` a still-served tool gave to its `<ns>_act`
    (`outils_retires.OPS_DEPLACEES`), or None. The signature refuses it as an unknown
    literal (`literal_error` on `op`); FastMCP 3 titles the error `call[<tool>]`, the
    function's name — that is the tool's canonical name, whatever a tenant prefix."""
    err = next((e for e in _chain(exc) if isinstance(e, ValidationError)), None)
    if err is None:
        return None
    m = _TITRE_D_OUTIL.match(err.title or "")
    if not m:
        return None
    try:
        for d in err.errors():
            if (d.get("type") == "literal_error" and tuple(d.get("loc") or ()) == ("op",)
                    and isinstance(d.get("input"), str)):
                return outils_retires.op_deplacee(m.group(1), d["input"])
    # noqa: SILENT — unexpected pydantic shape: the generic refusal naming the key stays
    except Exception:  # noqa: BLE001
        return None
    return None


def _arg_error_message(exc, parametres: Optional[list] = None) -> str:
    """"Invalid arguments" that NAMES the faulty key — parity with the REST face.

    `parametres` = the tool's parameters, when the surface knows them
    (`ErrorEnvelopeMiddleware`): an unknown key that resembles one of them makes the
    gesture to replay explicit ("Replay `data_write(…)` with `rows=` instead of
    `rows_data`", oto#135) instead of making the agent reread the whole schema.

    The REST face refuses an unknown field by naming the excess AND the expected ones
    (`_rest_adapter`, 400 `unknown_fields`); the MCP face said "check the tool's
    parameters", which leaves you guessing WHICH. Measured on 14/08: two faulty forms
    (`{op:"draft"}`, `{action:"draft"}`) refused without naming the key, then the call
    recomposed from scratch — forgetting the parameter being sought for four attempts.

    The pydantic `ValidationError` carries everything: `loc` = the key, `type` = the nature
    of the refusal (`extra_forbidden` = unknown key, `missing` = required key absent)."""
    err = next((e for e in _chain(exc) if isinstance(e, ValidationError)), None)
    if err is None:
        return "Invalid arguments — check the tool's parameters."
    inconnus, manquants, autres = [], [], []
    valeurs: dict = {}
    try:
        for d in err.errors():
            cle = ".".join(str(p) for p in (d.get("loc") or ())) or "?"
            kind = d.get("type") or ""
            # FastMCP 3 types an unknown key `unexpected_keyword_argument` (oto#135).
            if kind in ("extra_forbidden", "unexpected_keyword_argument"):
                inconnus.append(cle)
                valeurs[cle] = d.get("input")
            elif kind.startswith("missing"):
                manquants.append(cle)
            else:
                autres.append(f"{cle} ({d.get('msg') or kind})")
    # noqa: SILENT — degraded help message, the taxonomy returns its default
    except Exception:      # unexpected pydantic shape: don't break the message
        return "Invalid arguments — check the tool's parameters."
    outil = outil_de_signature(err)
    from . import deprecations  # late import: the taxonomy is imported everywhere
    # oto#127: the old head settings of `data_patch_schema` are refused
    # TOGETHER — the equivalent is computed on the combination received.
    if outil == "data_patch_schema":
        from .datastore import reglages
        refus = reglages.refus_parametres({c: valeurs.get(c) for c in inconnus}, outil)
        if refus:
            return "Invalid arguments — " + refus
    for cle in inconnus:
        refus = deprecations.refus_parametre_renomme(cle, valeurs.get(cle), outil)
        if refus:  # the new name is then not "required but absent": it is misnamed
            return "Invalid arguments — " + refus
    # oto#135: an unknown key that resembles a tool parameter has a
    # destination — the refusal says it, and the parameter thus named is no longer a
    # "required but absent": it is misspelled.
    proches = {}
    for cle in inconnus:
        trouve = get_close_matches(cle, list(parametres or ()), n=1, cutoff=0.6)
        if trouve:
            proches[cle] = trouve[0]
    manquants = [c for c in manquants if c not in proches.values()]
    bouts = []
    if inconnus:
        bouts.append(f"unrecognized field(s): {', '.join(inconnus)}")
    if manquants:
        bouts.append(f"required field(s) missing: {', '.join(manquants)}")
    if autres:
        bouts.append(f"rejected value(s): {'; '.join(autres)}")
    if not bouts:
        return "Invalid arguments — check the tool's parameters."
    schema = f'oto_tool_schema(name="{outil}")' if outil else "oto_tool_schema(name=…)"
    rejeu = ""
    if proches:
        appel = f"`{outil}(…)`" if outil else "the same call"
        rejeu = (f" Replay {appel} with "
                 + ", ".join(f"`{p}=` instead of `{c}`" for c, p in proches.items())
                 + ".")
    return ("Invalid arguments — " + " · ".join(bouts) + "." + rejeu
            + f" The exact schema: {schema}.")


def _is_oauth_exchange_refused(exc) -> bool:
    """True if the chain carries a REFUSAL from the authorization server (`OAuthExchangeRefused`).

    The refusal describes the USER's Connected App or grant — expired code, missing scopes,
    mismatched callback, IP restriction — never our code. The chain is enough:
    each connector re-raises its translated message `from e`, so the original cause remains
    visible here without the taxonomy having to know a single connector by name.

    Local import: `oauth_flow` imports the config at load time, and this module is imported
    very early by the Sentry middleware."""
    try:
        from .auth.flow import OAuthExchangeRefused
    # noqa: SILENT — shape predicate: undecidable => False (no OAuth guessed)
    except Exception:
        return False
    return any(isinstance(e, OAuthExchangeRefused) for e in _chain(exc))


def _is_upstream_managed_error(exc) -> bool:
    """True if the chain carries an upstream connector INPUT/config error WITHOUT an
    HTTP status (oto-backend#90): LinkedIn facet not found, account not connected,
    unsupported param, identity_mismatch… `UnipileError` (oto-core) models this — a
    user input refusal, never a backend bug. 4xx errors already carry `.status_code`
    (covered by `_is_managed_connector_error`); NETWORK errors (message "network")
    stay reported (transient, potential outage, not an input problem).

    Recognized by class NAME (`UnipileError`) so as not to couple the taxonomy to
    the oto-core import (the module must remain importable on its own, without cycles)."""
    for e in _chain(exc):
        if type(e).__name__ == "UnipileError" and getattr(e, "status_code", None) is None:
            if "network" not in str(e).lower():
                return True
    return False


# CLIENT disconnect while we were replying. The MCP client closes the POST
# (tab closed, conversation abandoned, timeout on the claude.ai side) and the server writes
# into an already-dead stream. Nothing went wrong ON OUR SIDE: there is no one left at the
# other end of the line. Two forms of the SAME incident, chained in the same event:
#   - `ClosedResourceError` (anyio) when the MCP SDK pushes into the closed stream;
#   - `RuntimeError: Unexpected ASGI message … after response already completed`
#     when uvicorn refuses the headers of an already-finished response.
# 38 Sentry events in 3 weeks, none actionable.
_CLIENT_DISCONNECT_TYPES = {
    "ClosedResourceError", "BrokenResourceError", "EndOfStream", "ClientDisconnect",
}
_ASGI_AFTER_COMPLETE = "after response already completed"


def _is_client_disconnect(exc) -> bool:
    """True if the chain carries a client disconnect mid-response.

    Recognized by class NAME (like `_is_upstream_managed_error`): the taxonomy must
    not import anyio or the MCP SDK so it can remain importable on its own.

    ⚠️ DELIBERATELY outside `_is_expected_error`: this predicate does not answer the
    same question. `_is_expected_error` = "should the agent be held responsible?",
    and is also used by `ErrorEnvelopeMiddleware` to compose the response RETURNED to
    the agent. Here, there is no agent left to answer — the only decision remaining
    is "should someone be woken up?", which is a Sentry question. Hence the separate
    call in `_before_send`.
    """
    for e in _chain(exc):
        if type(e).__name__ in _CLIENT_DISCONNECT_TYPES:
            return True
        if isinstance(e, RuntimeError) and _ASGI_AFTER_COMPLETE in str(e):
            return True
    return False


_UNKNOWN_TOOL = re.compile(r"Unknown tool: '([^']+)'")


def _unknown_tool_name(exc) -> Optional[str]:
    """Tool name if the chain carries fastmcp's "Unknown tool" dispatch
    refusal — the tool is not mounted in THIS session (connector not installed,
    ADR 0019/0050 selection, or hidden tool). Visibility filters `tools/list`,
    not `tools/call`: an agent can still TRY a name (it infers it from an
    instance ref, the catalog, a conversation) → the server refusal must
    be actionable, not an opaque 500 (experienced 2026-07-16, signals #224/#225:
    two agents concluded a credential bug). None otherwise."""
    for e in _chain(exc):
        if isinstance(e, NotFoundError):
            m = _UNKNOWN_TOOL.search(str(e))
            if m:
                return m.group(1)
    return None


def _connector_of_tool(name: str) -> Optional[str]:
    """Connector owning the namespace of `name`, if the registry knows it.
    Lazy import — the taxonomy remains importable on its own (and without cycles)."""
    try:
        from . import providers
        from .tool_visibility import namespace_of
        con = providers.connector_for_namespace(namespace_of(name))
        return con.name if con else None
    # noqa: SILENT — connector-ownership hint: absent rather than wrong
    except Exception:
        return None


def _surviving_siblings(name: str) -> Optional[list[str]]:
    """The tools of the SAME namespace that still exist — when `name` itself does not.

    `None` = nothing can be asserted: the name IS in the registry (so it is not removed,
    just not mounted), or the registry is not warmed up (outside the server it returns an empty
    list — concluding "the tool no longer exists" would make EVERY message lie), or its
    namespace has nothing left to offer.

    DERIVED, never a rename table to maintain: a table would have to be fed on each
    consolidation, hence stale at the first oversight — and it is exactly this kind of oversight
    that produces the misleading message we are closing here."""
    try:
        from . import tool_registry
        from .tool_visibility import namespace_of
        connus = set(tool_registry.boot_tool_names())
        if not connus or name in connus:
            return None
        ns = namespace_of(name)
        voisins = sorted(t for t in connus if namespace_of(t) == ns)
        return voisins or None
    # noqa: SILENT — sibling hint: absent rather than wrong
    except Exception:
        return None


def _refus_nomme_hors_http(exc):
    """The refusal a connector DESCRIPTION names (`tools.decrits.RefusNomme`), when no HTTP error status carries it:
    a refusal read in a 200 answer's body (DataForSEO's `status_code` 40101, 40602…) or a check of the arguments or
    of the answer (`status_code` None). Its message is the description's, written by us, never the vendor's body.
    Matched by name, as `UnipileError`: `tools.decrits` imports this module's world, not the reverse. With a 4xx/5xx
    status, path (3) of `classify` reads it as any upstream error."""
    for e in _chain(exc):
        if type(e).__name__ == "RefusNomme":
            sc = getattr(e, "status_code", None)
            return e if sc is None or sc < 400 else None
    return None


def _refus_nomme_d_un_403(exc):
    """The refusal a connector DESCRIPTION names under a 403 that does not mark the key: a right the account lacks on
    one resource (a property shared Restricted, where submitting needs Owner or Full). Its message is ours and says
    the gesture, so it is served under its own code — never "check that the connector is connected", which sends
    people replacing a key that works (13/11/2026)."""
    for e in _chain(exc):
        if type(e).__name__ == "RefusNomme":
            return e if e.status_code == 403 and not e.credential_rejected else None
    return None


def _is_expected_error(exc) -> bool:
    """Handled error, NOT to be reported to Sentry: upstream 4xx OR user input/config
    refusal OR rejected args OR OAuth exchange refusal OR unmounted tool (toolbox
    condition, not a bug).
    Real code exceptions (5xx, KeyError, InvalidTag…) stay reported."""
    return (_is_managed_connector_error(exc)
            or _is_user_input_error(exc)
            or _is_arg_validation_error(exc)
            or _is_upstream_managed_error(exc)
            or _is_oauth_exchange_refused(exc)
            or _refus_nomme_hors_http(exc) is not None
            or _unknown_tool_name(exc) is not None)


# --- Error envelope returned to the agent (D2) --------------------------------

@dataclass
class ErrorInfo:
    """Normalized error presented to the agent. `code` = machine category;
    `retryable` = the agent can retry as is; `message` scrubbed (zero
    stacktrace/route/id); `hint` = what to do, when derivable."""
    code: str
    retryable: bool
    message: str
    hint: Optional[str] = None
    # Connector at fault, when derivable from the tool name (`tool_not_mounted`).
    # The classifier stays PURE (it only sees an exception): it is the envelope, which has
    # the session context, that uses it to enrich the hint (instances in scope).
    connector: Optional[str] = None


#: The guidance returned with `quota_exhausted` (402): nothing to fix in the call.
QUOTA_HINT = ("no point retrying or fixing the call: the provider account "
              "is out of credit — top up its credits with the provider, or set another key")


# net::ERR_* (raw Chromium errors) — replace the whole message (no useful info).
_NET_ERR = re.compile(r"net::ERR_[A-Z_]+")
# Internal routes ("Cannot GET /api/v1/…", API paths) — server topology leak.
_ROUTE = re.compile(r"(?:Cannot\s+(?:GET|POST|PUT|DELETE|PATCH)\s+)?/(?:api|v\d)[\w/.\-]*", re.I)
# Long technical tokens (account_id, uuid) >= 20 chars — internal identifier leak.
_LONG_ID = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9_\-]{19,}\b")
_TIMEOUT_MARKERS = ("timeout", "timed out", "délai d'attente", "read timed out")


def scrub(message: str) -> str:
    """Strip internal leaks (net::ERR_*, routes, technical ids) from an error message.
    Best-effort — applied to upstream messages, never to the `McpError`s
    we curated ourselves."""
    if not message:
        return ""
    if _NET_ERR.search(message):
        return "Upstream network failure (host not resolved or service unreachable)."
    message = _ROUTE.sub("[internal route]", message)
    message = _LONG_ID.sub("[id]", message)
    return message.strip()


def _first_upstream_message(exc) -> str:
    """Str of the 1st exception in the chain carrying an upstream status (for scrub)."""
    for e in _chain(exc):
        if _upstream_status(e) is not None:
            return str(e)
    return str(exc)


#: Longest upstream detail echoed on a 5xx: a body can be a whole error page.
_DETAIL_5XX_MAX = 300


def _detail_5xx(raw: str) -> str:
    """The upstream's own words on a 5xx, or "" when they say nothing useful: empty,
    an HTML page (a proxy's error page), or a bare restatement of the status."""
    texte = (raw or "").strip()
    if not texte or "<html" in texte.lower() or "<!doctype" in texte.lower():
        return ""
    texte = " ".join(texte.split())
    return texte if len(texte) <= _DETAIL_5XX_MAX else texte[:_DETAIL_5XX_MAX] + "…"


def _looks_like_timeout(exc) -> bool:
    for e in _chain(exc):
        if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
            return True
        if any(m in str(e).lower() for m in _TIMEOUT_MARKERS):
            return True
    return False


def classify(exc, parametres: Optional[list] = None) -> ErrorInfo:
    """Classify a tool exception into an `ErrorInfo` with the uniform contract.

    Order: (1) `McpError` we raised (curated message kept); (2) rejected pydantic
    args; (3) upstream HTTP status (timeout/rate-limit/not-found/authz/4xx/5xx);
    (4) untyped timeout; (5) the rest = internal — **no echo of `str(exc)`** (anti-leak).
    """
    # (00) The process-wide tool-call bound (#1232, `bornes_processus`, enforced mode):
    # the call was abandoned by US after the bound. Said as such, with the bound —
    # never "internal error", nor an upstream timeout the agent could just retry.
    for e in _chain(exc):
        if isinstance(e, DelaiOutilDepasse):
            return ErrorInfo(
                "tool_timeout", False,
                f"The tool call exceeded the server's limit of {e.secondes:g} s and "
                "was abandoned.",
                "narrow the request (fewer rows, pages or items) and call again; a "
                "write may still have completed — check before repeating it")

    # (0) Credits exhausted: an upstream 402, wherever it is in the chain — including
    # under the curated `McpError` that a tool raises in its `except` (theirstack, AI Ark
    # do this). Without this, the curation won and the agent received `invalid_input`:
    # "fix your call", on a call that was right and an account out of credit. The curated
    # message is kept (it already says what to do); the CODE gives the category, and it is
    # what the envelope reads to mark the served key (`connectors.health`).
    if upstream_status_in_chain(exc) == 402:
        curated = next((((getattr(e.error, "message", None) or "").strip())
                        for e in _chain(exc) if isinstance(e, McpError)), "")
        return ErrorInfo("quota_exhausted", False,
                         curated or scrub(_first_upstream_message(exc))
                         or "Credits exhausted on the upstream service.",
                         QUOTA_HINT)

    # (0b) A refusal a connector description names, carried by no HTTP error status (a 200 answer whose body
    # refuses, a check of the arguments or of the answer): its message is ours, written in the description, so it is
    # served as is, under its own code. Without this, path (5) answered "Internal server error." to "the task is not
    # done yet: read it again later" (DataForSEO, 12/11/2026).
    refus = _refus_nomme_hors_http(exc)
    if refus is not None:
        message = str(refus)
        if refus.marks_account == "quota_exhausted":
            return ErrorInfo("quota_exhausted", False, message, QUOTA_HINT)
        if refus.marks_account == "credential_rejected":
            return ErrorInfo("not_authorized", False, message,
                             "check that the connector is connected and authorized")
        return ErrorInfo(refus.code, bool(refus.retryable), message,
                         "retry in a moment" if refus.retryable else None)

    # (1) McpError curated by a tool/capability: message already agent-facing.
    for e in _chain(exc):
        if isinstance(e, McpError):
            jcode = getattr(e.error, "code", None)
            msg = (getattr(e.error, "message", None) or "").strip()
            if jcode in _USER_INPUT_CODES:
                return ErrorInfo("invalid_input", False, msg or "Invalid request.")
            # McpError raised with another code (rare): we keep the curated text,
            # treated as internal non-retryable.
            return ErrorInfo("internal", False, msg or "Internal server error.")

    # (2) Rejected arguments (the LLM passed bad parameters) — NAMING the key. An op
    # that moved to `<ns>_act` names its new call (`outils_retires.OPS_DEPLACEES`).
    if _is_arg_validation_error(exc):
        deplacee = _op_deplacee(exc)
        if deplacee is not None:
            return ErrorInfo("invalid_input", False, deplacee.message, deplacee.hint)
        return ErrorInfo("invalid_input", False, _arg_error_message(exc, parametres))

    # (2b) fastmcp dispatch refusal: the tool is registered server-side but not
    # mounted in THIS session (connector not installed / hidden). Made actionable
    # with both routes: `oto_call` (immediate, no installation — ADR 0036) or
    # installing the connector. Without this: "Internal server error".
    name = _unknown_tool_name(exc)
    if name:
        # A name DELIBERATELY REMOVED (`outils_retires`): its refusal is WRITTEN and names the
        # gesture that works — which neither the derived siblings below (~60 `oto_*` for
        # `oto_kb`) nor "unknown" can say. Consulted BEFORE them: a removal is not
        # a consolidation, there is no neighbor carrying the verbs.
        retire = outils_retires.retrait(name)
        if retire is not None:
            return ErrorInfo("unknown_tool", False, retire.message, retire.hint)
        # A REMOVED name is not an absent connector — and confusing them sends people looking
        # for a mounting problem that does not exist. Experienced on 14/08: `gmail_search`,
        # removed by the google consolidation (33→13 tools), answered "the google
        # connector is not installed in your toolbox" while google WAS installed and
        # the verbs of the vanished name lived in `gmail_message`. The session
        # looked for a nonexistent half-mount.
        voisins = _surviving_siblings(name)
        if voisins is not None:
            return ErrorInfo(
                "unknown_tool", False,
                f"The tool `{name}` no longer exists (name removed or never existed). "
                f"The tools of this domain today: {', '.join(voisins)}.",
                "its verbs probably live under one of them, as the `op` parameter — "
                f"read its schema with oto_tool_schema(name='{voisins[0]}')")
        con = _connector_of_tool(name)
        if con:
            return ErrorInfo(
                "tool_not_mounted", False,
                f"The tool `{name}` is not mounted in your session — this is not "
                f"an outage of the `{con}` connector: it may not be installed in "
                f"your toolbox, the tool may be hidden there or missing from the list frozen at "
                f"session start, or the name no longer exists.",
                f"call it immediately via oto_call(name='{name}', args={{…}}); "
                f"or install the connector — oto_connector(op='select', name='{con}') "
                f"— and open a new conversation to see it listed",
                connector=con)
        return ErrorInfo("unknown_tool", False, f"Unknown tool `{name}`.",
                         "check the exact name with oto_list_my_tools")

    # (3) Upstream HTTP status.
    sc = upstream_status_in_chain(exc)
    if sc is not None:
        raw = scrub(_first_upstream_message(exc))
        if sc in (408, 504):
            return ErrorInfo("upstream_timeout", True,
                             "Timed out on the upstream service.",
                             "retry in a moment")
        if sc == 429:
            # The connector can contradict the status (Hunter: 429 = plan credits
            # exhausted, not too fast a rate) → its verdict wins, and its message
            # says what to do instead.
            declared = _upstream_retryable(exc)
            retryable = True if declared is None else declared
            return ErrorInfo("rate_limited" if retryable else "quota_exhausted",
                             retryable,
                             raw or "Too many requests on the upstream service.",
                             "retry after a short pause" if retryable
                             else "no point retrying: change source or upgrade "
                                  "the connector plan")
        if sc == 404:
            return ErrorInfo("not_found", False,
                             raw or "Resource not found on the upstream service.")
        refus = _refus_nomme_d_un_403(exc) if sc == 403 else None
        if refus is not None:
            return ErrorInfo(refus.code, False, str(refus))
        if sc in (401, 403):
            return ErrorInfo("not_authorized", False,
                             raw or "Access denied by the upstream service.",
                             "check that the connector is connected and authorized")
        if 400 <= sc < 500:
            return ErrorInfo("upstream_4xx", False,
                             raw or f"Request refused by the upstream service ({sc}).")
        if 500 <= sc < 600:
            # Its message, when it says something (scrubbed, bounded, never an HTML page):
            # « the upstream service hit an error (500) » alone could not tell an outage
            # from a disconnected account (WhatsApp, 05/10/2026, signals 1354 and 1359).
            detail = _detail_5xx(raw)
            return ErrorInfo("upstream_5xx", True,
                             f"The upstream service hit an error ({sc})"
                             + (f": {detail}" if detail else "."),
                             "retry later")

    # (4) Timeout not carried by a status.
    if _looks_like_timeout(exc):
        return ErrorInfo("upstream_timeout", True,
                         "Timed out.", "retry in a moment")

    # (4b) HANDLED upstream connector error without an HTTP status (UnipileError for input/config,
    # #90): its message is agent-useful ("Facet not found…", "account not
    # connected") → we echo it AS IS rather than an opaque "Internal error".
    #
    # NO `scrub` here (removed on 2026-07-28, signal #282): these messages are written
    # BY US in oto-core, not relayed from upstream — exactly the case that `scrub`'s
    # docstring excludes ("never to the McpErrors we curated ourselves").
    # Scrubbing them destroyed their only value: `identity_mismatch` compares the REQUESTED
    # id and the RECEIVED id, both public LinkedIn identifiers of >20 characters
    # → `_LONG_ID` rendered "requested profile '[id]', received '[id]'", i.e. a message
    # that says there is a difference without ever saying which. TRULY upstream messages
    # keep their scrub: they go through path (3), above.
    if _is_upstream_managed_error(exc):
        for e in _chain(exc):
            if type(e).__name__ == "UnipileError":
                return ErrorInfo("invalid_input", False,
                                 str(e) or "Request refused by the upstream service.")

    # (5) The rest = bug/internal error: NO echo of str(exc) (anti-leak).
    return ErrorInfo("internal", False, "Internal server error.")


def jsonrpc_code(info: ErrorInfo) -> int:
    """JSON-RPC code of the returned `McpError`: INVALID_PARAMS for an input refusal
    (arguments, unmounted/unknown tool — the agent must change its CALL, not
    retry), INTERNAL_ERROR otherwise (the fine discriminant lives in `data.oto.code`)."""
    return (INVALID_PARAMS
            if info.code in ("invalid_input", "tool_not_mounted", "unknown_tool")
            else INTERNAL_ERROR)
