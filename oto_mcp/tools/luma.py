"""Luma tools — events, guests and blasts (lu.ma).

Wraps `oto.tools.luma.client.LumaClient`. Five tools here, five more in
`luma_calendar.py`; the shared base (key, calendar of an organization key,
refusal translation) is `luma_socle.py`.

- READ and WRITE are separate tools for events and guests: `luma_events` and
  `luma_guests` only read (declared `LECTURE`, so a recipe can pull a guest
  list), `luma_event_admin` and `luma_guest_admin` change things.
- ⚠️ Three gestures reach people outside the organization and are **dry-run
  by default**: `luma_guest_admin op="invite"`, `luma_blasts op="send"`,
  `luma_event_admin op="cancel"`.
- **Cursor pagination**: lists return `{entries, has_more, next_cursor}`;
  pass `next_cursor` back as `cursor` while `has_more` is true.

Client calls are written out in plain form (`c.list_guests(…)`): that is what
the version-skew probe (`test_tools_client_methods_exist`) reads.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastmcp import FastMCP

from ..connectors import verify as connector_verify
from .lecture import LECTURE
from .luma_socle import (EVENT_ROW_OMITTED, GUEST_ROW_OMITTED, _bad, _client,
                         _slim, _verify, bad_op, dry_run_reply, need, run)


def register(mcp: FastMCP) -> None:
    connector_verify.register("luma", _verify)

    # --- events: read ---------------------------------------------------------

    @mcp.tool(annotations=LECTURE)
    def luma_events(
        op: Literal["list", "get", "lookup", "resolve", "org_list"] = "list",
        event_id: Optional[str] = None,
        url: Optional[str] = None,
        after: Optional[str] = None,
        before: Optional[str] = None,
        status: Optional[Literal["approved", "pending"]] = None,
        include_viewed: bool = False,
        include_external: bool = False,
        sort_direction: Optional[Literal["asc", "desc"]] = None,
        full: bool = False,
        calendar_id: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        """Luma — events of the calendar (read only).

        `op`:
        - `list` (default) — events the calendar manages, start date bounded by
          `after`/`before` (ISO 8601). `include_viewed` adds events listed on
          the calendar but managed elsewhere (location reduced to the city);
          `include_external` adds non-Luma events; `status="pending"` lists
          submissions awaiting approval. Entries omit the description, and
          the default view drops heavy fields (registration questions,
          coordinates, legacy ids) and names them in `omitted`.
        - `get` — one event (`event_id`, `evt-…`) in full: description, hosts,
          guest counts by status, registration questions, ticket info.
        - `lookup` — is this event (`event_id`, or any event by `url`) already
          on the calendar?
        - `resolve` — an event or a calendar from a lu.ma URL or slug (`url`):
          the way to get an `evt-…` id from a link someone pasted.
        - `org_list` — events across ALL calendars of the organization
          (organization key only), deduplicated.

        Args:
            op: the operation, see above.
            event_id: the event (`evt-…`) for get / lookup.
            url: op='resolve' — lu.ma URL or slug; op='lookup' — the event URL.
            after: list / org_list — events starting after (ISO 8601).
            before: list / org_list — events starting before (ISO 8601).
            status: op='list' — approved (default) or pending submissions.
            include_viewed: op='list' — also events managed by other calendars.
            include_external: op='list' — also events hosted outside Luma.
            sort_direction: list / org_list — by start date.
            full: list / org_list — every field of each entry, as Luma sends it.
            calendar_id: with an ORGANIZATION key, the calendar (`cal-…`).
            cursor: next page (`next_cursor` of the previous answer).
            limit: rows per page (the server caps it).
        """
        c = _client(calendar_id)
        if op == "list":
            return _slim(run(lambda: c.list_events(
                after=after, before=before, status=status,
                access=["manage", "view"] if include_viewed else None,
                platforms=["luma", "external"] if include_external else None,
                sort_column="start_at" if sort_direction else None,
                sort_direction=sort_direction, limit=limit, cursor=cursor)),
                EVENT_ROW_OMITTED, full)
        if op == "get":
            need(event_id, "event_id", op)
            return run(lambda: c.get_event(event_id))
        if op == "lookup":
            return run(lambda: c.lookup_event(
                event_id=event_id, url=url,
                platform="luma" if event_id else None))
        if op == "resolve":
            need(url, "url", op)
            return run(lambda: c.lookup_entity(url))
        if op == "org_list":
            return _slim(run(lambda: c.list_organization_events(
                after=after, before=before, sort_direction=sort_direction,
                limit=limit, cursor=cursor)), EVENT_ROW_OMITTED, full)
        raise bad_op(op, "list | get | lookup | resolve | org_list")

    # --- events: write --------------------------------------------------------

    @mcp.tool()
    def luma_event_admin(
        op: Literal["create", "update", "cancel", "submit", "approve", "reject",
                    "add_host", "update_host", "remove_host", "transfer"],
        event_id: Optional[str] = None,
        fields: Optional[Dict[str, Any]] = None,
        email: Optional[str] = None,
        access_level: Optional[Literal["none", "check-in", "manager"]] = None,
        is_visible: Optional[bool] = None,
        calendar_event_id: Optional[str] = None,
        to_calendar_id: Optional[str] = None,
        message: Optional[str] = None,
        notify_submitter: Optional[bool] = None,
        should_refund: Optional[bool] = None,
        dry_run: bool = True,
        calendar_id: Optional[str] = None,
    ) -> Any:
        """Luma — create, change, cancel an event; its hosts; calendar submissions.

        `op`:
        - `create` — `fields`: `name`, `start_at` (ISO 8601, UTC), `timezone`
          (IANA, e.g. Europe/Paris) required; also end_at, description_md,
          geo_address_json (`{type:"google", place_id}` from `luma_calendar
          op="places"`), meeting_url or create_meeting (zoom|google-meet),
          max_capacity, visibility, registration_open, cover_url, slug,
          ticket_types, registration_questions…
        - `update` — `event_id` + the `fields` to change. `suppress_email` /
          `suppress_notifications` in `fields` keep guests from being told.
        - `cancel` — ⚠️ IRREVERSIBLE: every guest is notified, the event is
          DELETED, refunds follow `should_refund` (required when guests paid).
          **Dry-run by default**: it then only says whether guests paid.
        - `submit` — add an existing event to the calendar (`fields`:
          `{platform:"luma", event_id}` or `{platform:"external", url, name,
          start_at, duration_interval (e.g. PT2H), timezone}`).
        - `approve` / `reject` — a pending submission (`calendar_event_id`);
          `reject` emails the submitter only with `notify_submitter` or a
          `message`.
        - `add_host` / `update_host` / `remove_host` — by `email`;
          `access_level`: manager (co-host), check-in (staff), none.
        - `transfer` — move the event to another calendar of the organization
          (`to_calendar_id`; organization key).

        Args:
            op: the operation, see above.
            event_id: the event (`evt-…`).
            fields: op='create'/'update'/'submit' — the body, as Luma names it.
            email: host operations — the host's email.
            access_level: add_host / update_host.
            is_visible: add_host / update_host — shown on the event page.
            calendar_event_id: approve / reject — the submission id.
            to_calendar_id: op='transfer' — the destination calendar.
            message: op='reject' — note emailed to the submitter.
            notify_submitter: op='reject' — email the submitter.
            should_refund: op='cancel' — refund paid guests.
            dry_run: op='cancel' — True (default) describes without cancelling.
            calendar_id: with an ORGANIZATION key, the calendar (`cal-…`).
        """
        c = _client(calendar_id)
        if op == "create":
            need(fields, "fields", op)
            return run(lambda: c.create_event(fields))
        if op == "update":
            need(event_id, "event_id", op)
            need(fields, "fields", op)
            return run(lambda: c.update_event(event_id, fields))
        if op == "cancel":
            need(event_id, "event_id", op)
            ticket = run(lambda: c.request_event_cancellation(event_id))
            is_paid = (ticket or {}).get("is_paid")
            if dry_run:
                return dry_run_reply(
                    "cancel this event: notify every guest and delete it",
                    "irreversible — the event is deleted and guests are told",
                    event_id=event_id, is_paid=is_paid,
                    should_refund=should_refund)
            if is_paid and should_refund is None:
                raise _bad("op='cancel': this event has paid guests — pass "
                           "`should_refund` (true or false) explicitly.")
            return run(lambda: c.cancel_event(
                event_id, ticket.get("cancellation_token"),
                should_refund=should_refund))
        if op == "submit":
            need(fields, "fields", op)
            return run(lambda: c.add_event_to_calendar(fields))
        if op == "approve":
            need(calendar_event_id, "calendar_event_id", op)
            return run(lambda: c.approve_event(calendar_event_id))
        if op == "reject":
            need(calendar_event_id, "calendar_event_id", op)
            return run(lambda: c.reject_event(
                calendar_event_id, message=message,
                notify_submitter=notify_submitter))
        if op in ("add_host", "update_host", "remove_host"):
            need(event_id, "event_id", op)
            need(email, "email", op)
            if op == "add_host":
                return run(lambda: c.add_host(event_id, email,
                                              access_level=access_level,
                                              is_visible=is_visible))
            if op == "update_host":
                return run(lambda: c.update_host(event_id, email,
                                                 access_level=access_level,
                                                 is_visible=is_visible))
            return run(lambda: c.remove_host(event_id, email))
        if op == "transfer":
            need(event_id, "event_id", op)
            need(to_calendar_id, "to_calendar_id", op)
            return run(lambda: c.transfer_event_calendar(event_id, to_calendar_id))
        raise bad_op(op, "create | update | cancel | submit | approve | reject | "
                         "add_host | update_host | remove_host | transfer")

    # --- guests: read ---------------------------------------------------------

    @mcp.tool(annotations=LECTURE)
    def luma_guests(
        event_id: str,
        op: Literal["list", "get"] = "list",
        guest: Optional[str] = None,
        status: Optional[Literal["approved", "session", "pending_approval",
                                 "invited", "declined", "waitlist"]] = None,
        sort_column: Optional[Literal["name", "email", "created_at",
                                      "registered_at", "checked_in_at"]] = None,
        sort_direction: Optional[Literal["asc", "desc"]] = None,
        full: bool = False,
        calendar_id: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        """Luma — the guest list of an event (read only): who registered, who
        is going, waitlisted, invited, checked in.

        `op`:
        - `list` (default) — guests with their status and tickets;
          `status="approved"` = going. Sort by `checked_in_at` to see arrivals.
          The default view drops registration answers, phone number, QR code
          and wallet fields, named in `omitted` (`full=true`, or `op="get"`).
        - `get` — one guest (`guest`: guest id `gst-…`, ticket key, guest key
          `g-…`, or EMAIL) with registration answers and ticket orders
          (amount paid, coupon applied).

        Args:
            event_id: the event (`evt-…`).
            op: the operation, see above.
            guest: op='get' — guest id, ticket key, guest key or email.
            status: op='list' — approval status filter.
            sort_column: op='list'.
            sort_direction: op='list'.
            full: op='list' — every field of each guest, as Luma sends it.
            calendar_id: with an ORGANIZATION key, the calendar (`cal-…`).
            cursor: next page (`next_cursor` of the previous answer).
            limit: rows per page (the server caps it).
        """
        c = _client(calendar_id)
        if op == "list":
            return _slim(run(lambda: c.list_guests(
                event_id, approval_status=status, sort_column=sort_column,
                sort_direction=sort_direction, limit=limit, cursor=cursor)),
                GUEST_ROW_OMITTED, full)
        if op == "get":
            need(guest, "guest", op)
            return run(lambda: c.get_guest(event_id, guest))
        raise bad_op(op, "list | get")

    # --- guests: write --------------------------------------------------------

    @mcp.tool()
    def luma_guest_admin(
        event_id: str,
        op: Literal["add", "set_status", "set_tickets", "invite"],
        guests: Optional[List[Dict[str, Any]]] = None,
        guest: Optional[str] = None,
        status: Optional[Literal["approved", "declined", "pending_approval",
                                 "waitlist"]] = None,
        ticket_type_ids: Optional[List[str]] = None,
        remove_ticket_ids: Optional[List[str]] = None,
        send_email: Optional[bool] = None,
        should_refund: Optional[bool] = None,
        message: Optional[str] = None,
        dry_run: bool = True,
        calendar_id: Optional[str] = None,
    ) -> Any:
        """Luma — register, approve, decline, invite guests of an event.

        `op`:
        - `add` — register people directly (`guests`: `[{email, name?,
          registration_answers?}]`), going by default (`status` for
          pending_approval / waitlist), one ticket of the default type unless
          `ticket_type_ids`. Luma may email them: `send_email=false` to avoid.
        - `set_status` — `guest` → approved (going) | declined |
          pending_approval | waitlist. ⚠️ Luma EMAILS the guest unless
          `send_email=false`; `message` adds a note; `should_refund` refunds a
          paid guest moved out of approved.
        - `set_tickets` — give complimentary tickets (`ticket_type_ids`) or
          invalidate tickets (`remove_ticket_ids`, refunded only with
          `should_refund`).
        - `invite` — ⚠️ emails (and texts) each person in `guests` an
          invitation. **Dry-run by default.**

        Additions and invites run in the background: the answer lists who will
        be SKIPPED (unsubscribed, removed, or who blocked the calendar), not
        the final guest list — read it back with `luma_guests`.

        Args:
            event_id: the event (`evt-…`).
            op: the operation, see above.
            guests: op='add'/'invite' — `[{email, name?}]`.
            guest: set_status / set_tickets — guest id, ticket key, guest key or email.
            status: op='add' (approved | pending_approval | waitlist) or op='set_status'.
            ticket_type_ids: op='add'/'set_tickets' — ticket types to grant.
            remove_ticket_ids: op='set_tickets' — tickets to invalidate.
            send_email: add / set_status / set_tickets — let Luma email the guest.
            should_refund: set_status / set_tickets — refund what was paid.
            message: set_status / invite — personal note in the email.
            dry_run: op='invite' — True (default) describes without sending.
            calendar_id: with an ORGANIZATION key, the calendar (`cal-…`).
        """
        c = _client(calendar_id)
        if op == "add":
            need(guests, "guests", op)
            if status == "declined":
                raise _bad("op='add': a guest is added as approved, "
                           "pending_approval or waitlist — not declined.")
            return run(lambda: c.add_guests(
                event_id, guests, approval_status=status,
                ticket_type_ids=ticket_type_ids, send_email=send_email))
        if op == "set_status":
            need(guest, "guest", op)
            need(status, "status", op)
            return run(lambda: c.update_guest_status(
                event_id, guest, status, should_refund=should_refund,
                send_email=send_email, message=message))
        if op == "set_tickets":
            need(guest, "guest", op)
            return run(lambda: c.update_guest_tickets(
                event_id, guest, add_ticket_type_ids=ticket_type_ids,
                remove_ticket_ids=remove_ticket_ids,
                should_refund=should_refund, send_email=send_email))
        if op == "invite":
            need(guests, "guests", op)
            if dry_run:
                return dry_run_reply(
                    "email (and text) an invitation to each person",
                    "invitations go out at once and cannot be recalled",
                    event_id=event_id,
                    recipients=[g.get("email") for g in guests
                                if isinstance(g, dict)],
                    message=message)
            return run(lambda: c.send_invites(event_id, guests, message=message))
        raise bad_op(op, "add | set_status | set_tickets | invite")

    # --- blasts ---------------------------------------------------------------

    @mcp.tool()
    def luma_blasts(
        op: Literal["list", "get", "send", "update", "delete"] = "list",
        event_id: Optional[str] = None,
        blast_id: Optional[str] = None,
        content_md: Optional[str] = None,
        subject: Optional[str] = None,
        recipient_groups: Optional[List[Dict[str, Any]]] = None,
        scheduled_for: Optional[str] = None,
        dry_run: bool = True,
        calendar_id: Optional[str] = None,
    ) -> Any:
        """Luma — blasts: emails to an event's guests, also posted on the event page.

        `op`:
        - `list` — the event's blasts, newest first, with recipient and open
          counts once sent. `get` — one blast (`blast_id`, `ep-…`).
        - `send` — ⚠️ emails the guests now, or at `scheduled_for` (ISO 8601).
          Cannot be recalled once sent; counts against the calendar's send
          limit (10 blasts per 5 minutes). **Dry-run by default.**
          `recipient_groups`: `[{status, event_ticket_type_id?}]` with status
          approved (going, the default) | checked_in | pending_approval |
          waitlist | invited.
        - `update` — before sending, anything; after, only `subject` and
          `content_md`, which edits the page post, not delivered emails.
        - `delete` — cancels a scheduled blast; a sent one only leaves the page.

        Args:
            op: the operation, see above.
            event_id: list / send — the event (`evt-…`).
            blast_id: get / update / delete.
            content_md: send / update — the message, in markdown.
            subject: send / update — email subject (default "New message in …").
            recipient_groups: send / update — who receives it.
            scheduled_for: send / update — future send time (ISO 8601).
            dry_run: op='send' — True (default) describes without sending.
            calendar_id: with an ORGANIZATION key, the calendar (`cal-…`).
        """
        c = _client(calendar_id)
        if op == "list":
            need(event_id, "event_id", op)
            return run(lambda: c.list_blasts(event_id))
        if op == "get":
            need(blast_id, "blast_id", op)
            return run(lambda: c.get_blast(blast_id))
        if op == "send":
            need(event_id, "event_id", op)
            need(content_md, "content_md", op)
            if dry_run:
                return dry_run_reply(
                    "email this message to the event's guests",
                    "a sent blast cannot be recalled",
                    event_id=event_id, subject=subject,
                    recipient_groups=recipient_groups or [{"status": "approved"}],
                    scheduled_for=scheduled_for, content_md=content_md)
            return run(lambda: c.create_blast(
                event_id, content_md, subject=subject,
                recipient_groups=recipient_groups, scheduled_for=scheduled_for))
        if op == "update":
            need(blast_id, "blast_id", op)
            return run(lambda: c.update_blast(
                blast_id, content_md=content_md, subject=subject,
                recipient_groups=recipient_groups, scheduled_for=scheduled_for))
        if op == "delete":
            need(blast_id, "blast_id", op)
            return run(lambda: c.delete_blast(blast_id))
        raise bad_op(op, "list | get | send | update | delete")
