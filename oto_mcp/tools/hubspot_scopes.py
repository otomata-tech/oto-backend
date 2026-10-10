"""HubSpot — which scope each API family needs, how a refusal names it, and the
connection probe that measures it.

No tool here: this module is what the HubSpot tools (`hubspot`, `hubspot_lignes`,
`hubspot_pipelines`) share to turn an upstream refusal into an actionable one, and
what `_verify` uses to say, per family, what the token may read.

**Why scopes are measured at all.** HubSpot grants its scopes OBJECT BY OBJECT: a
private app can read contacts and not tickets. The connection probe only proved the
token authenticates (`coverage: "auth"`), so a portal missing the tickets scope
showed healthy, and the gap only appeared when an agent's call failed mid-task (a
customer portal, 2026-10-08). The probe now also reads the scopes — as a
MEASUREMENT next to the verdict, never as the verdict: a token missing one family
still works for the others (oto#69, third rule), and `ok` stays true.

**Why refusals are rewritten.** HubSpot's 403 says "The scope needed for this API
call isn't available for public use" — read as "you cannot have it", when it is a
checkbox on the private app. A 404 on a ticket came back as an HTML page. Each
refusal now says what happened and what to do, in one line, with the facts in
`data` for a program to route on.

`SCOPES` is the reference: it feeds the doc's checklist, the probe and the refusals.
Its names come from HubSpot's own scope list (links in `connectors/docs/hubspot.md`).
"""
from __future__ import annotations

import time
from typing import Optional

from mcp.types import ErrorData, INVALID_PARAMS

from ..mcp_errors import McpError

#: Per API family: (read scopes, write scopes), from HubSpot's endpoint reference
#: (developers.hubspot.com, "This API requires one of the following scopes"). A
#: family is READABLE if the token holds ANY of its read scopes; the FIRST one is
#: the one we tell people to tick.
#:
#: ⚠️ Tickets are split in two, and that is the trap behind the 403s. The RECORD
#: endpoints (`/crm/v3/objects/tickets`) list the granular `crm.objects.tickets.*`
#: only; the PIPELINES and PROPERTIES endpoints still list the legacy `tickets` scope
#: and not the granular one. HubSpot is migrating `tickets` to the granular names
#: (from 2026-09-08, with a switch that keeps reporting the legacy name), so a token
#: may carry either spelling — both are accepted when reading the token's scopes.
#: `crm.objects.tickets` (no `.read`/`.write`) is not a scope: our doc used to say it.
SCOPES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "contacts": (("crm.objects.contacts.read",), ("crm.objects.contacts.write",)),
    "companies": (("crm.objects.companies.read",), ("crm.objects.companies.write",)),
    "deals": (("crm.objects.deals.read",), ("crm.objects.deals.write",)),
    "tickets": (("crm.objects.tickets.read", "tickets"),
                ("crm.objects.tickets.write", "tickets")),
    "lists": (("crm.lists.read",), ("crm.lists.write",)),
    # Properties of contacts (the probe's target); per object type, `_PROPERTY_SCOPES`.
    "properties": (("crm.schemas.contacts.read", "crm.objects.contacts.read"),
                   ("crm.schemas.contacts.write",)),
    "owners": (("crm.objects.owners.read",), ()),
    "pipelines_deals": (("crm.objects.deals.read", "crm.schemas.deals.read"), ()),
    "pipelines_tickets": (("tickets", "crm.schemas.tickets.read"), ()),
}

#: Reading the PROPERTIES of an object type: its schema scope, or its object scope
#: (HubSpot accepts either) — tickets: the legacy `tickets` only.
_PROPERTY_SCOPES = {
    "contacts": ("crm.schemas.contacts.read", "crm.objects.contacts.read"),
    "companies": ("crm.schemas.companies.read", "crm.objects.companies.read"),
    "deals": ("crm.schemas.deals.read", "crm.objects.deals.read"),
    "tickets": ("tickets", "crm.schemas.tickets.read"),
}

#: The minimal read that proves a family, when the token's scopes cannot be read
#: directly. GETs with `limit=1`, and one POST that searches (it writes nothing).
_PROBES: dict[str, tuple[str, str, dict]] = {
    "contacts": ("GET", "/crm/v3/objects/contacts", {"params": {"limit": 1}}),
    "companies": ("GET", "/crm/v3/objects/companies", {"params": {"limit": 1}}),
    "deals": ("GET", "/crm/v3/objects/deals", {"params": {"limit": 1}}),
    "tickets": ("GET", "/crm/v3/objects/tickets", {"params": {"limit": 1}}),
    "lists": ("POST", "/crm/v3/lists/search", {"json": {"count": 1}}),
    "properties": ("GET", "/crm/v3/properties/contacts/email", {}),
    "owners": ("GET", "/crm/v3/owners", {"params": {"limit": 1}}),
    "pipelines_deals": ("GET", "/crm/v3/pipelines/deals", {}),
    "pipelines_tickets": ("GET", "/crm/v3/pipelines/tickets", {}),
}

#: `object_type` as the tools receive it → the family whose scope it needs.
_FAMILY_OF = {"contacts": "contacts", "companies": "companies", "deals": "deals",
              "tickets": "tickets"}

#: Time limits of the probe, at ITS level (`docs/event-loop-perf.md`: a bound set
#: deeper down bounds nothing if the layers above wait without one). One request
#: never waits more than `_PER_CALL_S`; the whole scope pass stops at `_TOTAL_S` and
#: says `unknown` for what it did not reach — well inside the 45 s of `executer`.
_PER_CALL_S = 8.0
_TOTAL_S = 20.0

GRANTED, MISSING, UNKNOWN = "granted", "missing", "unknown"


# --- reading a refusal -------------------------------------------------------------

def _status(e) -> Optional[int]:
    return getattr(e, "status_code", None)


def is_missing_scopes(e) -> bool:
    body = getattr(e, "body", None)
    return _status(e) == 403 and isinstance(body, dict) and \
        body.get("category") == "MISSING_SCOPES"


def scopes_in_body(body) -> list[str]:
    """The scope names HubSpot put in its refusal, if any — it lists them under
    `errors[].context.requiredGranularScopes` (or `requiredScopes`) on recent
    APIs (`missingScopes` in its error schema), and says nothing on others. Its
    list means "any ONE of these". Never guessed: empty when absent."""
    found: list[str] = []
    if not isinstance(body, dict):
        return found
    contexts = [body.get("context")] + [
        (err or {}).get("context") for err in body.get("errors") or []
        if isinstance(err, dict)]
    for ctx in contexts:
        if not isinstance(ctx, dict):
            continue
        for key in ("requiredGranularScopes", "missingScopes", "requiredScopes"):
            vals = ctx.get(key) or []
            for v in [vals] if isinstance(vals, str) else vals:
                if isinstance(v, str) and v not in found:
                    found.append(v)
    return found


def family_of(object_type=None, family=None) -> Optional[str]:
    if family:
        return family
    return _FAMILY_OF.get(str(object_type or "").strip().lower())


def needed_scopes(e, family: Optional[str],
                  object_type=None) -> tuple[list[str], str]:
    """(scope names, source): from HubSpot's body when it names them, else from
    the reference table — and the answer says which, so nobody mistakes our
    reference for HubSpot's word."""
    named = scopes_in_body(getattr(e, "body", None))
    if named:
        return named, "hubspot"
    otype = str(object_type or "").strip().lower()
    if family == "properties" and otype in _PROPERTY_SCOPES:
        return list(_PROPERTY_SCOPES[otype]), "reference"
    if family in SCOPES:
        return list(SCOPES[family][0]), "reference"
    return [], "unknown"


def recommended(names: list[str], family: Optional[str], object_type=None) -> Optional[str]:
    """The ONE scope to tell a person to tick. HubSpot's list means "any one of"
    and mixes in `…sensitive…` variants nobody should pick for this: we name our
    reference scope when HubSpot accepts it, else its first plain one."""
    otype = str(object_type or "").strip().lower()
    ours = (_PROPERTY_SCOPES.get(otype) if family == "properties" else None) or \
        (SCOPES[family][0] if family in SCOPES else ())
    for s in ours:
        if s in names:
            return s
    plain = [n for n in names if "sensitive" not in n]
    return (plain or names or [None])[0]


def scope_hint(e, family: Optional[str]) -> str:
    names, _ = needed_scopes(e, family)
    pick = recommended(names, family)
    return f"add `{pick}`" if pick else "add its read scope"


_REMEDY = ("on the HubSpot side: Settings > Integrations > Private Apps > the app "
           "that carries this token > Scopes, tick it and save. The token does not "
           "change — nothing to update in oto, and nothing to fix in the call.")


def _refusal(message: str, **data) -> McpError:
    """A curated refusal; its facts ride on `oto_detail`, which the error envelope
    relays as `data.oto.detail` (it rewrites everything else)."""
    err = McpError(ErrorData(code=INVALID_PARAMS, message=message))
    err.oto_detail = {k: v for k, v in data.items() if v is not None}
    return err


def _upstream_refusal(e, message: str, *, retryable=None, **data):
    """The same upstream refusal, KEEPING its HTTP status, with a clean message.

    Not an `McpError`: the error envelope classifies by the upstream status
    (`error_taxonomy`) — 404 `not_found`, 429 `rate_limited` and retryable, 401
    `not_authorized` AND the key marked red on its card. A curated `INVALID_PARAMS`
    would have told the agent "fix your call" on a rate limit."""
    from oto.tools.common.errors import UpstreamHTTPError

    class HubSpotRefusal(UpstreamHTTPError):
        def __str__(self) -> str:
            return message

    err = HubSpotRefusal(_status(e), message, service="hubspot")
    if retryable is not None:
        err.retryable = retryable
    err.oto_detail = {k: v for k, v in data.items() if v is not None}
    return err


def missing_scopes_refusal(e, *, object_type=None, family=None) -> Optional[McpError]:
    """The MISSING_SCOPES 403 as an actionable refusal, or None for anything else."""
    if not is_missing_scopes(e):
        return None
    fam = family_of(object_type, family)
    names, source = needed_scopes(e, fam, object_type)
    pick = recommended(names, fam, object_type)
    which = f"`{pick}`" if pick else "the read scope of this object"
    target = f"object_type={object_type!r}" if object_type else f"family={fam!r}"
    return _refusal(
        f"HubSpot refuses this call: the private app token lacks a scope "
        f"(403 MISSING_SCOPES, {target}). Scope to add: {which}. {_REMEDY} "
        "(HubSpot's own wording, \"isn't available for public use\", is misleading: "
        "the scope exists and can be ticked.)",
        reason="missing_scopes", status=403, family=fam, scope_to_add=pick,
        accepted_scopes=names, scopes_source=source, remedy=_REMEDY)


def _is_html(body) -> bool:
    return isinstance(body, str) and body.lstrip()[:1] == "<"


def translate(e, *, object_type=None, object_id=None, family=None,
              what: str = "object"):
    """Any HubSpot refusal worth rewriting, as an exception to raise in its place;
    None to let the original travel with its shape and its trace.

    403 MISSING_SCOPES → curated `McpError` naming the scope. 401 / 404 / 429 / an
    HTML body → the same upstream status with a clean message (`_upstream_refusal`).
    Only refusals are rewritten: an empty result is a normal `results: []` and never
    reaches this function."""
    code, body = _status(e), getattr(e, "body", None)
    refus = missing_scopes_refusal(e, object_type=object_type, family=family)
    if refus is not None:
        return refus
    if code == 401:
        return _upstream_refusal(
            e, "HubSpot rejects the token (401). The expected token is the private "
            "app's ACCESS token (`pat-<region>-…`). A long base64 value starting with "
            "`Ci…` is an OAuth refresh token: HubSpot reads it as \"expired\" since "
            "1970 — that means \"not an access token\", not \"too old\". Paste the "
            "private app's access token in oto (connector hubspot).",
            reason="unauthorized", status=401)
    if code == 404:
        ident = f" {object_id!r}" if object_id else ""
        scope = f" in {object_type}" if object_type else ""
        return _upstream_refusal(
            e, f"HubSpot: {what}{ident} not found{scope} (404) — deleted, archived, "
            "or never existed under this id. Check the id (a search finds it by its "
            "properties).", reason="not_found", status=404,
            object_type=object_type, object_id=object_id)
    if code == 429:
        policy = body.get("policyName") if isinstance(body, dict) else None
        daily = str(policy or "").upper() in ("DAILY", "MONTHLY")
        hint = ("the DAILY quota is spent: retrying today will not help"
                if daily else "wait about 10 seconds, then retry (fewer calls: "
                "batch reads, op='members' with properties)")
        return _upstream_refusal(
            e, f"HubSpot rate limit (429{', ' + policy if policy else ''}) — already "
            f"retried by the client. {hint}.", retryable=not daily,
            reason="rate_limited", status=429, policy=policy,
            retry_after_s=None if daily else 10)
    if _is_html(body):
        return _upstream_refusal(
            e, f"HubSpot HTTP {code} on this {what} (non-JSON page, {len(body)} "
            "chars, not shown).", reason=f"http_{code}", status=code)
    return None


# --- the probe ---------------------------------------------------------------------

def _granted_from_list(granted: set[str]) -> dict[str, str]:
    return {fam: GRANTED if granted & set(read) else MISSING
            for fam, (read, _) in SCOPES.items()}


def _token_info(client, token: str, deadline: float) -> Optional[set[str]]:
    """The scopes granted to a private app token, read directly — or None when
    HubSpot does not answer them (then the per-family reads decide)."""
    from oto.tools.common import raise_for_upstream
    try:
        resp = client.session.request(
            "POST", f"{client.BASE_URL}/oauth/v2/private-apps/get/access-token-info",
            json={"tokenKey": token}, timeout=min(_PER_CALL_S, max(deadline - time.monotonic(), 1)))
        raise_for_upstream(resp, service="hubspot")
        scopes = (resp.json() or {}).get("scopes")
    # noqa: SILENT — an unreadable introspection falls back to the per-family reads
    except Exception:  # noqa: BLE001
        return None
    return set(scopes) if isinstance(scopes, list) and scopes else None


def _probe_family(client, fam: str, deadline: float) -> str:
    from oto.tools.common import raise_for_upstream
    method, path, kw = _PROBES[fam]
    left = deadline - time.monotonic()
    if left <= 0:
        return UNKNOWN
    try:
        resp = client.session.request(method, f"{client.BASE_URL}{path}",
                                      timeout=min(_PER_CALL_S, left), **kw)
        raise_for_upstream(resp, service="hubspot")
        return GRANTED
    # noqa: SILENT — the failure IS the measurement: `missing` or `unknown`, never "granted"
    except Exception as e:  # noqa: BLE001
        # Only HubSpot's own MISSING_SCOPES reads as `missing`: another 403 (an
        # account-level block, a portal feature) is not a checkbox to tick.
        return MISSING if is_missing_scopes(e) else UNKNOWN


def summary(families: dict[str, str]) -> str:
    missing = [f for f, v in families.items() if v == MISSING]
    unknown = [f for f, v in families.items() if v == UNKNOWN]
    parts = ["connected"]
    if missing:
        parts.append(", ".join(missing) + (" scope missing" if len(missing) == 1
                                           else " scopes missing"))
    if unknown:
        parts.append(", ".join(unknown) + " not checked")
    return ", ".join(parts)


def measure_scopes(client, token: str) -> dict:
    """Per family: granted | missing | unknown, and the scopes to add.

    Side-effect free and bounded: one introspection call when HubSpot answers it,
    otherwise one minimal read per family (`_PROBES`), each under `_PER_CALL_S`,
    all under `_TOTAL_S`. Never raises: the authentication verdict was already
    given by the caller, and a scope pass that fails says `unknown`."""
    deadline = time.monotonic() + _TOTAL_S
    granted = _token_info(client, token, deadline)
    if granted is not None:
        families, method = _granted_from_list(granted), "token_info"
    else:
        families = {fam: _probe_family(client, fam, deadline) for fam in _PROBES}
        method = "probe"
    out = {"method": method, "families": families,
           "missing_scopes": {f: list(SCOPES[f][0]) for f, v in families.items()
                              if v == MISSING},
           "summary": summary(families)}
    if granted is not None:
        out["granted_scopes"] = sorted(granted)
    return out
