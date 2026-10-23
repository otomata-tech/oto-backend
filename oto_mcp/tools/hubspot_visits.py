"""HubSpot — website visits, read-only: who came to the site, and what a contact read.

Fourth module of the connector (`hubspot` holds objects, lists and properties,
`hubspot_lignes` the push by reference, `hubspot_pipelines` the stages). It carries
`hubspot_visits`.

**Why a tool, when `hubspot_object` can already search.** Everything here but the
page views IS a company or contact search. But an agent asked "who visited the site
this week" had to know that the answer sits in `hs_most_recent_de_anonymized_visit`
(a name no label hints at), that the contact side lives in another property family,
and — the costly part — that an EMPTY answer means "nobody is tracked in this
portal", not "nobody came". A portal read on 2026-10-13 carried the whole Buyer
Intent schema and not one value: every search came back empty, and nothing said
why. `op="companies"` therefore answers its own emptiness (`diagnosis`).

**Three sources, never merged — they do not measure the same thing.**

1. `op="companies"` — companies HubSpot de-anonymised from their network (Buyer
   Intent). HubSpot writes the visit properties on the companies it TRACKS, and
   tracking is paid in HubSpot Credits. Company-level only: a last visit time and
   two 30-day counts. WHICH pages a company read is not a property — HubSpot shows
   it on the Buyer Intent card and nowhere in its API reference.
2. `op="contacts"` — people HubSpot already knows (a form, an email click, a
   cookie matched to a record) and their last session. Free on every plan, and
   blind to anonymous visitors.
3. `op="page_views"` — the pages ONE contact read, with their times, from the
   events API. HubSpot's reference asks for the `business-intelligence` scope and a
   Professional or Enterprise plan. ⚠️ On the one portal read (2026-10-14), a private
   app token HOLDING that scope was refused: HubSpot asked for `event-detail-read`,
   which is in no published scope list. Unproven to work with a private app.

⚠️ **Not exercised against a portal that tracks companies.** The property names
come from a portal's schema; the events parameters and response shape from HubSpot's
published OpenAPI spec (Events v3: `objectType`, `objectId`, `eventType`,
`occurredAfter`/`occurredBefore`, `limit`, `after`; `results` + `paging.next.after`).
That spec lists a `sort` parameter without its values: none is sent, events come in
HubSpot's order. The shapes in `tests/test_hubspot_visits.py` are built from these
sources. The first portal with tracking on is the first real reading — if it
disagrees with this module, the module is wrong.

The HubSpot client (oto-core) has no events method; we call its `_request`, as
`hubspot_pipelines` does, rather than tag oto-core for two GETs.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal, Optional

from fastmcp import FastMCP

from ..auth import hubspot as hubspot_auth
from .lecture import LECTURE

logger = logging.getLogger(__name__)

#: The property HubSpot fills with a tracked company's last de-anonymised visit —
#: what `op="companies"` filters and sorts on.
COMPANY_VISIT = "hs_most_recent_de_anonymized_visit"

#: Set to true on a company whose intent signals are tracked: the one fact that
#: tells "nobody came" from "nobody is tracked".
COMPANY_TRACKED = "hs_intent_signals_enabled"

#: Served for each company. Counts, never pages: see the module docstring.
COMPANY_PROPS = ("name", "domain", COMPANY_VISIT, "hs_intent_visitors_last_30_days",
                 "hs_intent_page_views_last_30_days", "hs_recent_intent_signals",
                 COMPANY_TRACKED)

#: A contact's last session — what `op="contacts"` filters and sorts on.
CONTACT_VISIT = "hs_analytics_last_visit_timestamp"

CONTACT_PROPS = ("email", "firstname", "lastname", "company", "associatedcompanyid",
                 CONTACT_VISIT, "hs_analytics_last_url", "hs_analytics_first_url",
                 "hs_analytics_num_visits", "hs_analytics_num_page_views",
                 "hs_analytics_source")

#: The events API names an object in the singular; the rest of the connector says
#: `contacts`. Anything else (an `objectTypeId` like `0-2`) is passed as given.
_EVENT_OBJECT = {"contacts": "contact", "companies": "company", "deals": "deal",
                 "tickets": "ticket"}

#: Where an event keeps the page's address, in the order we trust them.
_URL_KEYS = ("hs_url", "hs_canonical_url", "hs_base_url")

_NOT_TRACKED = (
    "no company has intent tracking on in this portal (`hs_intent_signals_enabled` "
    "is true on none), so HubSpot has written no visit on any company. Per HubSpot's "
    "documentation it fills these properties for the companies TRACKED in Buyer "
    "Intent: the tracking code on the site, then a saved view with \"Add new "
    "companies\" and \"Tracking intent signals\" turned on (or companies tracked by "
    "hand). Tracking is paid in HubSpot Credits. People HubSpot already knows are "
    "readable without it: op='contacts'.")


def _instant(value, name: str) -> Optional[datetime]:
    """`since` / `until` as a UTC instant. Accepts what a caller actually holds: an
    ISO 8601 string (a date alone is midnight UTC), or epoch seconds / milliseconds
    — HubSpot's own filters speak milliseconds, its events API speaks ISO, and the
    caller should not have to know which. Raises `ValueError` on anything else."""
    if value is None or value == "":
        return None
    text = str(value).strip()
    try:
        number = float(text)
    except ValueError:
        number = None
    refus = ValueError(
        f"{name}={value!r} is not a date: pass ISO 8601 (\"2026-10-06\" or "
        "\"2026-10-06T08:00:00Z\") or an epoch in milliseconds")
    if number is not None:
        # A bare number under 1e9 (before 2001 in seconds) is a year or a compact
        # date like 20261006, not an epoch: reading it as one would silently
        # open the window in 1970.
        if number < 1e9:
            raise refus
        # Below 1e11 it is seconds (until the year 5138), above it milliseconds.
        return datetime.fromtimestamp(number / 1000 if number >= 1e11 else number,
                                      tz=timezone.utc)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise refus from None
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None \
        else moment.astimezone(timezone.utc)


def _ms(moment: datetime) -> str:
    return str(int(moment.timestamp() * 1000))


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def visit_filters(prop: str, since: Optional[datetime],
                  until: Optional[datetime]) -> list[dict]:
    """The search filters of a visit window on `prop`. Without a window, "has a
    visit at all" — a filter is needed either way, or the search would return
    every record of the portal, visited or not."""
    if since is None and until is None:
        return [{"propertyName": prop, "operator": "HAS_PROPERTY"}]
    filters = []
    if since is not None:
        filters.append({"propertyName": prop, "operator": "GT", "value": _ms(since)})
    if until is not None:
        filters.append({"propertyName": prop, "operator": "LTE", "value": _ms(until)})
    return filters


def tracking_diagnosis(client, windowed: bool) -> dict:
    """Why `op="companies"` came back empty — ONE more search, only when it did.

    Counts the companies whose intent signals are tracked. Zero: the portal never
    turned tracking on, and no window will ever return anything. More: tracking
    works and nobody came (in this window). Never raises: a diagnosis that fails
    says so, the empty answer it decorates stays a valid answer."""
    try:
        rep = client.search_objects(
            "companies", properties=["name"], limit=1,
            filters=[{"propertyName": COMPANY_TRACKED, "operator": "EQ",
                      "value": "true"}]) or {}
    except Exception as e:  # noqa: BLE001 — degraded to a NAMED diagnosis below
        logger.warning("hubspot_visits: tracking diagnosis failed (%s)", type(e).__name__)
        return {"tracked_companies": None,
                "note": "could not count the tracked companies "
                        f"({type(e).__name__}): the empty result is not explained"}
    tracked = rep.get("total")
    if tracked is None:
        tracked = len(rep.get("results") or [])
    if not tracked:
        return {"tracked_companies": 0, "note": _NOT_TRACKED}
    return {"tracked_companies": tracked,
            "note": f"{tracked} company(ies) tracked, and none with a recorded visit"
                    + (" in this window" if windowed else "") + "."}


def compact_record(record: dict, wanted) -> dict:
    """One company or contact as a flat row: its id, the properties ASKED for that
    have a value, and its link in HubSpot.

    HubSpot nests the values under `properties`, adds three of its own to every
    search (`createdate`, `hs_lastmodifieddate`, `hs_object_id`) and serves the
    empty ones as null: on a page of a hundred visitors that is most of the
    weight, and none of the answer. `full` serves the record untouched."""
    props = record.get("properties") or {}
    row = {"id": record.get("id")}
    row.update({k: props[k] for k in wanted if props.get(k) not in (None, "")})
    if record.get("url"):
        row["url"] = record["url"]
    return row


def compact_event(event: dict) -> dict:
    """One event, reduced to when and where. `properties` (browser, country, UTM…)
    stays behind `full`: a contact's history is read as a list of pages."""
    props = event.get("properties") or {}
    row = {"occurredAt": event.get("occurredAt"),
           "url": next((props[k] for k in _URL_KEYS if props.get(k)), None),
           "path": props.get("hs_url_path"),
           "title": props.get("hs_page_title"),
           "id": event.get("id")}
    return {k: v for k, v in row.items() if v is not None}


# --- the tool ---------------------------------------------------------------------

def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.hubspot.client import HubSpotClient

    from ..mcp_errors import McpError
    from mcp.types import ErrorData, INVALID_PARAMS
    from .hubspot_scopes import translate

    def _bad(msg: str) -> McpError:
        return McpError(ErrorData(code=INVALID_PARAMS, message=msg))

    def _window(since, until) -> tuple[Optional[datetime], Optional[datetime]]:
        try:
            return _instant(since, "since"), _instant(until, "until")
        except ValueError as e:
            raise _bad(str(e)) from None

    def _names(value) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise _bad("properties = list of INTERNAL property names (list[str]), "
                       "added to the ones this op already returns")
        return value

    def _visited(c, object_type, prop, base, extra, since, until, limit, after,
                 full):
        """Records of `object_type` with a visit in the window, latest first."""
        wanted = list(base) + [p for p in extra if p not in base]
        page = c.search_objects(
            object_type, filters=visit_filters(prop, since, until),
            properties=wanted, limit=limit, after=after,
            sorts=[{"propertyName": prop, "direction": "DESCENDING"}]) or {}
        if full:
            return page
        return {**page, "results": [compact_record(r, wanted)
                                    for r in page.get("results") or []]}

    @mcp.tool(annotations=LECTURE)
    def hubspot_visits(
        op: Literal["companies", "contacts", "page_views",
                    "event_types"] = "companies",
        since: Optional[str] = None,
        until: Optional[str] = None,
        object_id: Optional[str] = None,
        object_type: str = "contacts",
        event_type: str = "e_visited_page",
        properties: Optional[list[str]] = None,
        limit: int = 50,
        after: Optional[str] = None,
        full: bool = False,
    ) -> dict:
        """HubSpot website visits (read-only) — which companies and contacts came
        to the site, latest first, and the pages a contact read.

        Three sources that do NOT measure the same thing:

        - **"companies"** (default) : companies HubSpot identified from their
          network (Buyer Intent), with the last visit time, visitors and page
          views over 30 days, and recent intent signals. ⚠️ HubSpot only fills
          these for the companies TRACKED in Buyer Intent — tracking is paid in
          HubSpot Credits. An empty answer carries a `diagnosis` saying whether
          any company is tracked at all. Counts only: WHICH pages a company read
          is not exposed by HubSpot.
        - **"contacts"** : people HubSpot already knows (form, email click,
          matched cookie) with their last session, last and first page, session
          and page-view counts. Free on every plan; anonymous visitors are not
          contacts and are not here.
        - **"page_views"** : the pages ONE record read, with their times.
          Requires `object_id`. ⚠️ HubSpot may refuse it to a private app
          token even with the `business-intelligence` scope its reference
          names (seen: it asked for `event-detail-read`, which no published
          scope list offers). Then the refusal says so, and op="contacts"
          still gives the first page, last page and last session.
        - **"event_types"** : the event types this portal defines — what
          `event_type` accepts.

        A recurring read ("new visitors since the last run") passes `since` =
        the previous run's time minus a margin: HubSpot's search index lags a
        few moments behind a write, and a visit landing in that gap would be
        skipped by an exact cursor. Deduplicate on the record id.

        Args:
            op: companies | contacts | page_views | event_types.
            since: only visits AFTER this instant — ISO 8601 ("2026-10-06" or
                "2026-10-06T08:00:00Z") or an epoch in milliseconds. Omitted:
                every record that has a visit (op="page_views": HubSpot's
                default window).
            until: only visits up to this instant, same formats.
            object_id: op="page_views" — the record id (a contact id from
                op="contacts").
            object_type: op="page_views" — contacts (default, the case HubSpot
                documents). Another type, or a raw objectTypeId, is passed to
                HubSpot as given.
            event_type: op="page_views" — the event to read, `e_visited_page`
                by default (names: op="event_types").
            properties: op="companies"/"contacts" — INTERNAL property names to
                return IN ADDITION to the visit ones (`hubspot_property`).
            limit: page size (HubSpot caps a search page at 100).
            after: pagination cursor from a previous response
                (paging.next.after). A search stops at 10 000 results: narrow
                the window rather than paging that far.
            full: HubSpot's raw records and events instead of the compact rows
                — companies and contacts: a flat row (id, the properties that
                have a value, url); page views: occurredAt, url, path, title,
                id.
        """
        key = hubspot_auth.bearer()
        c = HubSpotClient(api_key=key)
        extra = _names(properties)
        size = max(1, min(limit, 100))
        try:
            if op == "companies":
                debut, fin = _window(since, until)
                out = {"source": "buyer_intent", **_visited(
                    c, "companies", COMPANY_VISIT, COMPANY_PROPS, extra,
                    debut, fin, size, after, full)}
                # Only the FIRST page can be diagnosed: an empty page behind a
                # cursor is the end of a list, not a portal without tracking.
                if not out.get("results") and not after:
                    out["diagnosis"] = tracking_diagnosis(
                        c, windowed=bool(debut or fin))
                return out

            if op == "contacts":
                debut, fin = _window(since, until)
                return {"source": "known_contacts", **_visited(
                    c, "contacts", CONTACT_VISIT, CONTACT_PROPS, extra,
                    debut, fin, size, after, full)}

            if op == "page_views":
                if not object_id:
                    raise _bad("op='page_views' requires object_id (a contact id, "
                               "from op='contacts')")
                debut, fin = _window(since, until)
                otype = str(object_type or "").strip()
                params = {"objectType": _EVENT_OBJECT.get(otype.lower(), otype),
                          "objectId": object_id, "eventType": event_type,
                          "limit": size}
                if debut is not None:
                    params["occurredAfter"] = _iso(debut)
                if fin is not None:
                    params["occurredBefore"] = _iso(fin)
                if after:
                    params["after"] = after
                raw = c._request("GET", "/events/v3/events", params=params) or {}
                if full:
                    return raw
                out = {"object_type": otype, "object_id": object_id,
                       "event_type": event_type,
                       "results": [compact_event(e)
                                   for e in raw.get("results") or []]}
                if raw.get("paging"):
                    out["paging"] = raw["paging"]
                return out

            if op == "event_types":
                return c._request("GET", "/events/v3/events/event-types") or {}

            raise _bad("op must be 'companies', 'contacts', 'page_views' or "
                       "'event_types'")
        except UpstreamHTTPError as e:
            events = op in ("page_views", "event_types")
            refus = translate(
                e, object_id=object_id if events else None,
                object_type=(object_type if op == "page_views"
                             else None if events else op),
                family="events" if events else None,
                what="record" if events else "object")
            if refus is None:
                raise          # any other upstream refusal keeps its shape and its trace
            raise refus from None
