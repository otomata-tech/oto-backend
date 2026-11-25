"""Minari — phone prospecting: transcribed calls, lists, analytics.

Wraps `oto.tools.minari.client.MinariClient` (public v1 API, Bearer). The key
is created in **Settings → API & webhook** and carries the rights of the WHOLE
company — not of a single person. That is why the connector is **byo-only,
with no platform key**: a customer's call log is theirs, and a Minari key
shared across orgs would make no sense (same principle as `stripe`).

**Written from the contract, NOT verified live** (2026-08-31): everything comes from the published OpenAPI
3.1 and the vendor's LLM guide, with no probe against a real account. The
guards below are therefore readings of the contract, to be confirmed with the first
connected account — hence the `_verify` probe, which is the first real test.

**Six tools, one per business object** (ADR 0047), verb in `op=`. No parameter
is silently swallowed: an `op` that does not use a supplied argument REFUSES
(`_refuse_ignored` pattern, silae/granola/stripe).

**Three budgets, because three responses can blow up** — and that is the only
reason this module does more than relay:

1. ⚠️ **The call record embeds the full transcript.** `GET /calls/{id}`
   returns `CallDetail` = all of `CallSummary` PLUS every utterance of a call
   that can last 45 minutes. `op="get"` therefore REMOVES it and replaces it with
   `transcript_utterances` (the count); the text is obtained via `op="transcript"`,
   which is precisely the endpoint Minari split off for this reason. Nothing is
   lost silently: the removed key is named in the response.
2. ⚠️ **A list returns its 1500 contacts in one block**, with no pagination. `op="get"`
   on `minari_list` therefore stops at `max_contacts` (100 by default) and STATES the
   total and the truncation, instead of returning a wall of data.
3. `op="transcript"` caps at `max_utterances` (200 by default) and says so.

⚠️ **The connector's trap no. 1: `minari_list` only sees CSV lists.**
Minari's lists/contacts endpoints cover ONLY the CSV import source;
an account whose contacts come from HubSpot or Salesforce has perfectly real lists that these
endpoints never return. An empty `op="list"` therefore reads as "no CSV
list", NEVER "no list" — and the all-sources view is
`minari_analytics(op="lists")`. The empty response's message says so, because
this is exactly the case where an agent would wrongly conclude the account is empty.

Calls and analytics, on the other hand, cover all sources.

**This module invents nothing**: Minari exposes no call triggering, no
contact editing, no user management. What is missing here is missing from
the API — and the oto-core client does not carry these methods, so adding them
would take an oto-core PR, not a line here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from urllib.parse import parse_qs, urlparse

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, output_projection
from ..mcp_errors import McpError
from ..connectors import verify as connector_verify

# Rendering bounds — Minari fixes its pages server-side (50, and 10 for
# `analytics/lists`), but nothing bounds a list record or a transcript.
_DEFAULT_MAX_CONTACTS = 100
_DEFAULT_MAX_UTTERANCES = 200
# HARD ceilings: a good-faith `max_contacts=1500` would return up to
# 1500 contacts each carrying a 5,000-character note — several megabytes
# in the context. The ceiling is announced in the response when it bites,
# never applied silently.
_CEILING_MAX_CONTACTS = 500
_CEILING_MAX_UTTERANCES = 2000


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _refuse_ignored(op: str, hint: str, **provided) -> None:
    """A supplied argument that THIS op does not use is an error of intent.
    Otherwise `minari_call(op="list", call_id=…)` would return ALL calls while
    letting the caller believe one was targeted."""
    for name, value in provided.items():
        if value is not None:
            raise _bad(f"op={op!r} does not use `{name}` — {hint}")


def _upstream_message(e) -> str:
    """Translate Minari's refusal into something actionable.

    The error contract is `{"error": {"code", "message", "details"}}`; the upstream
    code is more telling than the status, so we keep it.
    """
    status = getattr(e, "status_code", None)
    raw = getattr(e, "body", None)
    # `raw` is not always JSON: a proxy 502 returns HTML, and reducing it
    # to `{}` would erase the only information available.
    body = raw if isinstance(raw, dict) else {}
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = err.get("code")
    detail = err.get("message") or body.get("detail") or ""
    reste = detail or (raw if raw not in (None, "", {}) else "")
    if status == 401:
        return ("Minari rejected the key (401"
                + (f" {code}" if code else "") + ") — it is missing, invalid "
                "or revoked. Recreate it in Minari → Settings → API & webhook, "
                "then set it again on the connector card.")
    if status == 404:
        return (f"Minari cannot find the target (404{' ' + code if code else ''}) — "
                f"check the identifier. {reste}".strip())
    if status == 409:
        return (f"Minari refuses: the identifier already exists (409"
                f"{' ' + code if code else ''}). {reste}".strip())
    if status == 429:
        return (f"Minari call limit reached (429) — 60 requests/minute for "
                f"the WHOLE company, so shared with the other automations "
                f"under the same key. {reste}".strip())
    if status == 400:
        return (f"Minari refuses the request (400{' ' + code if code else ''}): "
                f"{reste}").strip()
    return f"Minari HTTP {status}{' ' + code if code else ''}: {reste}".strip()


def _next_cursor(envelope: Any) -> Optional[str]:
    """The next-page cursor, extracted from `next_url`.

    Minari returns an absolute URL; passing it back as is would force the agent to
    parse it (or to send it back to us and us to validate it as an upstream URL).
    The cursor is the only part it cares about.
    """
    if not isinstance(envelope, dict):
        return None
    url = envelope.get("next_url")
    if not url:
        return None
    try:
        values = parse_qs(urlparse(url).query).get("cursor") or []
    except Exception:  # noqa: SILENT — an unreadable upstream `next_url` costs pagination, not the response: we return the page without a cursor rather than fail a call that succeeded
        return None
    return values[0] if values else None


def _with_note(payload: Any, texte: str) -> Any:
    """Attach an out-of-band remark under `note` (the house key), ACCUMULATING
    if another is already there — two remarks beat one overwritten."""
    if not isinstance(payload, dict):
        return payload
    ancienne = payload.get("note")
    return {**payload, "note": f"{ancienne} · {texte}" if ancienne else texte}


def _paged(envelope: Any) -> Any:
    """Add `next_cursor` to a paginated envelope, removing nothing.

    ⚠️ Minari's cursor is a POSITION, not a query: the contract's example
    decodes to `{"s": "<started_at>", "c": <call_id>}` — it carries
    no filter. The filters live in the query string of `next_url`.
    An agent replaying `cursor` ALONE would therefore receive the next page of the
    ENTIRE log, unfiltered, and merge it into a response it believes is
    filtered — wrong without any error. Hence the systematic remark: the
    only time it is read is when one is about to turn the page.
    """
    cursor = _next_cursor(envelope)
    if cursor and isinstance(envelope, dict):
        return _with_note(
            {**envelope, "next_cursor": cursor},
            "next page: pass `cursor` WITH the same filters — the "
            "cursor is a position, it does not carry them")
    return envelope


_ADRESSE_APPEL = ("id",)


def _choisis(page: Any, fields: Optional[List[str]]) -> Any:
    """Une page d'appels réduite aux clés que l'appelant nomme (signal #1953 : 50
    appels pesaient 44-46k caractères, au-delà de ce qu'un tour lit en ligne, et Minari
    ne laisse pas réduire la page). `id` reste toujours, pour rouvrir un appel ;
    l'enveloppe (`next_cursor`, la note de pagination) ne bouge pas, et la notice dit
    ce qui a été écarté et ce qui était demandé mais absent."""
    if not fields:
        return page
    voulus = list(dict.fromkeys(fields))
    if output_projection.RAW not in voulus:
        voulus += [k for k in _ADRESSE_APPEL if k not in voulus]
    out, notice = output_projection.select_fields(page, voulus)
    if notice is None:
        return out
    if notice.get("missing"):
        notice = {**notice, "missing": [k for k in notice["missing"]
                                        if k not in _ADRESSE_APPEL or k in fields]}
        if not notice["missing"]:
            notice.pop("missing")
    return {**out, "selection": notice} if isinstance(out, dict) else \
        {"result": out, "selection": notice}


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """"Test the connection" probe.

    `GET /users` rather than a call log: it is the lightest read
    (a handful of objects, no filter), it is served by the same company-level key
    as everything else, and a key that passes it can read the rest —
    Minari has no per-endpoint scopes.

    ⚠️ **A 200 is enough, even if the list is empty.** The probe runs
    BEFORE persistence (#106): anything it refuses is never
    saved. An earlier version raised on an empty directory, believing it read
    "key taken from the wrong workspace" — faulty reasoning (a key from
    another workspace returns THAT workspace's members, not an empty list) whose
    cost was real: it prevented SAVING a working key.
    A probe answers "does this key authenticate?", nothing more.
    """
    from oto.tools.minari.client import MinariClient
    MinariClient(api_key=fields["key"]).list_users()


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.minari.client import MinariClient

    connector_verify.register("minari", _verify)

    def _client() -> MinariClient:
        key, _ = access.resolve_api_key("minari")
        return MinariClient(api_key=key)

    def _run(fn):
        try:
            return fn()
        except ValueError as e:
            raise _bad(str(e))
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e))

    # ================================================================
    # Calls — the heart of the product
    # ================================================================

    @mcp.tool()
    def minari_call(
        op: Literal["list", "get", "transcript", "recording"] = "list",
        call_id: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        user_id: Optional[List[int]] = None,
        status: Optional[List[Literal["connected", "missed", "voicemail",
                                      "left-voicemail", "canceled", "busy",
                                      "failed", "no-answer",
                                      "meeting-booked"]]] = None,
        direction: Optional[Literal["incoming", "outgoing"]] = None,
        min_duration: Optional[int] = None,
        search: Optional[str] = None,
        transcript_search: Optional[str] = None,
        language: Optional[str] = None,
        contact_id: Optional[int] = None,
        list_id: Optional[List[str]] = None,
        cursor: Optional[str] = None,
        max_utterances: Optional[int] = None,
        fields: Optional[List[str]] = None,
    ) -> object:
        """Calls made and received in Minari, with AI summaries and objections.

        Covers ALL sources (CSV imports and CRM-synced contacts alike), unlike
        `minari_list`.

        "get" deliberately OMITS the transcript and reports
        `transcript_utterances` instead — a 45-minute call would otherwise bury
        the summary and objections you asked for. Use "transcript" for the text.

        Search two different ways: `search` matches the contact (name, company,
        phone number); `transcript_search` matches what was SAID. Transcript
        search is a literal substring match, words AND-ed, never semantic and
        never translated — so a French call will not match English terms. Each
        row carries `language`; re-issue in that language (or scope with
        `language="fr"`) to search across locales. Both need 3+ characters.

        "recording" returns availability and size, NOT the audio — an MP3 has no
        useful form in a tool result. For something a human can open, a call
        carries `public_call_link`, a shareable page needing no key — but it is
        null when the company has external call sharing switched off, so check
        it rather than promising someone a link.

        Args:
            op: "list" browses; "get" is one call without its transcript;
                "transcript" is the text; "recording" reports whether audio
                exists (it never returns audio bytes — see above).
            call_id: REQUIRED by "get", "transcript" and "recording".
            start_date: "list" only — ISO 8601, calls started at or after.
            end_date: "list" only — ISO 8601, calls started at or before.
            user_id: "list" only — filter by team member id(s), from
                `minari_user`.
            status: "list" only — one or more call statuses. "meeting-booked"
                is a FILTER-only value: it is how you ask for calls that led to
                a meeting, but a returned row never carries it as its `status`
                (rows use the other eight, plus a `meeting_booked` boolean).
            direction: "list" only — omit to get both directions.
            min_duration: "list" only — minimum duration in seconds.
            search: "list" only — contact name, company or phone (3+ chars).
            transcript_search: "list" only — what was said (3+ chars).
            language: "list" only — ISO 639-1 of the AI content, e.g. "fr".
            contact_id: "list" only — filter to one contact.
            list_id: "list" only — filter by list id(s).
            cursor: "list" only — `next_cursor` from a previous response.
                RE-SEND YOUR FILTERS WITH IT: the cursor is a position
                (`started_at` + call id), not a saved query, so a cursor on its
                own pages the whole journal and silently drops your filters.
                Pages are fixed at 50 by Minari: neither larger nor smaller.
            fields: "list" only — keep only these keys of each call (`id` is
                always kept, so a call can still be opened), e.g.
                `["started_at", "status", "duration", "contact", "summary"]`.
                A full page of 50 calls can pass 40k characters; scan with
                `fields`, then `op="get"` the calls worth reading. The envelope
                (`next_cursor`, `has_more`) stays; `selection` names what was
                left out and what was asked for but absent.
            max_utterances: "transcript" only — cap on returned lines
                (default 200). The response states the true total and whether
                it was truncated.
        """
        client = _client()

        if op == "list":
            _refuse_ignored(op, 'these arguments target ONE call — use op="get"',
                            call_id=call_id, max_utterances=max_utterances)
            return _run(lambda: _choisis(_paged(client.list_calls(
                start_date=start_date, end_date=end_date, user_id=user_id,
                status=status, direction=direction, min_duration=min_duration,
                search=search, transcript_search=transcript_search,
                language=language, contact_id=contact_id, list_id=list_id,
                cursor=cursor)), fields))

        _refuse_ignored(op, 'these filters only apply to op="list"', fields=fields,
                        start_date=start_date, end_date=end_date, user_id=user_id,
                        status=status, direction=direction, min_duration=min_duration,
                        search=search, transcript_search=transcript_search,
                        language=language, contact_id=contact_id, list_id=list_id,
                        cursor=cursor)
        if not call_id:
            raise _bad(f'op={op!r} requires `call_id`')

        if op == "get":
            _refuse_ignored(op, 'the cap only applies to op="transcript"',
                            max_utterances=max_utterances)

            def _get():
                out = client.get_call(call_id)
                data = out.get("data") if isinstance(out, dict) else None
                if not isinstance(data, dict):
                    return out
                lines = data.get("transcript")
                trimmed = {k: v for k, v in data.items() if k != "transcript"}
                if isinstance(lines, list) and lines:
                    trimmed["transcript_utterances"] = len(lines)
                    note = ('transcript removed from op="get" to stay within the response '
                            'budget — call op="transcript" with the same call_id')
                else:
                    # `null` ≠ zero utterances, and a silent call has nothing to
                    # fetch: announcing "transcript removed" would send the agent
                    # to spend a second call — on a 60/minute budget
                    # shared by the whole company — to receive nothing.
                    trimmed["transcript_utterances"] = 0
                    note = ("no transcript — call not connected or "
                            'transcription in progress; op="transcript" will return '
                            "nothing more")
                return {**out, "data": trimmed, "note": note}

            return _run(_get)

        if op == "transcript":
            demande = (_DEFAULT_MAX_UTTERANCES if max_utterances is None
                       else int(max_utterances))
            if demande < 1:
                raise _bad("`max_utterances` must be at least 1")
            cap = min(demande, _CEILING_MAX_UTTERANCES)

            def _transcript():
                out = client.get_call_transcript(call_id)
                data = out.get("data") if isinstance(out, dict) else None
                if not isinstance(data, dict):
                    return out
                lines = data.get("transcript")
                if not isinstance(lines, list):
                    # `null` = call did not complete or transcription in progress. This
                    # is not an error, and saying so avoids a pointless retry.
                    return {**out, "note": "no transcript — call not connected "
                                           "or transcription in progress"}
                total = len(lines)
                bloc = {**data, "transcript": lines[:cap],
                        "transcript_utterances": total, "truncated": total > cap}
                res = {**out, "data": bloc}
                if total > cap:
                    res["note"] = (f"{cap} utterances out of {total} — retry with a higher "
                                   "`max_utterances` if the rest matters")
                return res

            return _run(_transcript)

        _refuse_ignored(op, 'the cap only applies to op="transcript"',
                        max_utterances=max_utterances)
        return _run(lambda: client.call_recording_status(call_id))

    # ================================================================
    # Team — the id resolver for everything else
    # ================================================================

    @mcp.tool()
    def minari_user() -> object:
        """Active team members of the Minari company.

        Their `id` is what every `user_id` filter expects, and what
        `minari_list(op="create")` needs as `assigned_to`. Only members who
        accepted their invitation appear.
        """
        return _run(lambda: _client().list_users())

    # ================================================================
    # Contact lists — CSV source ONLY
    # ================================================================

    @mcp.tool()
    def minari_list(
        op: Literal["list", "get", "create", "delete"] = "list",
        list_id: Optional[str] = None,
        name: Optional[str] = None,
        assigned_to: Optional[int] = None,
        contacts: Optional[List[Dict[str, Any]]] = None,
        update_existing_contacts: Optional[bool] = None,
        cursor: Optional[str] = None,
        max_contacts: Optional[int] = None,
    ) -> object:
        """Contact lists — the call lists reps work through.

        SCOPE WARNING: these endpoints see the CSV-import source ONLY. A company
        whose contacts come from HubSpot or Salesforce has real lists that never
        appear here, so an empty result means "no CSV list", not "no list". The
        all-sources view is `minari_analytics(op="lists")`.

        "create" is how you push a prospecting list into the dialer: build the
        contacts elsewhere, assign the list to a rep, and it appears in their
        Minari. A contact already known to the account is ADDED to the list
        rather than duplicated; its stored fields stay untouched unless
        `update_existing_contacts=True` (a blank value never overwrites).

        A list holds at most 1500 contacts, and at most 1500 can be sent per
        request.

        "delete" is PERMANENT and takes the list's contacts out of the rep's
        queue. It cannot be undone from this tool or from Minari.

        Args:
            op: "list" browses lists; "get" is one list with its contacts;
                "create" makes a list and imports contacts; "delete" destroys
                a list permanently.
            list_id: REQUIRED by "get" and "delete".
            name: REQUIRED by "create" — the list name (255 chars max).
            assigned_to: REQUIRED by "create" — the team member id that will own
                the list, from `minari_user`.
            contacts: REQUIRED by "create" — up to 1500 objects. Each needs at
                least one of `firstName`, `lastName`, `email`. Also accepts
                `company`, `title`, `companyDomain`, `linkedinUrl`,
                `description`, `phoneNumber1`…`phoneNumber5`, `note` (5000
                chars, attached as a note), and `customFields` keyed by ids
                registered through `minari_custom_field`.
            update_existing_contacts: "create" only — overwrite stored fields of
                contacts that already exist. Defaults to false.
            cursor: "list" only — `next_cursor` from a previous response; it
                is a position, not a saved query.
            max_contacts: "get" only — cap on contacts returned (default 100).
                A list returns all 1500 at once otherwise; the response states
                the true total and whether it was truncated.
        """
        client = _client()

        if op == "list":
            _refuse_ignored(op, 'these arguments target ONE list or its creation',
                            list_id=list_id, name=name, assigned_to=assigned_to,
                            contacts=contacts,
                            update_existing_contacts=update_existing_contacts,
                            max_contacts=max_contacts)

            def _browse():
                out = _paged(client.list_lists(cursor=cursor))
                rows = out.get("data") if isinstance(out, dict) else None
                # On the FIRST page only (the cursor says we are continuing),
                # and whatever the number of rows: the note does not explain an
                # empty result, it states a SCOPE. The partial case — a few CSV
                # lists next to many CRM lists — under-reports just as much,
                # and nothing signals it.
                if isinstance(rows, list) and not cursor:
                    return _with_note(
                        out,
                        "these endpoints only see lists from a CSV "
                        "import; lists synced from a CRM do not appear "
                        "here, not even partially. All-sources "
                        'view: minari_analytics(op="lists").')
                return out

            return _run(_browse)

        _refuse_ignored(op, 'pagination only applies to op="list"', cursor=cursor)

        if op == "get":
            _refuse_ignored(op, 'these arguments only apply to op="create"',
                            name=name, assigned_to=assigned_to, contacts=contacts,
                            update_existing_contacts=update_existing_contacts)
            if not list_id:
                raise _bad('op="get" requires `list_id`')
            demande = _DEFAULT_MAX_CONTACTS if max_contacts is None else int(max_contacts)
            if demande < 1:
                raise _bad("`max_contacts` must be at least 1")
            cap = min(demande, _CEILING_MAX_CONTACTS)

            def _one():
                out = client.get_list(list_id)
                data = out.get("data") if isinstance(out, dict) else None
                if not isinstance(data, dict):
                    return out
                rows = data.get("contacts")
                if not isinstance(rows, list):
                    return out
                total = len(rows)
                bloc = {**data, "contacts": rows[:cap], "total_contacts": total,
                        "truncated": total > cap}
                res = {**out, "data": bloc}
                if demande > cap:
                    res["note"] = (
                        f"`max_contacts={demande}` reduced to {cap} — a contact "
                        "note weighs up to 5,000 characters")
                elif total > cap:
                    res["note"] = f"{cap} contacts out of {total}"
                return res

            return _run(_one)

        if op == "create":
            _refuse_ignored(op, 'op="create" creates the list, it does not target an existing list',
                            list_id=list_id, max_contacts=max_contacts)
            if not name:
                raise _bad('op="create" requires `name`')
            if assigned_to is None:
                raise _bad('op="create" requires `assigned_to` — a member id, '
                           "returned by minari_user")
            if not contacts:
                raise _bad('op="create" requires `contacts` (at least one)')
            return _run(lambda: client.create_list(
                name=name, assigned_to=assigned_to, contacts=contacts,
                update_existing_contacts=bool(update_existing_contacts)))

        _refuse_ignored(op, 'op="delete" only takes `list_id`',
                        name=name, assigned_to=assigned_to, contacts=contacts,
                        update_existing_contacts=update_existing_contacts,
                        max_contacts=max_contacts)
        if not list_id:
            raise _bad('op="delete" requires `list_id`')
        return _run(lambda: client.delete_list(list_id))

    # ================================================================
    # Contacts of a list
    # ================================================================

    @mcp.tool()
    def minari_contact(
        op: Literal["add", "remove"],
        list_id: str,
        contacts: Optional[List[Dict[str, Any]]] = None,
        contact_ids: Optional[List[int]] = None,
        update_existing_contacts: Optional[bool] = None,
    ) -> object:
        """Add contacts to, or remove them from, an existing Minari list.

        Same CSV-only scope as `minari_list`.

        "add" tops up a list a rep is already working. Up to 1500 per request,
        and a list caps at 1500 total: contacts beyond the cap are SILENTLY
        skipped and counted in `skippedCount` — the request still succeeds, so
        read that number rather than assuming everything landed.

        "remove" takes contacts out of the list by contact id (the `contactId`
        of `minari_list(op="get")`). Minari refuses to empty a list this way —
        that is `minari_list(op="delete")`.

        Args:
            op: "add" or "remove".
            list_id: the list to modify.
            contacts: REQUIRED by "add" — same shape as
                `minari_list(op="create")`.
            contact_ids: REQUIRED by "remove" — ids to take out of the list.
            update_existing_contacts: "add" only — overwrite stored fields of
                contacts that already exist. Defaults to false.
        """
        client = _client()
        if op == "add":
            _refuse_ignored(op, 'op="add" takes contacts, not ids',
                            contact_ids=contact_ids)
            if not contacts:
                raise _bad('op="add" requires `contacts` (at least one)')
            return _run(lambda: client.add_contacts(
                list_id, contacts,
                update_existing_contacts=bool(update_existing_contacts)))

        _refuse_ignored(op, 'op="remove" takes ids, not contacts',
                        contacts=contacts,
                        update_existing_contacts=update_existing_contacts)
        if not contact_ids:
            raise _bad('op="remove" requires `contact_ids`')
        return _run(lambda: client.remove_contacts(list_id, contact_ids))

    # ================================================================
    # Custom fields
    # ================================================================

    @mcp.tool()
    def minari_custom_field(
        op: Literal["list", "create", "delete"] = "list",
        field_id: Optional[str] = None,
        label: Optional[str] = None,
    ) -> object:
        """Custom contact fields — the metadata keys an import may carry.

        A key must be registered here BEFORE it can be used in the
        `customFields` of an imported contact. The contract states the
        requirement but not how a breach fails (rejected request? key dropped?
        contact skipped?) — so register first rather than relying on the error.

        "delete" removes the field from future imports and from the UI.

        Args:
            op: "list" shows registered fields; "create" registers one;
                "delete" removes one.
            field_id: REQUIRED by "create" and "delete" — the stable key used
                inside `customFields`, e.g. "industry". Must be unique.
            label: REQUIRED by "create" — the display name, e.g. "Industry".
        """
        client = _client()
        if op == "list":
            _refuse_ignored(op, 'op="list" takes no arguments',
                            field_id=field_id, label=label)
            return _run(client.list_custom_fields)
        if op == "create":
            if not field_id:
                raise _bad('op="create" requires `field_id`')
            if not label:
                raise _bad('op="create" requires `label`')
            return _run(lambda: client.create_custom_field(field_id=field_id, label=label))
        _refuse_ignored(op, 'op="delete" only takes `field_id`', label=label)
        if not field_id:
            raise _bad('op="delete" requires `field_id`')
        return _run(lambda: client.delete_custom_field(field_id))

    # ================================================================
    # Analytics — answering without downloading the calls
    # ================================================================

    @mcp.tool()
    def minari_analytics(
        op: Literal["overview", "users", "objections", "lists"] = "overview",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        user_id: Optional[List[int]] = None,
        list_id: Optional[List[str]] = None,
        conversation_threshold: Optional[Literal[0, 30, 60, 90, 120]] = None,
        period: Optional[Literal["day", "week", "month", "all"]] = None,
        call_limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> object:
        """Aggregated calling metrics — answer "what's our connect rate" with
        one call instead of downloading and summing calls yourself.

        DEFAULT WINDOWS DIFFER, and getting this wrong silently returns the
        wrong period: "overview" and "users" default to TODAY; "objections"
        defaults to the LAST 7 DAYS. Always pass `start_date` and `end_date`
        together when you mean a specific range. Analytics are day-granular,
        and the resolved window comes back in `period` next to the data.

        Rates are percentages (0-100) and are null when their denominator is 0.
        The per-user rows sum to the overview totals for the same filters.

        "lists" is the all-sources view of list progress (CSV, HubSpot, …) —
        use it, not `minari_list`, to answer "which lists are stalled".

        Args:
            op: "overview" is the company total; "users" breaks the same
                metrics down per rep (rank by connect rate); "objections" is
                what prospects push back on and how well reps handle it;
                "lists" is per-list completion and health.
            start_date: "overview", "users" and "objections" only — ISO 8601,
                sent together with `end_date`. "lists" has NO date window; its
                window is `period`.
            end_date: "overview", "users" and "objections" only — ISO 8601, sent
                together with `start_date`.
            user_id: restrict to team member id(s), from `minari_user`.
            list_id: restrict to calls made within these list id(s).
            conversation_threshold: "overview" and "users" only — seconds a
                connected call must last to count as a conversation. One of
                0, 30, 60, 90, 120 (default 30).
            period: REQUIRED by "lists" — the dial window counted:
                "day", "week", "month" or "all".
            call_limit: REQUIRED by "lists" — 1 to 10, the attempts after which
                an unconnected contact counts as completed. With `period` it
                DEFINES what "completed" means, so two different values are two
                different questions, not a contradiction.
            cursor: "lists" only — `next_cursor` from a previous response.
                Re-send `period`, `call_limit` and any filters with it — the
                cursor is a position, not a saved query. Pages are 10, not 50.
        """
        client = _client()
        if (start_date is None) != (end_date is None):
            raise _bad("`start_date` and `end_date` go together — Minari refuses "
                       "one without the other.")

        if op == "lists":
            _refuse_ignored(op, 'the conversation threshold only applies to '
                                'op="overview"/"users"',
                            conversation_threshold=conversation_threshold)
            # `analytics/lists` has NO date window: its own is
            # `period`. Silently accepting them would return a "since January"
            # computed over the week, with nothing to signal it.
            _refuse_ignored(op, 'op="lists" has no date window — its '
                                "own is `period` (day/week/month/all)",
                            start_date=start_date, end_date=end_date)
            if not period:
                raise _bad('op="lists" requires `period` — it defines the window '
                           "for counting calls (day/week/month/all)")
            if call_limit is None:
                raise _bad('op="lists" requires `call_limit` (1-10) — it defines '
                           "after how many attempts a never-reached contact "
                           "counts as exhausted")
            return _run(lambda: _paged(client.analytics_lists(
                period=period, call_limit=call_limit, user_id=user_id,
                list_id=list_id, cursor=cursor)))

        _refuse_ignored(op, 'these arguments only apply to op="lists"',
                        period=period, call_limit=call_limit, cursor=cursor)

        if op == "objections":
            _refuse_ignored(op, 'the conversation threshold only applies to '
                                'op="overview"/"users"',
                            conversation_threshold=conversation_threshold)
            return _run(lambda: client.analytics_objections(
                start_date=start_date, end_date=end_date, user_id=user_id,
                list_id=list_id))

        fn = client.analytics_overview if op == "overview" else client.analytics_users
        return _run(lambda: fn(
            start_date=start_date, end_date=end_date, user_id=user_id,
            list_id=list_id, conversation_threshold=conversation_threshold))
