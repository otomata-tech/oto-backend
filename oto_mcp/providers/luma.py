"""Registry declaration of the `luma` connector.

Single home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# luma: event management on Luma (lu.ma) — events, guests, tickets and
# coupons, blasts, the calendar's contacts and tags, memberships, webhooks,
# and the organization routes.
#
# ONE key in the `x-luma-api-key` header. A key belongs either to a CALENDAR
# (it acts on that calendar) or to an ORGANIZATION (it covers all its
# calendars, and calendar-scoped calls then name the calendar — the tools'
# `calendar_id`, sent as `x-luma-calendar-id`). Luma requires an active Luma
# Plus subscription on the calendar for any API access.
#
# Strict BYOK (`byo_user` + `byo_org`, no platform mode): these are the
# organization's events and attendee lists. Several gestures reach people
# outside the organization (invites, blasts, cancellation); see
# `tools/luma.py`.
CONNECTOR = _c(
    "luma", ["luma"], auth_modes={"byo_user", "byo_org"},
    keyed=True, secret_kind="api_key",
    modules=("luma", "luma_calendar"),
    label="Luma",
    help="events (lu.ma): events, guests and check-ins, tickets and coupons, "
         "invites and blasts, calendar contacts, memberships, webhooks",
    href="https://luma.com",
)

CATEGORY = "Marketing"
PUBLISHER = "Luma"
LOGO_DOMAIN = "luma.com"

DESCRIPTION = (
    "Events run on Luma (lu.ma): create and update events, manage the guest "
    "list (registrations, approvals, waitlist, tickets), invite people and "
    "email guests, coupons and ticket types, and the calendar's audience — "
    "contacts, tags, memberships. API key of a Luma Plus calendar or "
    "organization."
)
