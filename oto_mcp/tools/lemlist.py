"""Lemlist — campaigns, sequences, schedules, leads, stats and enrichment.

First of the TWO modules of the connector: this one holds the CAMPAIGN and its
leads, `lemlist_crm.py` holds everything else (CRM, inbox, unsubscribes, watch
lists, tasks, shared base, team, mailboxes, lemwarm, deliverability,
webhooks). Together they cover lemlist's 141 documented routes, without
exception — and two inventories prove it rather than assert it:
`test_lemlist_coverage.py` on the oto-core side (every route has a path in the
client), `test_lemlist_surface_coverage.py` here (every client capability is
called by a tool, the few exceptions being named with their reason).

The connector knew how to READ a campaign and drop leads into it; it did not know
how to run one. `lemlist_campaign`, `lemlist_campaign_start`,
`lemlist_sequence`, `lemlist_schedule` and `lemlist_lead` close that gap: create
and configure a campaign, write its sequence step by step, hold its sending
windows, validate it, start it, pause it, duplicate it, measure it,
export it, and carry its leads end to end.

The bound has not disappeared, it has MOVED to where it really bites: on what
puts messages on the wire, not on writing in general. FOUR tools do so
or arm it, and all four are hidden by default
(`DEFAULT_HIDDEN_TOOLS`, self-activatable): `lemlist_campaign_start` (lemlist
runs the sequence for all launched leads), `lemlist_launch_lead` (a lead
leaves review), `lemlist_act` (the three direct sends of the inbox, with no
campaign or review in front of them — and the personas of the shared base) and
`lemlist_campaign_auto_review`. Everything else — create,
configure, duplicate, pause, schedule, tidy the CRM — works on a DRAFT
or on data, and sends nothing.

That grain is what dictated the split: `DEFAULT_HIDDEN_TOOLS` has the grain of the
TOOL, not of the `op`. `start` as `lemlist_campaign(op="start")` would have gone into
a visible tool and silently unhinged the bound — hence a bare tool for it
alone, and likewise for the inbox sends.

Corollary, and it is the least obvious point of the module: `autoReview` /
`autoReviewConditions` do NOT go through `lemlist_campaign`'s settings dict.
The field is not removed from the connector for all that — it has its
own hidden tool, `lemlist_campaign_auto_review`. The reason: this setting
launches any lead as soon as it is added, so it would turn `lemlist_create_lead` — visible,
and visible BECAUSE it sends nothing — into a send path, without any hidden
tool being called. The field remains reachable; it is the ACTION that becomes
explicit.

Enrichment (`lemlist_enrich`, `lemlist_enrich_lead`) sends nothing — but
it SPENDS lemlist credits on every action. Hence the same bound, taken
differently: no action by default, a call with no action requested fails here
(INVALID_PARAMS) rather than going to fetch lemlist's documented 400.

Async surface, like FullEnrich (signal #252): the POST returns an `enrichment_id`
in ~1s and the work continues on the lemlist side. Polling belongs to the agent —
`lemlist_enrich_result` reads a status and hands control back. Never an in-process
wait loop: every MCP client hangs up around 60s, and the result
would be lost WHILE the credits are consumed.

Key resolved per call via `access.resolve_api_key("lemlist")`. No default
platform quota — each user sees THEIR OWN campaigns,
so a user key is required.
"""
from __future__ import annotations

from dataclasses import asdict
import datetime as _dt
from typing import Literal, Optional

import requests
from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..connectors import verify as connector_verify
from ..output_projection import project
from .lecture import LECTURE

#: Action vocabulary of lemlist's v2 bulk. Deliberately written by hand:
#: it is NOT a snake_case of the v1 flags — email verification is called
#: `verify` and not `verify_email`. Mirror of `LemlistClient.ENRICH_BULK_ACTIONS`,
#: kept aligned by a version-skew test.
BULK_ACTIONS = {
    "find_email": "find_email",
    "verify_email": "verify",
    "linkedin_enrichment": "linkedin_enrichment",
    "find_phone": "find_phone",
}


#: The TWO campaign settings that dissolve manual review: with them, a created
#: lead goes out RIGHT AWAY. The connector's safety model rests on the
#: opposite — `lemlist_create_lead` is visible because it sends nothing, and
#: only `lemlist_launch_lead` (hidden by default) triggers sending. Letting them
#: pass through a settings dict would turn a visible tool into a send path,
#: with nothing to signal it. They are not removed from the connector
#: for all that — they have their own hidden tool, `lemlist_campaign_auto_review`:
#: the field remains reachable, it is the ACTION that becomes explicit.
AUTO_REVIEW_KEYS = ("autoReview", "autoReviewConditions")

#: Floor of the stats window. Both dates are REQUIRED on lemlist's side
#: and an agent who wants "the campaign's stats" has none in mind: this
#: floor predates lemlist itself, so it means "since forever".
STATS_EPOCH = "2015-01-01T00:00:00.000Z"

#: Detail returned on `full=True` only — a `steps` per sequence step and
#: a `perChannel` per channel, where the usual question fits in the headline
#: counters.
STATS_DETAIL = ("steps", "perChannel")


#: The two DUPLICATE refusals of `POST /campaigns/{id}/leads/`, recognised by their
#: MESSAGE (plain-text body), never by their status: lemlist changed it under our
#: feet — "Lead already in other campaign" as a 500 on 31/08/2026 (23 calls), the
#: same as a 409 on 09/09 (71 calls), "Lead already in the campaign" as a 400
#: (otomata-tech/oto#263). It is not an outage: the contact is already taken, and
#: the agent must count it as such. Any other refusal keeps its error path.
LEAD_DEJA_PRIS = {
    "Lead already in other campaign": "already_in_other_campaign",
    "Lead already in the campaign": "already_in_campaign",
}


def _refus(code: str, message: str, **data) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=f"Refus `{code}` : {message}",
                              data={"code": code, "retryable": False, **data}))


def _texte(v) -> Optional[str]:
    """A value as lemlist takes a lead variable: a string (its OpenAPI types the
    extra keys of `POST /campaigns/{id}/leads/` `additionalProperties: string`).
    A list of scalars is joined; an object is not guessed at (None)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (str, int, float)):
        return str(v)
    if isinstance(v, list) and all(isinstance(x, (str, int, float)) for x in v):
        return ", ".join(str(x) for x in v)
    return None


def _variables_du_lead(custom_variables: dict, declarees) -> dict:
    """`custom_variables` as lemlist takes them, or a named refusal: a key that is
    a field of the lead (`email`, `firstName`…) would overwrite its argument in the
    same body, and a value that is not a text has no form lemlist documents."""
    collisions = sorted(set(custom_variables) & set(declarees))
    if collisions:
        raise _refus(
            "lemlist_custom_variable_collision",
            f"`custom_variables` names lead fields {collisions}: pass them through "
            "their own argument (`email`, `first_name`…). Nothing was sent.",
            keys=collisions)
    variables, illisibles = {}, []
    for cle, valeur in custom_variables.items():
        texte = _texte(valeur)
        if texte is None:
            illisibles.append(cle)
        else:
            variables[cle] = texte
    if illisibles:
        raise _refus(
            "lemlist_custom_variable_value",
            f"`custom_variables` {sorted(illisibles)}: a lead variable is a text "
            "(a number, a boolean or a list of them is written as text); an "
            "object or null is not. Nothing was sent.", keys=sorted(illisibles))
    return variables


def _campagne_introuvable(exc) -> bool:
    """Le 404 « Campaign not found » de `POST /campaigns/{id}/leads/` (doc API)."""
    body = getattr(exc, "body", None)
    return (getattr(exc, "status_code", None) == 404 and isinstance(body, str)
            and body.strip() == "Campaign not found")


#: What `lemlist_create_lead` says about the review of a created lead (otomata-tech/oto#264):
#: "unknown", because lemlist says it NOWHERE in this response. Measured on
#: 04/09/2026: `isPaused` stays `false` whether a lead is held in review or gone; only
#: the campaign's counters (`reviewedCount`, `inSequenceLeadCount`) tell them apart.
#: `isPaused` is therefore DROPPED from the response — left in, it read as "sending goes out".
REVIEW_STATE_UNKNOWN = "unknown"
REVIEW_HINT = (
    "lemlist does not say, at creation, whether this lead awaits review or goes into the "
    "sequence; `isPaused` was dropped because it does not tell them apart. To find out: "
    "lemlist_campaign(op=\"reports\") — if `reviewedCount` or `inSequenceLeadCount` "
    "went up with the addition, the lead goes out; only `totalCount` going up = held in review."
)


def _lead_deja_pris(exc) -> Optional[str]:
    """La `reason` d'un refus de doublon de lemlist, `None` pour tout autre refus."""
    body = getattr(exc, "body", None)
    return LEAD_DEJA_PRIS.get(body.strip()) if isinstance(body, str) else None


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _default_window(start_date: Optional[str], end_date: Optional[str]) -> tuple[str, str]:
    """Fill in the stats window — ISO 8601 bounds, both required upstream."""
    # Aliased `_dt`: `timezone` is an ARGUMENT of `lemlist_campaign`/
    # `lemlist_schedule` (lemlist's IANA zone), importing it bare would shadow it.
    now = _dt.datetime.now(_dt.timezone.utc)
    return start_date or STATS_EPOCH, end_date or (
        now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z")


def _project_stats(result: dict, *, full: bool) -> dict:
    """Cut the per-step/per-channel detail, and NAME what was dropped."""
    if full or not isinstance(result, dict):
        return result
    dropped = {k: len(result[k]) for k in STATS_DETAIL
               if isinstance(result.get(k), (list, dict))}
    out = project(result, drop=STATS_DETAIL)
    if dropped:
        out["projection"] = {
            "dropped": dropped,
            "hint": "Per-step / per-channel detail dropped — `full=True` returns it.",
        }
    return out


#: Cap on a voice note's audio — 20 MB, the limit lemlist announces
#: on its media. The bound is here because WE do the downloading: an
#: MCP agent has no disk shared with the server.
AUDIO_MAX_BYTES = 20 * 1024 * 1024


def _fetch_audio(source) -> tuple[bytes, str]:
    """Fetch a voice note's audio, through the SHARED `file_source` seam.

    Hand-written on the first pass, this function redid a size guard
    and a schema check — but NOT the anti-SSRF: a URL supplied by an
    agent could have made the server read `localhost` or the cloud IMDS. The seam
    (`file_source.resolve`, already used by lighton and pennylane) carries that
    guard, refuses redirects, and also accepts `{"kind": "drive"}` and
    `{"kind": "gmail"}` and `{"kind": "project_file"}` — the audio can therefore come
    from a Drive, an attachment or a project file, not only from a
    public URL.

    Returns `(bytes, file name)`: the name comes from the SOURCE (attachment,
    Drive file, last URL segment) and goes out as multipart. Dropping it
    would make every voice note arrive under the same generic name
    on the lemlist side.
    """
    from .. import file_source

    if isinstance(source, str):
        source = {"kind": "url", "url": source}
    try:
        resolved = file_source.resolve(source, max_bytes=AUDIO_MAX_BYTES)
    except file_source.FileSourceError as e:
        raise _bad(f"audio illisible : {e}")
    return resolved.data, resolved.filename or "audio.mp3"


def _refuse_auto_review(settings: dict) -> None:
    """Refuse `autoReview*` — cf. AUTO_REVIEW_KEYS."""
    present = [k for k in AUTO_REVIEW_KEYS if k in settings]
    if present:
        raise _bad(
            f"{', '.join(present)} cannot be set here. This setting makes any added "
            "lead go out WITHOUT review: it would turn `lemlist_create_lead` "
            "into a send. It has its own tool, `lemlist_campaign_auto_review`, hidden "
            "by default (`oto_enable_tool lemlist_campaign_auto_review`) — arming "
            "sending takes a deliberate action, not a key slipped into a settings "
            "dict."
        )


def _found_digest(data: dict) -> dict:
    """What was REALLY found, per axis — `data` always carries the key of
    the requested axis, even empty, so its mere presence says nothing.

    Shapes observed live (beyond the published schema): `email` carries `email`
    and a verification `status` (`deliverable`/`undeliverable`), `phone`
    carries `phone`, `linkedin` carries a full profile — or `{}` when the profile
    could not be resolved. `notFound` is NOT reliable: it was seen as `false`
    on a payload with no number.
    """
    found = {}
    email = (data.get("email") or {}).get("email")
    if email:
        found["email"] = email
    status = (data.get("email") or {}).get("status")
    if status:
        found["email_status"] = status
    phone = (data.get("phone") or {}).get("phone")
    if phone:
        found["phone"] = phone
    linkedin = data.get("linkedin") or {}
    if linkedin:
        found["linkedin"] = {
            k: v for k, v in linkedin.items()
            if k in ("firstName", "lastName", "tagline", "locationName", "linkedinUrl")
        } or True
    return found


def _verify(fields: dict, config: dict | None = None) -> None:
    """"Test the connection" probe — otomata-tech/oto#69. Covers `auth` + RIGHTS.

    `GET /team` (already in the client — `LemlistClient.get_team`). What the doc
    establishes:

    - **authenticated** — Basic auth, the key as the password, like the rest of
      the lemlist API;
    - **no side effect** — a team read (`_id`, `name`, `billing`);
    - **the cost** — no mention of a cost for this read call. Absence of a
      mention is a hint, not proof — like Folk and Pennylane.

    **Authenticated ≠ usable** (class oto#69): `billing.ok` tells apart a team
    whose subscription is in good standing from one whose it no longer is
    (unpaid, suspended) — an axis that touches the WHOLE connector. Does NOT read the
    enrichment credits (`get_team_credits`): those gate ONLY one
    feature (`lemlist_enrich`) — declaring them "dry" would say "connector
    dead" of a key that can still do everything on campaigns/sequences/leads,
    exactly the opposite of the misleading green that this series corrects.
    """
    from oto.tools.lemlist import LemlistClient

    infos = LemlistClient(api_key=fields["key"]).get_team() or {}
    if not infos.get("_id"):
        raise RuntimeError(
            "Lemlist answered without identifying a team for this key — "
            f"unexpected response: {str(infos)[:200]}")
    billing = infos.get("billing") or {}
    if billing.get("ok") is False:
        raise RuntimeError(
            f"The team \"{infos.get('name') or '?'}\" authenticates, but its "
            "Lemlist billing is not in good standing (unpaid or "
            "suspended subscription) — reactivate it at Lemlist.")


def register(mcp: FastMCP) -> None:
    from oto.tools.lemlist import LemlistClient

    connector_verify.register("lemlist", _verify)

    def _client(units: int = 1) -> tuple[LemlistClient, bool]:
        key, is_platform = access.resolve_api_key("lemlist", units=units)
        return LemlistClient(api_key=key), is_platform

    @mcp.tool(annotations=LECTURE)
    def lemlist_status() -> dict:
        """Workspace status (account, credits, plan).

        Never raises on a dead/invalid key — check `connected` first:
        `{"connected": true, "campaigns_count", "campaigns_capped"}` on
        success, `{"connected": false, "error": "<message>"}` on failure. A
        200 response with `connected: false` IS the failure — don't treat a
        non-empty result as automatically working."""
        client, is_platform = _client()
        result = client.status()
        return result

    @mcp.tool(annotations=LECTURE)
    def lemlist_list_campaigns(
        status: Optional[str] = None,
        created_by: Optional[str] = None,
        newest_first: bool = False,
        max_campaigns: int = 500,
    ) -> dict:
        """List the campaigns of the workspace.

        Returns `{campaigns: [{id, name, status, senders, emoji, labels,
        timezone, created_at, created_by, has_error, errors}], count,
        truncated}`. `truncated: true` means the ceiling was hit and the list is
        INCOMPLETE — do not conclude a campaign is absent from it.

        Args:
            status: Keep only `running`, `draft`, `archived`, `ended`, `paused`
                or `errors`. A campaign can hold several at once (paused WITH
                errors), so this filters, it does not partition.
            created_by: Keep only campaigns created by a user id (`usr_…`).
            newest_first: Sort on creation date, most recent first.
            max_campaigns: Ceiling on the walk (lemlist pages 100 at a time).
        """
        client, is_platform = _client()
        filters = {}
        if status is not None:
            filters["status"] = status
        if created_by is not None:
            filters["created_by"] = created_by
        if newest_first:
            filters["sort_order"] = "desc"
        pages = max(1, -(-max_campaigns // 100))  # ceil
        campaigns, truncated = client.list_all_campaigns(max_pages=pages, **filters)
        return {
            "campaigns": [asdict(c) for c in campaigns],
            "count": len(campaigns),
            "truncated": truncated,
        }

    @mcp.tool(annotations=LECTURE)
    def lemlist_get_campaign(campaign_id: str) -> dict:
        """Fetch full campaign details by ID."""
        client, is_platform = _client()
        result = client.get_campaign(campaign_id)
        return result

    @mcp.tool(annotations=LECTURE)
    def lemlist_get_campaign_stats(
        campaign_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        channels: Optional[list[str]] = None,
        ab_selected: Optional[str] = None,
        send_user: Optional[str] = None,
        full: bool = False,
    ) -> dict:
        """Campaign performance (leads reached, opened, replied, bounced…).

        Reads lemlist's own counters. Previously derived from one page of
        activities, which under-counted every campaign past 1000 events — the
        field names changed with the fix (`nbLeads`, `messagesSent`, `opened`,
        `replied`… instead of `emails_sent` & co).

        Args:
            start_date / end_date: ISO 8601 window. Defaults to "since 2015" →
                now, i.e. the campaign's whole life.
            channels: Any of `email`, `linkedin`, `others`.
            ab_selected: `A` or `B`, to read one side of a running A/B test.
            send_user: `usr_…|sender@email` — both halves required.
            full: Also return the per-step (`steps`) and per-channel
                (`perChannel`) breakdowns, dropped by default for size.
        """
        client, is_platform = _client()
        start, end = _default_window(start_date, end_date)
        result = client.get_campaign_stats_v2(
            campaign_id, start_date=start, end_date=end,
            channels=channels, ab_selected=ab_selected, send_user=send_user,
        )
        return _project_stats(result, full=full)

    @mcp.tool(annotations=LECTURE)
    def lemlist_get_activities(
        campaign_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        activity_type: Optional[str] = None,
        lead_id: Optional[str] = None,
        is_first: Optional[bool] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        min_date: Optional[str] = None,
        max_date: Optional[str] = None,
        all_pages: bool = False,
        since: Optional[str] = None,
        max_pages: int = 50,
        fields: Optional[list[str]] = None,
        compact: bool = False,
    ) -> dict:
        """Get activity events (opens, clicks, replies, LinkedIn actions…).

        Each event carries its whole lead (every personalised copy of the
        sequence, in HTML, and the custom variables) and lemlist's metadata:
        100 events run to hundreds of thousands of characters. Pass
        `compact=True` unless you need a field it drops.

        Args:
            campaign_id: Restrict to a campaign.
            limit: Max events (default 100).
            offset: Pagination offset.
            activity_type: One event name (`emailsSent`, `emailsOpened`,
                `emailsReplied`, `linkedinInviteAccepted`, `paused`…).
            lead_id: Restrict to one lead.
            is_first: Keep only the first event of its kind per lead.
            start_date / end_date: ISO 8601 bounds.
            min_date / max_date: the OTHER documented pair of bounds — lemlist
                exposes both on this route; kept distinct rather than guessed
                into one.
            all_pages: Walk the pages instead of returning one. Use `since`
                (ISO date) to stop early, and `max_pages` to cap the cost
                (default 50 = 5 000 events). Ignores the other filters — the
                paging route only takes the campaign and the date floor.
            since: With `all_pages`, keep only what is newer than this date.
            max_pages: Ceiling on the walk.
            compact: The short view of each event: `_id`, `type`,
                `createdAt`, `campaignId`, `campaignName`, `sequenceStep`,
                `leadId`, `contactId`, `leadEmail`, `leadFirstName`,
                `leadLastName`, `leadCompanyName`, `fromEmail` (who replied),
                `sendUserEmail`, `subject`, and `excerpt`: the first 280
                characters of the message body (text first, else lemlist's
                preview, else the HTML body without its tags). Never HTML,
                never the lead's variables.
            fields: Keep only these keys of each event, a nested one by a
                dotted path (`lead.variables.email`, lists traversed, no
                indexes); a key an event lacks is skipped. With
                `compact=True`, each field named is taken from the full event
                and added to (or replaces) the short view's key of the same
                first level. Neither: the events as lemlist sends them.
        """
        client, is_platform = _client()
        # Passed only when asked for: a call without a view stays the call lemlist
        # has always received, whatever the installed client.
        view = {}
        if fields is not None:
            view["fields"] = fields
        if compact:
            view["compact"] = compact
        try:
            if all_pages:
                events = client.sync_activities(
                    campaign_id=campaign_id, since=since, max_pages=max_pages,
                    **view)
            else:
                events = client.get_activities(
                    campaign_id=campaign_id, limit=limit, offset=offset,
                    type=activity_type, lead_id=lead_id, is_first=is_first,
                    start_date=start_date, end_date=end_date,
                    min_date=min_date, max_date=max_date, **view,
                )
        except ValueError as e:
            # A malformed `fields` or `compact`: the client refuses it before any
            # request. Only then: `requests` has ValueErrors of its own (a bad URL,
            # a body that is not JSON), which say nothing about the arguments.
            if not view or isinstance(e, requests.RequestException):
                raise
            raise _bad(str(e)) from e
        return {"activities": events, "count": len(events)}

    @mcp.tool(annotations=LECTURE)
    def lemlist_get_leads(campaign_id: str) -> dict:
        """List all leads for a campaign with their state (sent, replied…).

        ⚠️ Goes through the JSON export, NOT `get_all_leads`: the latter calls
        the export without `state`, hence with lemlist's default — which filters everything out and
        returns an empty list that reads as "no leads". A one-lead campaign
        thus came back empty (signal 719) while the single-lead route returned it just
        fine, and the guide announced the forcing as a given for the whole connector.
        `export_campaign_leads` carries the default `state="all"`, verified live on
        2026-08-31: we take the surface that has the guard rather than rebuild one.
        """
        client, is_platform = _client()
        exported = client.export_campaign_leads(campaign_id, format="json")
        return {"leads": exported if isinstance(exported, list)
                else (exported or {}).get("leads", exported)}

    @mcp.tool()
    def lemlist_create_lead(
        campaign_id: str,
        email: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        company_name: Optional[str] = None,
        job_title: Optional[str] = None,
        linkedin_url: Optional[str] = None,
        phone: Optional[str] = None,
        company_domain: Optional[str] = None,
        icebreaker: Optional[str] = None,
        timezone: Optional[str] = None,
        contact_owner: Optional[str] = None,
        custom_variables: Optional[dict] = None,
        deduplicate: bool = False,
        linkedin_enrichment: bool = False,
        find_email: bool = False,
        verify_email: bool = False,
        find_phone: bool = False,
    ) -> dict:
        """Create a lead in a campaign.

        Several leads: lemlist_push_rows reads them from a datastore table instead.

        All lead fields are optional (lemlist accepts phone/LinkedIn-only
        leads), but you'll usually pass at least `email` or `linkedin_url`.
        `custom_variables` merges extra key-value pairs into the lead, used for
        campaign personalization (e.g. `{{variableName}}` in a template). Each
        value goes as text (a number or boolean written out, a list joined); an
        object or null is refused (`lemlist_custom_variable_value`), and so is a
        key that is a lead field such as `email` or `firstName`
        (`lemlist_custom_variable_collision`) — pass it through its argument.

        Enrichment flags (all default False, each may cost lemlist credits):
        `deduplicate` skips the insert if the email already exists in another
        campaign, `linkedin_enrichment` runs LinkedIn enrichment,
        `find_email`/`verify_email` find or verify the email, `find_phone`
        finds a phone number.

        `campaign_id` is lemlist's campaign id as it returns it, WITH its `cam_`
        prefix (`cam_A1B2C3…`) — an id without it is refused before any call.

        Returns the created lead, including `_id` — pass it to
        `lemlist_launch_lead`/`lemlist_add_lead_variables`. A response without a
        lead `_id` is a named refusal (`lemlist_lead_not_created`), never a
        success. `review_state` is always `"unknown"`: lemlist's answer does not
        say whether the lead is held for review or already in sequence (its
        `isPaused` does not tell them apart, so it is removed). To know if it
        will send, read `lemlist_campaign(op="reports")`: `reviewedCount` or
        `inSequenceLeadCount` rising with the add means it goes out; only
        `totalCount` rising means it is held for review until
        `lemlist_launch_lead`.
        A 404 « Campaign not found » is a named refusal
        (`lemlist_campaign_not_found`): the campaign is not reachable by the key
        in use for leads, even if its reports are.

        A lead lemlist refuses as a DUPLICATE is not an error: the call returns
        `{created: false, reason, message, campaign_id, lead}` with `reason`
        `already_in_other_campaign` or `already_in_campaign` — nothing was
        created; count the contact as already taken and move on.
        """
        champs = {
            "email": email,
            "firstName": first_name,
            "lastName": last_name,
            "companyName": company_name,
            "jobTitle": job_title,
            "linkedinUrl": linkedin_url,
            "phone": phone,
            "companyDomain": company_domain,
            "icebreaker": icebreaker,
            "timezone": timezone,
            "contactOwner": contact_owner,
        }
        lead = {k: v for k, v in champs.items() if v is not None}
        if custom_variables:
            lead.update(_variables_du_lead(custom_variables, champs))
        from oto.tools.common.errors import UpstreamHTTPError

        if not campaign_id.startswith("cam_"):
            # oto#1072: without the prefix, lemlist answered 200 WITHOUT creating a lead.
            raise _refus(
                "lemlist_campaign_id_format",
                f"`campaign_id` must be the lemlist id with its `cam_` prefix "
                f"(e.g. `cam_{campaign_id}`), as lemlist_campaign returns it. "
                "Nothing was sent.", campaign_id=campaign_id)
        client, is_platform = _client()
        try:
            result = client.create_lead(
                campaign_id, lead,
                deduplicate=deduplicate, linkedin_enrichment=linkedin_enrichment,
                find_email=find_email, verify_email=verify_email, find_phone=find_phone,
            )
        except UpstreamHTTPError as e:
            if _campagne_introuvable(e):
                raise _refus(
                    "lemlist_campaign_not_found",
                    f"lemlist cannot find campaign `{campaign_id}` for the key "
                    "used (404). Check the id with lemlist_campaign, and that the "
                    "campaign belongs to this key's lemlist team (`_account` / "
                    "`_instance`): its reports may stay readable when its leads "
                    "are not. Nothing was created.", campaign_id=campaign_id)
            reason = _lead_deja_pris(e)
            if reason is None:
                raise
            return {"created": False, "reason": reason, "message": e.body.strip(),
                    "campaign_id": campaign_id, "lead": lead}
        if not (isinstance(result, dict) and result.get("_id")):
            # oto#1072: an empty success, with no lead created, read as a success.
            raise _refus(
                "lemlist_lead_not_created",
                f"lemlist answered without a lead id for campaign `{campaign_id}`: "
                "no lead was created. Check the campaign id and the lead's "
                "fields before retrying.", campaign_id=campaign_id)
        # oto#264: the review state is not in the response — say so, and drop
        # the field that was wrongly read as its answer.
        return {**project(result, drop=("isPaused",)),
                "review_state": REVIEW_STATE_UNKNOWN, "review_hint": REVIEW_HINT}

    def _require_action(**flags: bool) -> None:
        if not any(flags.values()):
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=("No enrichment requested — set at least one of "
                         "find_email, verify_email, linkedin_enrichment, find_phone."),
            ))

    @mcp.tool()
    def lemlist_enrich(
        email: Optional[str] = None,
        linkedin_url: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        company_name: Optional[str] = None,
        company_domain: Optional[str] = None,
        find_email: bool = False,
        verify_email: bool = False,
        linkedin_enrichment: bool = False,
        find_phone: bool = False,
        webhook_url: Optional[str] = None,
    ) -> dict:
        """Submit an ASYNC enrichment on a person — no campaign, no lead needed.

        Returns immediately with an `enrichment_id`; the work runs server-side.
        THEN call `lemlist_enrich_result(enrichment_id)` to collect it (first
        poll after ~10-20s, then every ~15-30s until `done`).

        Args:
            email, linkedin_url, first_name, last_name, company_name,
                company_domain: the identity to enrich. All optional, but
                lemlist only resolves what it can match — pass a LinkedIn URL,
                or a first/last name together with a company domain.

        
        Enrichment actions — at least one is required, and each spends lemlist
        credits: `find_email` finds a verified email, `verify_email` verifies the
        email you passed (debounce), `linkedin_enrichment` runs the LinkedIn
        enrichment, `find_phone` finds a phone number. Ask only for what you need.
        """
        _require_action(
            find_email=find_email, verify_email=verify_email,
            linkedin_enrichment=linkedin_enrichment, find_phone=find_phone,
        )
        client, is_platform = _client()
        result = client.enrich(
            email=email, linkedin_url=linkedin_url,
            first_name=first_name, last_name=last_name,
            company_name=company_name, company_domain=company_domain,
            find_email=find_email, verify_email=verify_email,
            linkedin_enrichment=linkedin_enrichment, find_phone=find_phone,
            webhook_url=webhook_url,
        )
        return {
            "enrichment_id": result.get("id"),
            "next_step": ("Enrichment accepted. Call "
                          "lemlist_enrich_result(enrichment_id) in ~10-20s."),
        }

    @mcp.tool()
    def lemlist_enrich_lead(
        lead_id: str,
        find_email: bool = False,
        verify_email: bool = False,
        linkedin_enrichment: bool = False,
        find_phone: bool = False,
        webhook_url: Optional[str] = None,
    ) -> dict:
        """Enrich a lead that is ALREADY in a campaign, in place.

        Same actions as `lemlist_enrich`, but the identity comes from the
        existing lead and lemlist writes the result back onto it. Async too:
        returns an `enrichment_id` for `lemlist_enrich_result`.

        Only works on a lead still AWAITING REVIEW — lemlist answers
        `400 "lemrich is not available for lead reviewed"` once a lead has been
        reviewed, which is the state of every lead in a campaign without
        review-before-send. For anyone else, enrich the person with
        `lemlist_enrich` and write the result back yourself.

        Enrichment actions — at least one is required, and each spends lemlist
        credits: `find_email` finds a verified email, `verify_email` verifies the
        email you passed (debounce), `linkedin_enrichment` runs the LinkedIn
        enrichment, `find_phone` finds a phone number. Ask only for what you need.

        Args:
            lead_id: the lead's `_id`, as returned by `lemlist_create_lead`.
        """
        _require_action(
            find_email=find_email, verify_email=verify_email,
            linkedin_enrichment=linkedin_enrichment, find_phone=find_phone,
        )
        client, is_platform = _client()
        result = client.enrich_lead(
            lead_id,
            find_email=find_email, verify_email=verify_email,
            linkedin_enrichment=linkedin_enrichment, find_phone=find_phone,
            webhook_url=webhook_url,
        )
        return {
            "enrichment_id": result.get("id"),
            "next_step": ("Enrichment accepted. Call "
                          "lemlist_enrich_result(enrichment_id) in ~10-20s."),
        }

    @mcp.tool(annotations=LECTURE)
    def lemlist_enrich_result(enrichment_id: str | list[str]) -> dict:
        """Collect the result of an enrichment submitted with `lemlist_enrich`,
        `lemlist_enrich_lead` or `lemlist_enrich_bulk`.

        Single status check per id, returns immediately — no waiting. If a
        result is not `done`, wait ~15-30s and call again.

        Returns `results`: one entry per id, `{enrichment_id, status, done,
        input, data, found}`. `status` is `done`, `in-progress` or `not-found`.
        `data` is lemlist's raw payload; `found` is the digest to read — only
        the axes that actually carry a value (`email`, `email_status`
        `deliverable`/`undeliverable`, `phone`, `linkedin`). An axis key is
        present in `data` even when empty, so presence alone means nothing, and
        `notFound: false` has been seen on a payload with no number.

        A result can come back `done` with nothing in it: lemlist sometimes
        flips the status before the payload lands. Such an entry carries a
        `warning` and is NOT counted in `all_done` — poll it once more before
        concluding nothing was found (a poll costs no credits).

        Args:
            enrichment_id: one id, or a list of ids (a bulk submit yields one id
                per person, so pass them all here in one go).
        """
        ids = [enrichment_id] if isinstance(enrichment_id, str) else list(enrichment_id)
        client, _ = _client()
        results = []
        for eid in ids:
            res = client.get_enrichment(eid)
            status = res.get("enrichmentStatus", "unknown")
            data = res.get("data") or {}
            row = {
                "enrichment_id": res.get("enrichmentId", eid),
                "status": status,
                # `not-found` is terminal too: polling again will not make it
                # appear, it is an id unknown to lemlist.
                "done": status in ("done", "not-found"),
                "input": res.get("input", {}),
                "data": data,
                "found": _found_digest(data),
            }
            if status == "done" and not row["found"]:
                # Observed live: lemlist sometimes flips to `done` BEFORE the
                # payload is in place (an empty `data`, then populated at the
                # next read). Without this safeguard, an agent reads "done + nothing"
                # and concludes "not found" on data that arrives right after.
                # A read costs no credit: might as well redo it once.
                row["warning"] = (
                    "done but empty — lemlist sometimes flips to done before the "
                    "payload lands. Poll once more (~15s) before concluding "
                    "nothing was found."
                )
            results.append(row)
        pending = [r["enrichment_id"] for r in results if not r["done"]]
        settling = [r["enrichment_id"] for r in results if r.get("warning")]
        # `all_done` speaks ONLY of what is still running: a legitimately
        # empty result (person not found) would stay so forever, and an
        # agent looping on `all_done` would never stop. Re-reading
        # an empty `done` is a SUGGESTION, to do once — not an
        # exit condition.
        out = {"results": results, "all_done": not pending}
        if settling:
            out["recheck_suggested"] = settling
        if pending or settling:
            bits = []
            if pending:
                bits.append(f"{len(pending)} still running")
            if settling:
                bits.append(f"{len(settling)} done-but-empty (re-poll ONCE, "
                            "then treat as not found)")
            out["next_step"] = (
                ", ".join(bits) + " — call lemlist_enrich_result again in ~15-30s."
            )
        return out

    @mcp.tool()
    def lemlist_enrich_bulk(
        people: list[dict], webhook_url: Optional[str] = None,
    ) -> dict:
        """Submit several enrichments in one call.

        Returns `submitted`: one entry per person, in order, each carrying
        either `enrichment_id` or `error` (e.g. `MISSING_INPUTS`). Unlike a
        FullEnrich job, a bulk submit yields one id PER PERSON — pass the whole
        list of ids to `lemlist_enrich_result`.

        Args:
            people: one entry per person. Identity keys (all optional, same
                matching rules as `lemlist_enrich`): `email`, `linkedin_url`,
                `first_name`, `last_name`, `company_name`, `company_domain`.
                Plus `actions`: a list among `find_email`, `verify_email`,
                `linkedin_enrichment`, `find_phone` — required, and each action
                spends lemlist credits per person.
        """
        if not people:
            raise McpError(ErrorData(
                code=INVALID_PARAMS, message="`people` is empty — nothing to enrich.",
            ))
        client, is_platform = _client(units=len(people))

        items = []
        for i, person in enumerate(people):
            actions = person.get("actions") or []
            if isinstance(actions, str):
                actions = [actions]
            unknown = [a for a in actions if a not in BULK_ACTIONS]
            if unknown:
                raise McpError(ErrorData(
                    code=INVALID_PARAMS,
                    message=(f"people[{i}]: unknown action(s) {unknown} — allowed: "
                             f"{sorted(BULK_ACTIONS)}."),
                ))
            if not actions:
                raise McpError(ErrorData(
                    code=INVALID_PARAMS,
                    message=(f"people[{i}]: no `actions` — set at least one of "
                             f"{sorted(BULK_ACTIONS)}."),
                ))
            item = {
                "input": {
                    k: v for k, v in {
                        "email": person.get("email"),
                        "linkedinUrl": person.get("linkedin_url"),
                        "firstName": person.get("first_name"),
                        "lastName": person.get("last_name"),
                        "companyName": person.get("company_name"),
                        "companyDomain": person.get("company_domain"),
                    }.items() if v is not None
                },
                # v2 vocabulary: `verify`, not `verify_email` — the mapping
                # table lives in the client, it is not a mechanical snake_case
                # of the v1 flags.
                "enrichmentRequests": [BULK_ACTIONS[a] for a in actions],
                "metadata": {"index": str(i)},
            }
            items.append(item)

        raw = client.bulk_enrich(items, webhook_url=webhook_url)
        submitted = []
        for i, entry in enumerate(raw if isinstance(raw, list) else []):
            # `metadata` is returned as is by lemlist, but its shape is not
            # stable (their own example shows `{"id": ...}` AND a bare
            # string): we use it when it does carry the index we
            # set, otherwise we fall back on the position — entries come back
            # in the submitted order.
            meta = entry.get("metadata")
            index = i
            if isinstance(meta, dict) and str(meta.get("index", "")).isdigit():
                index = int(meta["index"])
            row = {"index": index}
            if entry.get("id"):
                row["enrichment_id"] = entry["id"]
            if entry.get("error"):
                row["error"] = entry["error"]
            submitted.append(row)
        ids = [r["enrichment_id"] for r in submitted if r.get("enrichment_id")]
        return {
            "submitted": submitted,
            "enrichment_ids": ids,
            "next_step": ("Call lemlist_enrich_result(enrichment_ids) in ~10-20s."
                          if ids else "Nothing accepted — check the per-entry errors."),
        }

    @mcp.tool()
    def lemlist_launch_lead(lead_id: str) -> dict:
        """Launch a lead held for manual review.

        Only relevant for a campaign with review-before-send enabled — such a
        campaign holds a newly created lead in review until launched (counted in
        the campaign's `reviewedCount`; the lead's `isPaused` stays `false`). Returns
        `{"ok": true}` on success; raises with a lemlist error code if it can't
        launch (already launched, paused, no sender available, invalid AI
        variable, campaign step errors…).
        """
        client, is_platform = _client()
        result = client.launch_lead(lead_id)
        return result

    @mcp.tool()
    def lemlist_add_lead_variables(lead_id: str, variables: dict) -> dict:
        """Set custom variables on a lead — merged into its personalization
        data, e.g. for `{{variableName}}` placeholders in campaign templates."""
        client, is_platform = _client()
        result = client.add_lead_variables(lead_id, variables)
        return result

    # --- Campaign management ---------------------------------------------------
    #
    # Three `op` tools, one bare tool. The split is not cosmetic: the
    # default hiding (`DEFAULT_HIDDEN_TOOLS`) has the grain of the TOOL, not of
    # the op. `start` — the only action here that puts messages on the wire — therefore
    # lives apart, hidden; the rest (create, configure, duplicate, pause,
    # validate) sends nothing and fits in one visible tool per family.

    @mcp.tool()
    def lemlist_campaign(
        op: Literal["create", "update", "pause", "duplicate", "statutes",
                    "reports", "batch_stats", "export_start", "export_status",
                    "export_email", "export_leads"],
        campaign_id: Optional[str] = None,
        campaign_ids: Optional[list[str]] = None,
        name: Optional[str] = None,
        timezone: Optional[str] = None,
        settings: Optional[dict] = None,
        sender_user_ids: Optional[list[str]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        channels: Optional[list[str]] = None,
        send_user: Optional[str] = None,
        ab_selected: Optional[str] = None,
        export_id: Optional[str] = None,
        email: Optional[str] = None,
        state: Optional[str] = None,
        format: Optional[str] = None,
        full: bool = False,
    ) -> dict:
        """Manage campaigns: create, configure, pause, duplicate, validate,
        report, export. `oto_guide op=read slug="lemlist-playbook"`: build order, where each id comes from, and the doc↔API gaps.

        Nothing here sends by itself — but a created or duplicated campaign is
        born RUNNING (its `status` reads "draft"): `op="pause"` is the switch.
        Putting messages on the wire is `lemlist_campaign_start` (hidden by
        default — enable it with `oto_enable_tool lemlist_campaign_start`).

        Args by op:
        - `create`: `name` (required), optional `timezone` (IANA, drives the
          auto-created schedule; server default `Europe/Paris`). Returns the
          campaign with `sequenceId` and `scheduleIds` — the two ids
          `lemlist_sequence` and `lemlist_schedule` need. `settings` is REFUSED
          here (the endpoint takes name + timezone only) — chain `update` on
          the returned id rather than believe a setting landed.
          ⚠️ The campaign is created in state RUNNING, not draft (its `status`
          reads "draft" only because it has no step and no lead yet). It sends
          nothing while the review gate holds, but chain `op="pause"` if you
          want to build it with the switch off.
        - `update`: `campaign_id` + `name`, `sender_user_ids` (`usr_…`, the
          senders) and/or `settings` (raw PATCH body: `stopOnEmailReplied`,
          `stopOnMeetingBooked`, `stopOnLinkClicked`, `disableTrackOpen`,
          `disableTrackClick`, `disableTrackReply`, `tracking`, `onReplied`,
          `aiFeatures`…). Only the keys sent change.
        - `pause`: `campaign_id`. THE off switch — a campaign created here is
          already running. Stops it advancing; already-scheduled leads are NOT
          recalled. Errors if the campaign is not running.
        - `duplicate`: `campaign_id` + optional `name`. Copies sequence, steps,
          schedules and AI templates into a new campaign (CRM settings excluded).
          ⚠️ Like `create`, the copy runs (`state` running in `reports`) although
          its `status` reads "draft": chain `op="pause"` to hold it.
        - `statutes`: `campaign_id`. The validation the lemlist UI runs — read it
          BEFORE starting: `level` 3 blocks the launch (no sender, broken DNS),
          2 warns (daily limit, missing schedule), 1 informs.
        - `reports`: `campaign_ids`. One row per campaign in operator vocabulary
          (`emailsSent`, `emailsOpened`, `emailsReplied`, `senderNames`, `state`)
          — the shape for comparing campaigns.
        - `batch_stats`: `campaign_ids` (≤ 100) + optional `start_date`/`end_date`
          (defaults to the whole life), `channels` (`email`/`linkedin`/`others`),
          `send_user` (`usr_…|sender@email`), `ab_selected` (`A`/`B`).
          Same counters as `lemlist_get_campaign_stats`, in one call.

        - `export_start`: `campaign_id`. Opens an ASYNCHRONOUS stats export and
          returns its id; poll `export_status` (`campaign_id` + `export_id`),
          or ask to be notified with `export_email` (+ `email`).
        - `export_leads`: `campaign_id` + optional `state` (defaults to `all`;
          lemlist's own default filters EVERYTHING out and returns an empty
          list that reads as "no leads"), `format` (`csv`, the API default, or
          `json`). Returns the leads directly — the synchronous cousin of the
          export above.

        `autoReview`/`autoReviewConditions` are not settable here: they make
        every added lead send immediately, which would turn `lemlist_create_lead`
        into a send path. They live in `lemlist_campaign_auto_review`, hidden by
        default — a switch that arms sending should take a deliberate gesture,
        not ride along in a settings dict.
        """
        client, is_platform = _client()
        settings = dict(settings or {})

        if op == "create":
            if not name:
                raise _bad("`name` required to create a campaign")
            _refuse_auto_review(settings)
            if settings:
                # `POST /campaigns` only takes name + timezone: accepting a
                # `settings` here would give a campaign that looks configured but
                # none of whose settings took. Refuse rather than silently
                # discard — and rather than create-then-update, which leaves a
                # half-configured campaign when the second call fails.
                raise _bad(
                    "`settings` does not apply at creation — the campaign "
                    "is born with `name` (+ `timezone`). Follow with "
                    'op="update" on the returned id.')
            result = client.create_campaign(name, timezone=timezone)
            # lemlist's response carries `state: running` and a `status` that says
            # "draft": an agent reading the latter thinks the campaign is stopped.
            # We say so rather than let it be inferred.
            if isinstance(result, dict):
                result = {**result, "warning": (
                    "Campaign created with state=running (its `status` shows "
                    "\"draft\" as long as it has neither step nor lead). Nothing goes out "
                    "until a lead is launched, but call "
                    "op=\"pause\" if you want to build it with the switch off.")}

        elif op == "update":
            if not campaign_id:
                raise _bad("`campaign_id` required")
            _refuse_auto_review(settings)
            if name is not None:
                settings["name"] = name
            if sender_user_ids is not None:
                settings["sendUserIds"] = sender_user_ids
            if not settings:
                raise _bad(
                    "nothing to update — pass `name`, `sender_user_ids` "
                    "and/or `settings`")
            result = client.update_campaign(campaign_id, settings)

        elif op == "pause":
            if not campaign_id:
                raise _bad("`campaign_id` required")
            result = client.pause_campaign(campaign_id)

        elif op == "duplicate":
            if not campaign_id:
                raise _bad("`campaign_id` required")
            result = client.duplicate_campaign(campaign_id, name=name)
            # Measured: the copy answers `status: "draft"` and `reports` shows
            # `state: running` — the same trap as `create`, said the same way.
            if isinstance(result, dict):
                result = {**result, "warning": (
                    "The copy is running (`op=\"reports\"` shows state=running; its "
                    "`status` shows \"draft\"). Call op=\"pause\" on its id to hold "
                    "it while you review it.")}

        elif op == "statutes":
            if not campaign_id:
                raise _bad("`campaign_id` required")
            result = client.get_campaign_statutes(campaign_id)

        elif op == "reports":
            if not campaign_ids:
                raise _bad("`campaign_ids` required (list of campaign ids)")
            result = {"reports": client.get_campaign_reports(campaign_ids)}

        elif op == "batch_stats":
            if not campaign_ids:
                raise _bad("`campaign_ids` required (list of campaign ids)")
            start, end = _default_window(start_date, end_date)
            result = client.get_batch_campaign_stats(
                campaign_ids, start_date=start, end_date=end, channels=channels,
                send_user=send_user, ab_selected=ab_selected)
            if not full:
                result = {**result, "results": [
                    _project_stats(r, full=False) for r in result.get("results", [])
                ]}

        elif op == "export_start":
            if not campaign_id:
                raise _bad("`campaign_id` required")
            result = client.start_campaign_export(campaign_id)

        elif op == "export_status":
            if not (campaign_id and export_id):
                raise _bad("`campaign_id` AND `export_id` required")
            result = client.get_campaign_export_status(campaign_id, export_id)

        elif op == "export_email":
            if not (campaign_id and export_id and email):
                raise _bad("`campaign_id`, `export_id` AND `email` required")
            result = client.set_campaign_export_email(campaign_id, export_id, email)

        elif op == "export_leads":
            if not campaign_id:
                raise _bad("`campaign_id` required")
            # `state`/`format` OMITTED when not requested: the client
            # has defaults that matter (`state="all"`, without which lemlist returns
            # an empty list that reads as "no leads"), and passing None would
            # override them. A client default does not survive an explicit None.
            exported = client.export_campaign_leads(campaign_id, **{
                k: v for k, v in (("state", state), ("format", format))
                if v is not None})
            result = exported if isinstance(exported, dict) else {"leads": exported}

        else:
            raise _bad(
                f'unknown op "{op}" — expected: create, update, pause, duplicate, '
                "statutes, reports, batch_stats, export_start, export_status, "
                "export_email, export_leads")

        return result

    @mcp.tool()
    def lemlist_campaign_start(campaign_id: str) -> dict:
        """Start (or resume) a campaign — lemlist begins sending.

        THE send gesture at campaign level: from here lemlist walks the sequence
        for every launched lead, to real people. A no-op if already running.

        Read `lemlist_campaign(op="statutes", …)` first — it names what would
        block or degrade the launch (missing sender, broken DNS, daily limit)
        with the same validation the UI runs.

        ⚠️ In practice this RESUMES a paused campaign: one created through
        `lemlist_campaign(op="create")` is already running, and lemlist answers
        `400 "You can't start campaigns that are already running"`.
        """
        client, is_platform = _client()
        result = client.start_campaign(campaign_id)
        return result

    @mcp.tool()
    def lemlist_sequence(
        op: Literal["get", "add_step", "update_step", "delete_step",
                    "ab_create", "ab_get", "ab_update", "ab_delete", "ab_winner"],
        campaign_id: Optional[str] = None,
        sequence_id: Optional[str] = None,
        step_id: Optional[str] = None,
        step: Optional[dict] = None,
        variant: Optional[str] = None,
    ) -> dict:
        """Read and edit the steps of a campaign sequence, and its A/B tests.

        A campaign owns a sequence (`seq_…`, returned by `create`) whose steps
        (`stp_…`) are the emails, LinkedIn actions and conditions it runs.

        Args by op:
        - `get`: `campaign_id`. Every sequence of the campaign with its steps —
          conditional steps branch into further sequences, so a campaign can
          hold several.
        - `add_step`: `sequence_id` + `step`. `step.type` is required, one of
          email, manual, phone, api, linkedinVisit, linkedinInvite,
          linkedinSend, linkedinVoiceNote, linkedinFollow, linkedinLikeLastPost,
          linkedinCommentLastPost, linkedinEndorse, linkedinWithdrawInvitation,
          sendToAnotherCampaign, conditional, whatsappMessage, sms. Common
          fields: `index` (insert position, appended when omitted), `delay`
          (days), `subject` + `message` (email), `title` (manual),
          `method` + `url` (api), `conditionKey` + `delayType` (conditional),
          `campaignId` (sendToAnotherCampaign).
        - `update_step`: `sequence_id` + `step_id` + `step`. `step.type` is
          required and must MATCH the existing step — it identifies the shape,
          it does not convert it. `images`/`videos` REPLACE what is there.
        - `delete_step`: `sequence_id` + `step_id`. Refused by lemlist while the
          campaign is running — pause it first.
        - `ab_create`: `sequence_id` + `step_id` (an EMAIL step). Creates
          variant B prefilled from A and STARTS the split. Email Pro plan.
        - `ab_get` / `ab_update` (`step` = the B fields: `subject`, `message`,
          `altMessage`, `cc`, `plainText`).
        - `ab_delete`: optional `variant` (default `B`). `A` promotes B to A.
        - `ab_winner`: `variant` (`A` or `B`) — the winner's template is then
          sent to every remaining lead.
        """
        client, is_platform = _client()

        if op == "get":
            if not campaign_id:
                raise _bad("`campaign_id` required")
            result = {"sequences": client.get_sequences(campaign_id)}
        else:
            if not sequence_id:
                raise _bad("`sequence_id` required (it comes from `lemlist_campaign` "
                           "op=create, or from op=\"get\" here)")
            if op == "add_step":
                if not step:
                    raise _bad("`step` required (at minimum `{\"type\": …}`)")
                result = client.add_step(sequence_id, step)
            elif op in ("update_step", "delete_step", "ab_create", "ab_get",
                        "ab_update", "ab_delete", "ab_winner"):
                if not step_id:
                    raise _bad("`step_id` required")
                if op == "update_step":
                    if not step:
                        raise _bad("`step` required (and `step.type` must match "
                                   "the existing type)")
                    result = client.update_step(sequence_id, step_id, step)
                elif op == "delete_step":
                    result = client.delete_step(sequence_id, step_id)
                elif op == "ab_create":
                    result = client.create_ab_variant(sequence_id, step_id)
                elif op == "ab_get":
                    result = client.get_ab_variant(sequence_id, step_id)
                elif op == "ab_update":
                    if not step:
                        raise _bad("`step` required (the fields of variant B)")
                    result = client.update_ab_variant(sequence_id, step_id, step)
                elif op == "ab_delete":
                    result = client.delete_ab_variant(
                        sequence_id, step_id, variant=variant or "B")
                else:  # ab_winner
                    if not variant:
                        raise _bad("`variant` required — 'A' or 'B'")
                    result = client.select_ab_winner(sequence_id, step_id, variant)
            else:
                raise _bad(
                    f'unknown op "{op}" — expected: get, add_step, update_step, '
                    "delete_step, ab_create, ab_get, ab_update, ab_delete, ab_winner")

        return result

    @mcp.tool()
    def lemlist_schedule(
        op: Literal["list", "get", "create", "update", "delete",
                    "for_campaign", "associate"],
        schedule_id: Optional[str] = None,
        campaign_id: Optional[str] = None,
        name: Optional[str] = None,
        timezone: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        weekdays: Optional[list[int]] = None,
        seconds_to_wait: Optional[int] = None,
        public: Optional[bool] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        page: Optional[int] = None,
        newest_first: bool = False,
    ) -> dict:
        """Manage sending windows (schedules) — days, hours, timezone, pacing.

        A schedule belongs to the TEAM, not to a campaign: several campaigns can
        share one, and a campaign can carry several. Creating a campaign
        auto-creates one and returns its id in `scheduleIds`.

        Args by op:
        - `list`: optional `limit`, `offset`/`page`, `newest_first` — the
          route is paginated, so a team with many windows needs them.
        - `get` / `delete`: `schedule_id`.
        - `create`: `name` (required) + `timezone` (IANA, default
          `Europe/Paris`), `start`/`end` (`HH:mm`, default 09:00-18:00),
          `weekdays` (1 = Monday … 7 = Sunday, default Mon-Fri),
          `seconds_to_wait` (pacing between two sends), `public` (offer it as a
          team template).
        - `update`: `schedule_id` + any of the same fields; only what is sent
          changes.
        - `for_campaign`: `campaign_id`. The schedules attached to a campaign.
        - `associate`: `campaign_id` + `schedule_id`. Attaches an existing
          window to a campaign.
        """
        client, is_platform = _client()

        if op == "list":
            result = client.list_schedules(
                limit=limit, offset=offset, page=page,
                sort_order="desc" if newest_first else None)
        elif op == "get":
            if not schedule_id:
                raise _bad("`schedule_id` required")
            result = client.get_schedule(schedule_id)
        elif op == "create":
            if not name:
                raise _bad("`name` required to create a schedule")
            kwargs = {k: v for k, v in {
                "timezone": timezone, "start": start, "end": end,
                "weekdays": weekdays, "seconds_to_wait": seconds_to_wait,
                "public": public,
            }.items() if v is not None}
            result = client.create_schedule(name, **kwargs)
        elif op == "update":
            if not schedule_id:
                raise _bad("`schedule_id` required")
            data = {k: v for k, v in {
                "name": name, "timezone": timezone, "start": start, "end": end,
                "weekdays": weekdays, "secondsToWait": seconds_to_wait,
                "public": public,
            }.items() if v is not None}
            if not data:
                raise _bad("nothing to update — pass at least one field")
            result = client.update_schedule(schedule_id, data)
        elif op == "delete":
            if not schedule_id:
                raise _bad("`schedule_id` required")
            result = client.delete_schedule(schedule_id)
        elif op == "for_campaign":
            if not campaign_id:
                raise _bad("`campaign_id` required")
            result = {"schedules": client.get_campaign_schedules(campaign_id)}
        elif op == "associate":
            if not (campaign_id and schedule_id):
                raise _bad("`campaign_id` ET `schedule_id` required")
            result = client.associate_schedule(campaign_id, schedule_id)
        else:
            raise _bad(
                f'unknown op "{op}" — expected: list, get, create, update, delete, '
                "for_campaign, associate")

        return result

    @mcp.tool()
    def lemlist_campaign_auto_review(
        campaign_id: str,
        enabled: bool,
        conditions: Optional[list[str]] = None,
    ) -> dict:
        """Arm (or disarm) auto-review on a campaign — leads then send on ADD.

        With auto-review on, a lead added to this campaign is launched
        immediately instead of waiting for manual review: `lemlist_create_lead`
        stops being a staging gesture and becomes a send. That is the whole
        reason this is its own tool, hidden by default, rather than a field in
        `lemlist_campaign(op="update")` — arming sending should be a deliberate
        act, not a key that rides along in a settings dict.

        Args:
            campaign_id: Campaign to arm or disarm.
            enabled: True arms it, False takes it back off.
            conditions: Restrict auto-launch to leads whose email verification
                is `deliverable`, `risky`, `undeliverable` or `unverified`.
                Narrowing to `["deliverable"]` is the cautious setting.
        """
        settings: dict = {"autoReview": enabled}
        if conditions is not None:
            settings["autoReviewConditions"] = conditions
        client, is_platform = _client()
        result = client.update_campaign(campaign_id, settings)
        return result

    @mcp.tool()
    def lemlist_lead(
        op: Literal["get", "list", "update", "delete", "unsubscribe",
                    "pause", "resume", "interested", "not_interested",
                    "vars_update", "vars_delete", "import_crm", "upload_audio"],
        campaign_id: Optional[str] = None,
        lead_id: Optional[str] = None,
        email: Optional[str] = None,
        fields: Optional[dict] = None,
        variables: Optional[dict] = None,
        variable_names: Optional[list[str]] = None,
        state: Optional[str] = None,
        limit: Optional[int] = None,
        crm: Optional[str] = None,
        user_id: Optional[str] = None,
        filter_id: Optional[str] = None,
        filter_type: Optional[str] = None,
        deduplicate: Optional[bool] = None,
        step_id: Optional[str] = None,
        audio: Optional[dict] = None,
    ) -> dict:
        """Lead lifecycle inside a campaign — read, edit, pause, qualify, remove.
        `oto_guide op=read slug="lemlist-playbook"`: build order, where each id comes from, and the doc↔API gaps.

        A LEAD is a person's copy inside ONE campaign (its sending state, its
        variables); the person themself is a contact (`lemlist_contact`).
        Creating a lead is `lemlist_create_lead`; releasing one held for review
        is `lemlist_launch_lead`.

        Args by op:
        - `get`: `lead_id` or `email`. `list`: `campaign_id` + optional `state`
          (`sent`, `replied`, `paused`…) and `limit`. ⚠️ `state` defaults to
          `"all"` HERE, which is NOT lemlist's own default: without it the API
          filters everything out and returns an empty list that reads as "no
          leads on this campaign". Pass a state only to narrow deliberately.
        - `update`: `campaign_id` + `lead_id` + `fields` (`firstName`,
          `lastName`, `companyName`, `jobTitle`, `preferredContactMethod`).
        - `delete`: `campaign_id` + `lead_id`/`email` — really removes it.
          `unsubscribe`: same arguments, but the lead STAYS on the campaign,
          marked unsubscribed. One lemlist route serves both, and its default is
          the soft one; here the two are named apart so neither is a surprise.
        - `pause`: `lead_id`; WITHOUT `campaign_id` it pauses the lead in EVERY
          campaign, not one. `resume`: `lead_id` — undoes a pause, so lemlist
          starts sending to that lead again (it does not skip a review; that is
          `lemlist_launch_lead`).
        - `interested` / `not_interested`: `lead_id` or `email`; with
          `campaign_id` it applies to that campaign, without it to all.
        - `vars_update`: `lead_id` + `variables`. `vars_delete`: `lead_id` +
          `variable_names` (the values are erased).
        - `import_crm`: `campaign_id` + `crm` + `user_id` + `filter_id`
          (from `lemlist_team(op="crm_filters")`), optional `filter_type`,
          `deduplicate`.
        - `upload_audio`: `lead_id` + `step_id` + `audio` — the audio of a
          `linkedinVoiceNote` step. `audio` is a source dict:
          `{"kind": "url", "url": …}`, `{"kind": "drive", "file_id": …}`,
          `{"kind": "gmail", "message_id": …, "filename": …}`,
          `{"kind": "project_file", "project_id": …, "file_id": …}` or a
          recording's `source` as read (`{"kind": "transit", "ref": …}`). The server
          fetches it (≤ 20 MB) and forwards the bytes.
        """
        client, is_platform = _client()
        target = lead_id or email

        if op == "get":
            if not target:
                raise _bad("`lead_id` or `email` required")
            result = (client.get_lead(lead_id=lead_id) if lead_id
                      else client.get_lead_by_email(email))

        elif op == "list":
            if not campaign_id:
                raise _bad("`campaign_id` required")
            # ⚠️ `state="all"` by DEFAULT — lemlist's default filters EVERYTHING out and returns
            # an empty list that reads as "no leads" (verified live on
            # 2026-08-31, see `export_campaign_leads` on the client side). The guide
            # `lemlist-playbook` announced this forcing as a given for the whole connector;
            # it only existed on the export route, and this route
            # therefore returned `[]` on a campaign that does contain leads
            # (signal 719). An explicit `state` remains master; there is NO
            # escape hatch to the raw — an undocumented magic value would be
            # the neighbouring default, and the raw is useful to no one: it filters everything out.
            result = {"leads": client.get_campaign_leads(
                campaign_id, state=(state or "all"), limit=limit)}

        elif op == "update":
            if not (campaign_id and lead_id and fields):
                raise _bad("`campaign_id`, `lead_id` AND `fields` required")
            result = client.update_lead(campaign_id, lead_id, fields)

        elif op in ("delete", "unsubscribe"):
            if not (campaign_id and target):
                raise _bad("`campaign_id` AND `lead_id`/`email` required")
            result = client.delete_lead(
                campaign_id, target, action="remove" if op == "delete" else None)

        elif op == "pause":
            if not lead_id:
                raise _bad("`lead_id` required")
            result = client.pause_lead(lead_id, campaign_id=campaign_id)

        elif op == "resume":
            if not lead_id:
                raise _bad("`lead_id` required")
            result = client.resume_lead(lead_id)

        elif op in ("interested", "not_interested"):
            if not target:
                raise _bad("`lead_id` or `email` required")
            mark = (client.mark_lead_interested if op == "interested"
                    else client.mark_lead_not_interested)
            result = mark(target, campaign_id=campaign_id)

        elif op == "vars_update":
            if not (lead_id and variables):
                raise _bad("`lead_id` AND `variables` required")
            result = client.update_lead_variables(lead_id, variables)

        elif op == "vars_delete":
            if not (lead_id and variable_names):
                raise _bad("`lead_id` AND `variable_names` required")
            result = client.delete_lead_variables(lead_id, variable_names)

        elif op == "import_crm":
            if not (campaign_id and crm and user_id and filter_id):
                raise _bad(
                    "`campaign_id`, `crm`, `user_id` AND `filter_id` required — "
                    'the filter comes from `lemlist_team(op="crm_filters")`')
            result = client.import_leads_from_crm(
                campaign_id, crm=crm, user_id=user_id, filter_id=filter_id,
                filter_type=filter_type, deduplicate=deduplicate)

        elif op == "upload_audio":
            if not (lead_id and step_id and audio):
                raise _bad(
                    "`lead_id`, `step_id` AND `audio` required — `audio` is a "
                    'source: {"kind": "url"|"drive"|"gmail"|"project_file"|"transit", …}')
            data, filename = _fetch_audio(audio)
            result = client.upload_lead_audio(
                lead_id, step_id, data, filename=filename)

        else:
            raise _bad(
                f'unknown op "{op}" — expected: get, list, update, delete, '
                "unsubscribe, pause, resume, interested, not_interested, "
                "vars_update, vars_delete, import_crm, upload_audio")

        return result
