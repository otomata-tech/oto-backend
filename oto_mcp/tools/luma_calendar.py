"""Luma tools — ticket types and coupons, contacts, the calendar itself,
memberships and webhooks (lu.ma).

The second module of the `luma` connector (`tools/luma.py` holds events,
guests and blasts; `luma_socle.py` the shared base). Each tool takes
`calendar_id`, needed only with an ORGANIZATION key.

Client calls are written out in plain form (`c.list_contacts(…)`): that is
what the version-skew probe (`test_tools_client_methods_exist`) reads.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastmcp import FastMCP

from .luma_socle import (CONTACT_ROW_OMITTED, TIER_ROW_OMITTED,
                         WEBHOOK_ROW_OMITTED, _client, _slim, bad_op, need,
                         run)

_COLORS = Literal["cranberry", "barney", "red", "green", "blue", "purple",
                  "yellow", "orange"]


def register(mcp: FastMCP) -> None:

    # --- ticket types and coupons ---------------------------------------------

    @mcp.tool()
    def luma_tickets(
        op: Literal["types", "type", "create_type", "update_type", "delete_type",
                    "coupons", "create_coupon", "update_coupon"] = "types",
        event_id: Optional[str] = None,
        ticket_type_id: Optional[str] = None,
        fields: Optional[Dict[str, Any]] = None,
        include_hidden: Optional[bool] = None,
        code: Optional[str] = None,
        discount: Optional[Dict[str, Any]] = None,
        remaining_count: Optional[int] = None,
        valid_start_at: Optional[str] = None,
        valid_end_at: Optional[str] = None,
        calendar_id: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        """Luma — ticket types of an event, and coupons (event or calendar-wide).

        `op`:
        - `types` / `type` — the event's ticket types (`include_hidden`), or one.
        - `create_type` — `fields`: `name` (≤ 30) and `type` free|paid required;
          a paid type needs `cents` (minor unit: 2500 = 25.00) + `currency`
          and a Stripe account on the calendar. Also require_approval,
          is_hidden, max_capacity, valid_start_at, valid_end_at,
          is_flexible + min_cents (pay what you want).
        - `update_type` — the `fields` to change. `delete_type` — soft delete:
          holders keep their tickets; the last visible type cannot go.
        - `coupons` — with `event_id`, the event's; without, the calendar's.
        - `create_coupon` — `code` (≤ 20, case-insensitive) + `discount`:
          `{discount_type:"percent", percent_off}` or `{discount_type:"amount",
          cents_off, currency}`. With `event_id` it is the event's (and
          `ticket_type_id` restricts it — on a hidden type it is an unlock
          code); without, valid on every event of the calendar.
          `remaining_count` 1000000 = unlimited.
        - `update_coupon` — only count and validity window change; the
          discount itself can never be edited.

        Args:
            op: the operation, see above.
            event_id: the event (`evt-…`); omitted on coupons = calendar-wide.
            ticket_type_id: type / update_type / delete_type, or create_coupon restriction.
            fields: create_type / update_type — the body, as Luma names it.
            include_hidden: op='types' — include hidden types.
            code: create_coupon / update_coupon.
            discount: op='create_coupon'.
            remaining_count: coupon uses left.
            valid_start_at: coupon validity start (ISO 8601).
            valid_end_at: coupon validity end (ISO 8601).
            calendar_id: with an ORGANIZATION key, the calendar (`cal-…`).
            cursor: next page (`next_cursor` of the previous answer).
            limit: rows per page (the server caps it).
        """
        c = _client(calendar_id)
        if op == "types":
            need(event_id, "event_id", op)
            return run(lambda: c.list_ticket_types(event_id,
                                                   include_hidden=include_hidden))
        if op == "type":
            need(ticket_type_id, "ticket_type_id", op)
            return run(lambda: c.get_ticket_type(ticket_type_id))
        if op == "create_type":
            need(event_id, "event_id", op)
            need(fields, "fields", op)
            return run(lambda: c.create_ticket_type(event_id, fields))
        if op == "update_type":
            need(ticket_type_id, "ticket_type_id", op)
            need(fields, "fields", op)
            return run(lambda: c.update_ticket_type(ticket_type_id, fields))
        if op == "delete_type":
            need(ticket_type_id, "ticket_type_id", op)
            return run(lambda: c.delete_ticket_type(ticket_type_id))
        if op == "coupons":
            if event_id:
                return run(lambda: c.list_event_coupons(event_id, limit=limit,
                                                        cursor=cursor))
            return run(lambda: c.list_calendar_coupons(limit=limit, cursor=cursor))
        if op == "create_coupon":
            need(code, "code", op)
            need(discount, "discount", op)
            return run(lambda: c.create_coupon(
                code, discount, event_id=event_id, ticket_type_id=ticket_type_id,
                remaining_count=remaining_count, valid_start_at=valid_start_at,
                valid_end_at=valid_end_at))
        if op == "update_coupon":
            need(code, "code", op)
            return run(lambda: c.update_coupon(
                code, event_id=event_id, remaining_count=remaining_count,
                valid_start_at=valid_start_at, valid_end_at=valid_end_at))
        raise bad_op(op, "types | type | create_type | update_type | delete_type "
                         "| coupons | create_coupon | update_coupon")

    # --- contacts -------------------------------------------------------------

    @mcp.tool()
    def luma_contacts(
        op: Literal["list", "import", "block", "remove", "restore", "tags",
                    "create_tag", "update_tag", "delete_tag", "tag",
                    "untag"] = "list",
        query: Optional[str] = None,
        tags: Optional[List[str]] = None,
        contacts: Optional[List[Dict[str, Any]]] = None,
        contact_id: Optional[str] = None,
        email: Optional[str] = None,
        emails: Optional[List[str]] = None,
        user_ids: Optional[List[str]] = None,
        tag: Optional[str] = None,
        tag_id: Optional[str] = None,
        name: Optional[str] = None,
        color: Optional[_COLORS] = None,
        sort_column: Optional[Literal["created_at", "event_checked_in_count",
                                      "event_approved_count", "name",
                                      "revenue_usd_cents"]] = None,
        sort_direction: Optional[Literal["asc", "desc"]] = None,
        full: bool = False,
        calendar_id: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        """Luma — the calendar's audience: contacts (who registered, subscribed
        or was imported) and their tags.

        `op`:
        - `list` — `query` searches names and emails, `tags` keeps contacts
          holding ANY of them; sort by `event_checked_in_count` or
          `revenue_usd_cents` to find the most engaged.
        - `import` — `contacts`: `[{email, name?}]`, tagged with `tags`. They
          can then be invited and receive the calendar's newsletters.
        - Moderation, by `contact_id` or `email` — NOT interchangeable:
          `remove` stops invites and newsletters (they may follow again);
          `block` also bars them from following and joining events;
          `restore` brings either back.
        - `tags` / `create_tag` (`name`, `color`) / `update_tag` / `delete_tag`
          (`tag_id`).
        - `tag` / `untag` — apply or remove `tag` (id or name) on EXISTING
          contacts, by `emails` and/or `user_ids`; creates no contact.

        Args:
            op: the operation, see above.
            query: op='list' — search over names and emails.
            tags: op='list' — filter; op='import' — tags to apply.
            contacts: op='import' — `[{email, name?}]`.
            contact_id: block / remove / restore.
            email: block / remove / restore.
            emails: tag / untag.
            user_ids: tag / untag.
            tag: tag / untag — tag id or name.
            tag_id: update_tag / delete_tag.
            name: create_tag / update_tag.
            color: create_tag / update_tag.
            sort_column: op='list'.
            sort_direction: op='list'.
            full: op='list' — every field of each contact (avatar included).
            calendar_id: with an ORGANIZATION key, the calendar (`cal-…`).
            cursor: next page (`next_cursor` of the previous answer).
            limit: rows per page (the server caps it).
        """
        c = _client(calendar_id)
        if op == "list":
            return _slim(run(lambda: c.list_contacts(
                query=query, tags=tags, sort_column=sort_column,
                sort_direction=sort_direction, limit=limit, cursor=cursor)),
                CONTACT_ROW_OMITTED, full)
        if op == "import":
            need(contacts, "contacts", op)
            return run(lambda: c.import_contacts(contacts, tags=tags))
        if op == "block":
            return run(lambda: c.block_contact(contact_id=contact_id, email=email))
        if op == "remove":
            return run(lambda: c.remove_contact(contact_id=contact_id, email=email))
        if op == "restore":
            return run(lambda: c.restore_contact(contact_id=contact_id, email=email))
        if op == "tags":
            return run(lambda: c.list_contact_tags())
        if op == "create_tag":
            need(name, "name", op)
            return run(lambda: c.create_contact_tag(name, color=color))
        if op == "update_tag":
            need(tag_id, "tag_id", op)
            return run(lambda: c.update_contact_tag(tag_id, name=name, color=color))
        if op == "delete_tag":
            need(tag_id, "tag_id", op)
            return run(lambda: c.delete_contact_tag(tag_id))
        if op == "tag":
            need(tag, "tag", op)
            return run(lambda: c.apply_contact_tag(tag, user_ids=user_ids,
                                                   emails=emails))
        if op == "untag":
            need(tag, "tag", op)
            return run(lambda: c.unapply_contact_tag(tag, user_ids=user_ids,
                                                     emails=emails))
        raise bad_op(op, "list | import | block | remove | restore | tags | "
                         "create_tag | update_tag | delete_tag | tag | untag")

    # --- the calendar, its tags, the organization -----------------------------

    @mcp.tool()
    def luma_calendar(
        op: Literal["me", "get", "update", "admins", "add_admins", "event_tags",
                    "create_event_tag", "update_event_tag", "delete_event_tag",
                    "tag_events", "untag_events", "places", "images",
                    "upload_url", "org_admins", "org_calendars",
                    "create_calendar"] = "get",
        fields: Optional[Dict[str, Any]] = None,
        emails: Optional[List[str]] = None,
        tag: Optional[str] = None,
        tag_id: Optional[str] = None,
        name: Optional[str] = None,
        color: Optional[_COLORS] = None,
        event_ids: Optional[List[str]] = None,
        query: Optional[str] = None,
        content_type: Optional[Literal["image/jpeg", "image/png"]] = None,
        calendar_id: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        """Luma — the key, the calendar and its settings, event tags, lookups
        that feed event creation, and the organization's calendars.

        `op`:
        - `me` — who the key belongs to (the connection check). `get` — the
          calendar. `update` — its settings (`fields`: name, slug,
          description, avatar_url, tint_color, launch_status, website, social
          handles, location; null clears a website or handle).
        - `admins` / `add_admins` (`emails`; adds needing more paid Luma Plus
          seats are refused — do those in the Luma app).
        - `event_tags` / `create_event_tag` / `update_event_tag` /
          `delete_event_tag`; `tag_events` / `untag_events` apply `tag` (id or
          name) to `event_ids`.
        - `places` — Google Maps candidates for `query`; pass the `place_id`
          as `geo_address_json: {type:"google", place_id}` on an event.
          `images` — Luma's stock covers for `query`, usable as `cover_url`.
          `upload_url` — a signed URL to upload your own cover image.
        - `org_admins` / `org_calendars` / `create_calendar` (`name` +
          `fields`) — ORGANIZATION key only.

        Args:
            op: the operation, see above.
            fields: update / create_calendar — the body, as Luma names it.
            emails: op='add_admins'.
            tag: tag_events / untag_events — tag id or name.
            tag_id: update_event_tag / delete_event_tag.
            name: create_event_tag / update_event_tag / create_calendar.
            color: create_event_tag / update_event_tag.
            event_ids: tag_events / untag_events.
            query: places / images.
            content_type: op='upload_url'.
            calendar_id: the calendar (`cal-…`) — required by op='update';
                with an ORGANIZATION key, the calendar every op acts on.
            cursor: next page (`next_cursor` of the previous answer).
            limit: rows per page (the server caps it).
        """
        c = _client(calendar_id)
        if op == "me":
            return run(lambda: c.get_self())
        if op == "get":
            return run(lambda: c.get_calendar())
        if op == "update":
            need(calendar_id, "calendar_id", op)
            need(fields, "fields", op)
            return run(lambda: c.update_calendar(calendar_id, fields))
        if op == "admins":
            return run(lambda: c.list_calendar_admins())
        if op == "add_admins":
            need(emails, "emails", op)
            return run(lambda: c.add_calendar_admins(emails))
        if op == "event_tags":
            return run(lambda: c.list_event_tags())
        if op == "create_event_tag":
            need(name, "name", op)
            return run(lambda: c.create_event_tag(name, color=color))
        if op == "update_event_tag":
            need(tag_id, "tag_id", op)
            return run(lambda: c.update_event_tag(tag_id, name=name, color=color))
        if op == "delete_event_tag":
            need(tag_id, "tag_id", op)
            return run(lambda: c.delete_event_tag(tag_id))
        if op in ("tag_events", "untag_events"):
            need(tag, "tag", op)
            need(event_ids, "event_ids", op)
            if op == "tag_events":
                return run(lambda: c.apply_event_tag(tag, event_ids))
            return run(lambda: c.unapply_event_tag(tag, event_ids))
        if op == "places":
            need(query, "query", op)
            return run(lambda: c.search_places(query))
        if op == "images":
            need(query, "query", op)
            return run(lambda: c.search_images(query, limit=limit, cursor=cursor))
        if op == "upload_url":
            return run(lambda: c.create_upload_url(content_type))
        if op == "org_admins":
            return run(lambda: c.list_organization_admins())
        if op == "org_calendars":
            return run(lambda: c.list_organization_calendars(limit=limit,
                                                             cursor=cursor))
        if op == "create_calendar":
            need(name, "name", op)
            return run(lambda: c.create_organization_calendar(name, fields=fields))
        raise bad_op(op, "me | get | update | admins | add_admins | event_tags | "
                         "create_event_tag | update_event_tag | delete_event_tag | "
                         "tag_events | untag_events | places | images | "
                         "upload_url | org_admins | org_calendars | create_calendar")

    # --- memberships ----------------------------------------------------------

    @mcp.tool()
    def luma_memberships(
        op: Literal["tiers", "add", "set_status"] = "tiers",
        email: Optional[str] = None,
        tier_id: Optional[str] = None,
        user_id: Optional[str] = None,
        status: Optional[Literal["approved", "declined"]] = None,
        skip_payment: Optional[bool] = None,
        registration_answers: Optional[List[Dict[str, Any]]] = None,
        full: bool = False,
        calendar_id: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        """Luma — calendar memberships: tiers and members.

        `op`:
        - `tiers` — the calendar's membership tiers, hidden ones included.
          Members themselves are contacts: `luma_contacts op="list"`.
        - `add` — put `email` in `tier_id`; for a paid tier, `skip_payment`
          when payment is handled outside Luma.
        - `set_status` — `user_id` → approved or declined. ⚠️ Approving a
          member of a PAID tier captures their payment; declining cancels
          their subscription.

        Args:
            op: the operation, see above.
            email: op='add'.
            tier_id: op='add' — the membership tier.
            user_id: op='set_status' — the member's Luma user id.
            status: op='set_status'.
            skip_payment: op='add' — payment handled outside Luma.
            registration_answers: op='add' — answers to the tier's questions.
            full: op='tiers' — with each tier's questions and access info.
            calendar_id: with an ORGANIZATION key, the calendar (`cal-…`).
            cursor: next page (`next_cursor` of the previous answer).
            limit: rows per page (the server caps it).
        """
        c = _client(calendar_id)
        if op == "tiers":
            return _slim(run(lambda: c.list_membership_tiers(limit=limit,
                                                             cursor=cursor)),
                         TIER_ROW_OMITTED, full)
        if op == "add":
            need(email, "email", op)
            need(tier_id, "tier_id", op)
            return run(lambda: c.add_member(
                email, tier_id, skip_payment=skip_payment,
                registration_answers=registration_answers))
        if op == "set_status":
            need(user_id, "user_id", op)
            need(status, "status", op)
            return run(lambda: c.update_member_status(user_id, status))
        raise bad_op(op, "tiers | add | set_status")

    # --- webhooks -------------------------------------------------------------

    @mcp.tool()
    def luma_webhooks(
        op: Literal["list", "get", "create", "update", "delete"] = "list",
        webhook_id: Optional[str] = None,
        url: Optional[str] = None,
        event_types: Optional[List[str]] = None,
        status: Optional[Literal["active", "paused"]] = None,
        full: bool = False,
        calendar_id: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        """Luma — webhook endpoints that receive the calendar's notifications.

        `event_types`: `*` (all) or among calendar.event.added,
        calendar.event.submitted, calendar.person.subscribed,
        calendar.person.unsubscribed, event.created, event.updated,
        event.canceled, guest.registered, guest.updated, guest.refunded,
        ticket.registered. `update` changes the types or pauses / resumes
        (`status`) without deleting. The signing `secret` is left out of
        `list` (named in `omitted`); `get` or `full=true` returns it.

        Args:
            op: list | get | create | update | delete.
            webhook_id: get / update / delete.
            url: op='create' — public HTTPS endpoint.
            event_types: create / update.
            status: op='update' — active or paused.
            full: op='list' — every field, signing secret included.
            calendar_id: with an ORGANIZATION key, the calendar (`cal-…`).
            cursor: next page (`next_cursor` of the previous answer).
            limit: rows per page (the server caps it).
        """
        c = _client(calendar_id)
        if op == "list":
            return _slim(run(lambda: c.list_webhooks(limit=limit, cursor=cursor)),
                         WEBHOOK_ROW_OMITTED, full)
        if op == "get":
            need(webhook_id, "webhook_id", op)
            return run(lambda: c.get_webhook(webhook_id))
        if op == "create":
            need(url, "url", op)
            need(event_types, "event_types", op)
            return run(lambda: c.create_webhook(url, event_types))
        if op == "update":
            need(webhook_id, "webhook_id", op)
            return run(lambda: c.update_webhook(webhook_id, event_types=event_types,
                                                status=status))
        if op == "delete":
            need(webhook_id, "webhook_id", op)
            return run(lambda: c.delete_webhook(webhook_id))
        raise bad_op(op, "list | get | create | update | delete")
