## prerequisite — a Luma API key (Luma Plus)

create a key in Luma, on the calendar or on the organization (Settings → Developer → API keys), then paste it into oto. Reference: [Luma API](https://help.luma.com/p/luma-api).
- ⚠️ **the API requires an active Luma Plus subscription** on the calendar: without it, every call is refused (403) even with a valid key
- a **calendar key** acts on its own calendar. An **organization key** covers all the organization's calendars: calendar calls then need `calendar_id` (`cal-…`, listed by `luma_calendar(op="org_calendars")`), and the `org_*` operations only work with it
- byo-only: no shared oto key — these are your events and attendee lists, each organization sets up its own

## usage — events and their guests

- "our upcoming events" → `luma_events(after="<now, ISO 8601>")`; the description is only in `op="get"`
- "this lu.ma link" → `luma_events(op="resolve", url="https://lu.ma/…")` gives the `evt-…` id
- "who is coming?" → `luma_guests(event_id="evt-…", status="approved")`; "who checked in" → `sort_column="checked_in_at"`; one person's answers and payment → `op="get", guest="<email>"`
- "create an event" → `luma_calendar(op="places", query="…")` for the place, then `luma_event_admin(op="create", fields={"name": …, "start_at": …, "timezone": "Europe/Paris", "geo_address_json": {"type": "google", "place_id": …}})`
- "approve the waitlist" → `luma_guest_admin(event_id=…, op="set_status", guest="<email>", status="approved")` — Luma emails the guest unless `send_email=false`
- "register these people" → `luma_guest_admin(op="add", guests=[{"email": …, "name": …}])`; "invite them" → `op="invite"` (dry-run first)
- "email the attendees" → `luma_blasts(op="send", event_id=…, content_md="…")` (dry-run first)
- "our audience" → `luma_contacts(op="list", sort_column="event_checked_in_count", sort_direction="desc")`; segment with tags (`op="tag"`)
- "a discount code" → `luma_tickets(op="create_coupon", event_id=…, code="EARLY", discount={"discount_type": "percent", "percent_off": 20})`

## note — what reaches people outside your organization

six gestures are **dry-run by default** and need `dry_run=false` to happen: `luma_guest_admin(op="invite")`, `luma_blasts(op="send")`, `luma_event_admin(op="cancel")`, `luma_calendar(op="add_admins")`, `luma_webhooks(op="create")` and `luma_memberships(op="set_status")`.
- ⚠️ **a calendar admin manages every event, guest list and setting** of the calendar
- ⚠️ **a webhook sends the calendar's notifications to a URL**, guest names, emails and registrations included; its signing secret is **never** returned by oto — read it in Luma's dashboard
- ⚠️ **cancelling an event is irreversible**: every guest is notified, the event is **deleted**, and if guests paid you must say `should_refund` explicitly
- ⚠️ **a sent blast cannot be recalled**; deleting it only removes the post from the event page
- ⚠️ **changing a guest's status emails them** unless `send_email=false`; `should_refund` refunds a paid guest moved out of "going"
- ⚠️ **approving a member of a paid membership tier captures their payment**; declining cancels their subscription
- adding guests and sending invites runs in the background: the answer lists who will be **skipped** (unsubscribed, removed, blocked), not the final list — read it back with `luma_guests`

## note — what can mislead

- an event is **managed** by one calendar; a calendar also **lists** events managed elsewhere (`include_viewed=true`), with the location reduced to the city and host-only fields missing
- dates are ISO 8601 in UTC (`2026-11-04T18:30:00.000Z`); the event's `timezone` is separate. Durations are ISO 8601 too (`PT2H`)
- a coupon's discount can **never** be edited after creation — only its remaining count and validity window
- `remove` and `block` on a contact are different: removed people may follow the calendar again, blocked ones cannot join its events
- guest and contact fields (email, name, phone, registration answers) can be masked by an org admin from the connector's "transformations" tab; nothing is masked by default
- rate limit: 200 requests per minute per calendar (500 per organization key); a 429 blocks the key for about a minute
