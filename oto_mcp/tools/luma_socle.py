"""Shared base of the `luma` connector modules.

The connector spans two modules (`Connector.modules` in the registry):
`tools/luma.py` (events, guests, blasts) and `tools/luma_calendar.py` (ticket
types and coupons, contacts, the calendar itself, memberships, webhooks). This
file holds what they have in common — key resolution, the calendar an
organization key targets, translation of a Luma refusal, argument checks — so
that a fix never covers only half the connector. It has no `register()`.

**Calendar key or organization key.** A calendar key acts on its calendar; an
organization key covers all the organization's calendars, and every
calendar-scoped call then needs `calendar_id` (sent as `x-luma-calendar-id`).
Every tool takes it, and ignores it when absent.

**What reaches people outside the organization, or hands them access or
money, is dry-run by default**: inviting people (`luma_guest_admin
op="invite"`), emailing guests (`luma_blasts op="send"`), cancelling an event
(`luma_event_admin op="cancel"`), making someone a calendar admin
(`luma_calendar op="add_admins"`), sending the calendar's notifications to a
URL (`luma_webhooks op="create"`) and changing a member's status, which
captures or cancels a payment (`luma_memberships op="set_status"`). Other
writes that may email a guest (`add`, `set_status`) say so in their docstring
and expose `send_email`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Optional

from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..mcp_errors import McpError

if TYPE_CHECKING:  # the annotation of `_client()` only — never evaluated
    from oto.tools.luma.client import LumaClient

#: Where the user creates their key, in THEIR Luma account.
WHERE_TO_CREATE = ("Luma → the calendar (or the organization) → Settings → "
                   "Developer → API keys")


def _client(calendar_id: Optional[str] = None) -> LumaClient:
    """The Luma client for THIS caller's key. Real import in the body: tests
    replace the client, and the version probe reads the return annotation."""
    from oto.tools.luma.client import LumaClient

    key, _is_platform = access.resolve_api_key("luma")
    return LumaClient(api_key=key, calendar_id=calendar_id)


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """The "test connection" probe: `GET /v1/users/get-self`, no side effect.
    Any valid key can call it; a refused key answers 401."""
    from oto.tools.luma.client import LumaClient

    LumaClient(api_key=fields["key"]).get_self()


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def need(value: Any, name: str, op: str) -> Any:
    if value is None or value == "" or value == [] or value == {}:
        raise _bad(f"op={op!r}: `{name}` is required.")
    return value


def bad_op(op: str, expected: str) -> McpError:
    return _bad(f"`op` invalid: {op!r} (expected: {expected}).")


def upstream_message(e: Any) -> str:
    """A Luma refusal, translated into what the caller can do about it."""
    status = e.status_code
    body = e.body if isinstance(e.body, dict) else {}
    detail = body.get("message") or body.get("error") or e.body
    if status == 401:
        return ("Luma rejected the key (401) — check the key configured on this "
                f"connector ({WHERE_TO_CREATE}).")
    if status == 403:
        return ("Luma denied access (403) — the key is valid but cannot do this: "
                "the calendar may lack an active Luma Plus subscription, the "
                "event may be managed by another calendar, an organization "
                "route needs an ORGANIZATION key, or an organization key needs "
                f"`calendar_id` for a calendar route. Luma said: {detail}")
    if status == 404:
        return f"Luma: not found (404) — check the identifier. Luma said: {detail}"
    if status in (400, 422):
        return f"Luma rejected the request (HTTP {status}): {detail}"
    if status == 429:
        return ("Luma: rate limit reached (429) — the key is blocked for about a "
                "minute (200 requests/minute per calendar, 500 per "
                "organization). Try again shortly.")
    if status >= 500:
        return f"Luma is temporarily unavailable (HTTP {status}) — try again later."
    return f"Luma rejected the request (HTTP {status}): {detail}"


def run(fn: Callable[[], Any]) -> Any:
    """Call the client; a local refusal or an upstream one becomes a named
    INVALID_PARAMS error."""
    from oto.tools.common.errors import UpstreamHTTPError

    try:
        return fn()
    except ValueError as e:
        raise _bad(str(e))
    except UpstreamHTTPError as e:
        raise _bad(upstream_message(e))


#: What a LIST leaves out by default, per family — named keys, dropped at any
#: depth of each entry (a Luma entry may nest the event under `event`).
#: `full=True` returns the entries as Luma sent them.
EVENT_ROW_OMITTED = (
    "registration_questions", "feedback_email", "coordinate", "geo_latitude",
    "geo_longitude", "api_id", "user_api_id", "calendar_api_id",
    "zoom_meeting_url", "managing_calendars", "cover_url", "description",
    "description_md")
GUEST_ROW_OMITTED = (
    "check_in_qr_code", "eth_address", "solana_address", "attribution_params",
    "registration_answers", "phone_number")
CONTACT_ROW_OMITTED = ("avatar_url",)
TIER_ROW_OMITTED = ("registration_questions", "access_info")
#: A webhook's signing secret NEVER comes back — not in a list, not from
#: `get`, `create` or `update` (all three return the webhook object, secret
#: included): an agent has no use for it, and whatever it reads can end up in a
#: transcript. Whoever verifies the signatures reads it in Luma's dashboard.
WEBHOOK_SECRET = "secret"
WEBHOOK_SECRET_HOW = ("the signing secret is never returned — read it in Luma's "
                      "dashboard (calendar → Settings → Developer → Webhooks)")


def _strip(node: Any, drop: set, removed: set) -> Any:
    """`node` without the `drop` keys, at any depth; the removed ones go to
    `removed`. Non-destructive: the upstream payload is left as is."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in drop:
                removed.add(k)
            else:
                out[k] = _strip(v, drop, removed)
        return out
    if isinstance(node, list):
        return [_strip(v, drop, removed) for v in node]
    return node


def without_secret(payload: Any) -> Any:
    """A webhook object (or list of them) without its signing secret, and an
    `omitted` block that says so when one was there."""
    removed: set = set()
    out = _strip(payload, {WEBHOOK_SECRET}, removed)
    if removed and isinstance(out, dict):
        out = dict(out, omitted={"keys": sorted(removed), "how": WEBHOOK_SECRET_HOW})
    return out


def _slim(payload: Any, omitted: tuple, full: bool) -> Any:
    """The tightened view of a Luma list: each entry of `entries` without the
    `omitted` keys (at any depth), the envelope (`has_more`, `next_cursor`)
    intact, and an `omitted` block that NAMES what was removed — an agent that
    does not see a key must know it exists. `full=True` returns the raw
    payload."""
    if full or not isinstance(payload, dict) or not isinstance(
            payload.get("entries"), list):
        return payload
    drop, removed = set(omitted), set()
    out = dict(payload, entries=[_strip(row, drop, removed)
                                 for row in payload["entries"]])
    if removed:
        out["omitted"] = {"keys": sorted(removed),
                          "how": "full=true returns every field"}
    return out


def dry_run_reply(would: str, warning: str, **what: Any) -> dict:
    """What a dry-run returns: the action described, nothing sent."""
    return {
        "dry_run": True,
        "would": would,
        **{k: v for k, v in what.items() if v is not None},
        "warning": warning,
        "to_execute": "call again with dry_run=false",
    }
