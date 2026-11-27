"""Twilio — the org's own Twilio account: phone numbers, bridged calls, SMS.

Wraps `oto.tools.twilio.TwilioClient` (REST v2010, Basic `account_sid:auth_token`),
credential resolved per call via `access.resolve_credential_fields("twilio")`
(ADR 0011). Own key of the org only (`byo_org`): Twilio bills numbers, minutes and
messages to that account.

**Surface** — the `<ns>` / `<ns>_act` pair (`tests/test_convention_act.py`):
- `twilio` (reads): the account, owned numbers, numbers available to buy, calls,
  recordings, messages, and the AUDIO of a recording put in transit
  (`op="recording_audio"`): a short `source` `{kind: "transit", ref}` for
  `transcription_create`, never the media URL (it needs the credentials);
- `twilio_act` (effects): `add_number` / `remove_number` (org admin only:
  money and an irreversible loss), `update_number`, `call` (bridged: the rep's phone
  rings first, then Twilio dials the contact and records both sides), `cancel_call`,
  `send_sms` (one recipient or a batch). `dry_run` on every op: identical
  validation, no call to Twilio.

**No content rule is enforced**: an SMS can be a reminder, a notification or a
message to a friend. Nothing here adds an opt-out mention or holds a message to
business hours; what Twilio or the carriers refuse comes back as their refusal.

**Two ways to reach Twilio, one per org** (decision of 26/11/2026): the org's OWN key,
called directly; or, without one, the org's PLATFORM numbers, bought on the platform's Twilio
account and billed in credits, reached through the commerce's phone relay
(`tools/twilio_courtier.py`) — this server never holds the platform's Twilio keys. On
the platform's numbers an agent reads numbers, SMS threads and the call log, sends SMS and places
bridged calls; buying, releasing and recordings stay on the Phone page.

Webhooks on the org's own account (inbound calls, voicemail, row write-back) are not wired
yet: Twilio logs inbound SMS and recordings on the account, read back here.

Calls to the client are written out in full (`_client().list_calls(…)`): this is what
makes them verifiable by the version-skew probe (`test_tools_client_methods_exist`).
"""
from __future__ import annotations

import time
from typing import Any, List, Literal, Optional

import requests

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, transit
from ..access.resolve import CredentialUnavailable
from . import twilio_courtier
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError
from .lecture import ACTE, LECTURE

_NAME = "twilio"

#: Recipients one `send_sms` call may carry: one POST each, well inside the invoke timeout.
SMS_BATCH_MAX = 50
#: Seconds a batch may spend sending before it stops and hands back the rest: the
#: invoke path times out at 45 s, and a timeout the caller retries would send twice.
SMS_TIME_BUDGET_S = 30.0

#: Keys dropped from every read by default: API links and sub-resource maps that repeat
#: the SID, never what an agent chooses on.
_DROP = frozenset({"uri", "subresource_uris", "links", "url"})


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _slim(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _slim(v) for k, v in value.items() if k not in _DROP}
    if isinstance(value, list):
        return [_slim(v) for v in value]
    return value


def _view(payload: Any, full: bool) -> Any:
    """Tightened view (API links dropped) unless `full`; `next_page_uri` kept."""
    if full or not isinstance(payload, dict):
        return payload
    out = _slim(payload)
    if "next_page_uri" in payload:
        out["next_page_uri"] = payload["next_page_uri"]
    return out


def _refuse_ignored(op: str, **provided) -> None:
    """An argument supplied that THIS op does not use is an intent error."""
    for name, value in provided.items():
        if value is not None and value is not False and value != []:
            raise _bad(f"op='{op}' does not use `{name}`.")


def _need(value, name: str, op: str):
    if value is None or value == "" or value == []:
        raise _bad(f"op='{op}' requires `{name}`.")
    return value


def _upstream_message(e) -> str:
    status = e.status_code
    body = e.body if isinstance(e.body, dict) else {}
    code, message = body.get("code"), body.get("message")
    detail = f" Twilio error {code}: {message}" if code or message else ""
    if status in (401, 403):
        return (f"Twilio refused access (HTTP {status}): the Account SID / Auth Token pair "
                "is invalid, or the account is suspended." + detail)
    if status == 404:
        return "Twilio: not found (404)." + detail
    if status == 429:
        return "Twilio: rate limit reached (429); retry in a moment." + detail
    if status >= 500:
        return f"Twilio is temporarily unavailable (HTTP {status}); retry later."
    return f"Twilio refused the request (HTTP {status})." + (detail or f" {str(e.body)[:400]}")


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """Probe for "test the connection": `GET /Accounts/{sid}.json`, free. A trial
    account is accepted, and says so: it only calls and texts verified numbers."""
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.twilio import TwilioClient

    try:
        TwilioClient(account_sid=fields.get("account_sid"),
                     auth_token=fields.get("auth_token")).get_account()
    except UpstreamHTTPError as e:
        if e.status_code in (401, 403):
            raise connector_verify.NonAutorise(f"Twilio HTTP {e.status_code}: {e.body}")
        raise RuntimeError(f"Twilio HTTP {e.status_code}: {e.body}")
    except ValueError as e:
        raise connector_verify.NonAutorise(str(e))


def _caller_is_org_admin() -> bool:
    from .. import roles
    from ..auth.hooks import current_user_sub_from_token
    sub = current_user_sub_from_token()
    org_id = access.current_org(sub) if sub else None
    return bool(sub and org_id) and roles.is_org_admin(sub, int(org_id))


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.twilio import TwilioClient, bridge_twiml, e164

    connector_verify.register(_NAME, _verify)

    def _client() -> TwilioClient:
        creds = access.resolve_credential_fields(_NAME)
        return TwilioClient(account_sid=creds.get("account_sid"),
                            auth_token=creds.get("auth_token"))

    def _courtier_ou_none():
        """None when the org has its OWN Twilio key (the direct path), else the broker
        for the caller, reached with the PLATFORM key (the relay credential, see
        `providers/twilio.py`); refused when neither resolves — no platform key, or the
        org's access to it cut (out of credits)."""
        from ..access import resolve as _resolve
        try:
            # A probe: its failure is the normal path to the broker, not a broken
            # connector — no monitoring event (`emit_on_failure=False`).
            _resolve.resolve_credential(_NAME, want="byo", emit_on_failure=False)
            return None
        except CredentialUnavailable:
            pass
        try:
            rc = _resolve.resolve_credential(_NAME, want="auto")
        except CredentialUnavailable as e:
            raise _bad("No Twilio account is connected for this organization and the "
                       "platform's numbers are not available to it (paused, or out of "
                       f"credits): {e}") from None
        token = (rc.fields or {}).get("auth_token") if rc.is_platform else None
        courtier = twilio_courtier.pour_l_appelant(token) if token else None
        if courtier is None:
            raise _bad("No Twilio account is connected for this organization, and "
                       "the platform's numbers are not reachable from here: connect the "
                       "org's Twilio account, or buy a number in Connectors → Twilio.")
        return courtier

    def _cr(fn):
        try:
            return fn()
        except twilio_courtier.CourtierRefus as e:
            raise _bad(twilio_courtier.message_de(e)) from None
        except requests.RequestException as e:
            raise _bad(f"The platform's phone is unreachable right now ({type(e).__name__}); "
                       "retry in a moment.") from None

    def _numero_plateforme(courtier, from_: str) -> dict:
        numbers = _cr(lambda: courtier.get()).get("numbers") or []
        for n in numbers:
            if n.get("e164") == from_:
                return n
        owned = ", ".join(n.get("e164") or "" for n in numbers) or "none"
        raise _bad(f"`from_` must be one of the organization's numbers bought from the platform ({owned}).")

    def _read_courtier(courtier, op, sid, to, from_, **ignored):
        if op not in ("numbers", "messages", "calls"):
            raise _bad(f"op='{op}' is not available on the organization's numbers bought from the platform: "
                       "agents read `numbers`, `messages` and `calls`; buying, releasing and "
                       "recordings are on the Phone page.")
        if sid:
            raise _bad("On the platform's numbers, `sid` is not used: filter `messages` with `to` or "
                       "`from_`.")
        if op == "numbers":
            return _cr(lambda: courtier.get())
        if op == "calls":
            return _cr(lambda: courtier.get("/calls"))
        peer = to or from_
        return _cr(lambda: courtier.get("/messages", {"peer": peer} if peer else None))

    def _act_courtier(courtier, op, *, rep, to, from_, body, media_urls, dry_run):
        if op not in ("send_sms", "call"):
            raise _bad(f"op='{op}' is not available on the organization's numbers bought from the platform: "
                       "an agent sends SMS and places calls; numbers are bought and released "
                       "on the Phone page.")
        if media_urls:
            raise _bad("MMS (`media_urls`) is not available on the platform's numbers.")
        sender = _need(from_, "from_", op)
        # A NAME (no leading +) is an alphanumeric sender: SMS only, send-only.
        if op == "send_sms" and not sender.strip().startswith("+"):
            number = None
        else:
            number = _numero_plateforme(courtier, sender)
        recipients = [_run(lambda r=r: e164(r, "to")) for r in _need(to, "to", op)]
        if op == "call":
            first = _run(lambda: e164(_need(rep, "rep", op), "rep"))
            if len(recipients) != 1:
                raise _bad("op='call' dials ONE contact: pass `to` as a single-number list.")
            if dry_run:
                return {"dry_run": True, "would": "call", "rings_first": first,
                        "then_dials": recipients[0], "caller_id": from_, "via": "platform"}
            return _cr(lambda: courtier.post("/calls", {"number_id": number["id"],
                                                        "rep": first, "to": recipients[0]}))
        if not body:
            raise _bad("op='send_sms' requires `body` on the platform's numbers.")
        if len(recipients) > SMS_BATCH_MAX:
            raise _bad(f"op='send_sms': at most {SMS_BATCH_MAX} recipients per call.")
        if dry_run:
            return {"dry_run": True, "would": "send", "from": from_, "to": recipients,
                    "characters": len(body), "via": "platform"}
        sent, failed, not_attempted = [], [], []
        started = time.monotonic()
        for index, number_to in enumerate(recipients):
            if time.monotonic() - started > SMS_TIME_BUDGET_S:
                not_attempted = [{"index": i, "to": recipients[i]}
                                 for i in range(index, len(recipients))]
                break
            try:
                origin = ({"number_id": number["id"]} if number is not None
                          else {"sender_id": sender.strip()})
                m = courtier.post("/messages", {**origin, "to": number_to, "body": body})
                sent.append({"index": index, "to": number_to, "id": m.get("id"),
                             "status": m.get("status"), "segments": m.get("segments")})
            except twilio_courtier.CourtierRefus as e:
                if e.status in (401, 402, 403, 503):
                    raise _bad(twilio_courtier.message_de(e)) from None
                failed.append({"index": index, "to": number_to,
                               "error": twilio_courtier.message_de(e)})
            except requests.RequestException as e:
                failed.append({"index": index, "to": number_to,
                               "error": f"unreachable ({type(e).__name__})"})
        out = {"total": len(recipients), "sent": sent, "failed": failed, "via": "platform"}
        if not_attempted:
            out["not_attempted"] = not_attempted
            out["warning"] = (f"Stopped after {SMS_TIME_BUDGET_S:.0f} s to stay inside the "
                              "call timeout: send `not_attempted` again in a new call.")
        return out

    def _run(fn):
        try:
            return fn()
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e))
        except ValueError as e:
            raise _bad(str(e))

    def _audio(recording_sid: str) -> dict:
        """The recording's mp3, downloaded with the account's credentials and put in
        transit under the caller (kept a day)."""
        data = _run(lambda: _client().recording_media(recording_sid))
        try:
            return transit.deposer("twilio-recordings", data, "audio/mpeg",
                                   f"{recording_sid}.mp3")
        except transit.TransitError as e:
            raise _bad(f"Twilio recording: {e}") from None

    @mcp.tool(annotations=LECTURE)
    def twilio(
        op: Literal["account", "numbers", "number", "available_numbers", "calls",
                    "recordings", "recording_audio", "messages", "message",
                    "next_page"] = "numbers",
        sid: Optional[str] = None,
        country: Optional[str] = None,
        number_type: Optional[Literal["local", "mobile", "toll_free", "national",
                                      "shared_cost", "voip", "machine_to_machine"]] = None,
        contains: Optional[str] = None,
        sms_enabled: Optional[bool] = None,
        voice_enabled: Optional[bool] = None,
        to: Optional[str] = None,
        from_: Optional[str] = None,
        status: Optional[str] = None,
        date_after: Optional[str] = None,
        date_before: Optional[str] = None,
        next_page_uri: Optional[str] = None,
        page_size: Optional[int] = None,
        full: bool = False,
    ) -> Any:
        """Read the organization's Twilio account: numbers, calls, recordings, SMS.

        `op`:
        - `account` — name, `status`, `type` (Trial or Full: a trial account only
          calls and texts verified numbers) and the balance.
        - `numbers` — numbers the account owns, with capabilities (voice, SMS)
          and the webhook URLs they call. `number` — one, by `sid` (PN…).
        - `available_numbers` — numbers you can buy in `country` (ISO code, FR)
          of `number_type` (local, mobile, toll_free, national…), optionally
          `contains` digits, `sms_enabled`, `voice_enabled`. Each says its
          `address_requirements`; many countries need an approved regulatory
          bundle before purchase. Free, nothing is reserved.
        - `calls` — calls, most recent first, filtered by `to`, `from_`,
          `status`, `date_after` / `date_before` (YYYY-MM-DD, UTC); with `sid`
          (CA…), that one call: status, duration, price.
        - `recordings` — recordings of the account, or of one call (`sid` = CA…).
        - `recording_audio` — the audio of a recording (`sid` = RE…), kept a
          day: pass the returned `source` as is to `transcription_create`.
        - `messages` — SMS both ways, most recent first, filtered by `to`,
          `from_`, `date_after` / `date_before`. A thread with someone is
          `to=<them>` plus `from_=<them>`. `message` — one by `sid` (SM…/MM…).
        - `next_page` — the next page of a list, from its `next_page_uri`.

        Numbers are E.164 (+33612345678). Lists drop API links by default;
        `full=true` returns raw records.

        Without its own Twilio account connected, the organization's PLATFORM
        numbers are read instead (bought on the Phone page, billed in
        credits): only `numbers`, `messages` (filtered by `to` or `from_`) and
        `calls` are served there.

        Args:
            op: account | numbers | number | available_numbers | calls |
                recordings | recording_audio | messages | message | next_page.
            sid: number (PN…), call (CA…), recording (RE…) or message (SM…) SID.
            country: op='available_numbers' — ISO 3166 alpha-2 (FR, IT, GB, US).
            number_type: op='available_numbers' — default local.
            contains: op='available_numbers' — digits the number contains.
            sms_enabled: op='available_numbers' — only numbers that can text.
            voice_enabled: op='available_numbers' — only numbers that can call.
            to: op='calls'/'messages' — filter on the recipient number.
            from_: op='calls'/'messages' — filter on the sender number.
            status: op='calls' — queued, ringing, in-progress, completed, busy,
                no-answer, failed, canceled.
            date_after: op='calls'/'messages' — on or after this date (YYYY-MM-DD).
            date_before: op='calls'/'messages' — before this date (YYYY-MM-DD).
            next_page_uri: op='next_page' — `next_page_uri` of a previous list.
            page_size: lists — 1-1000 (default 50).
            full: raw records instead of the trimmed view.
        """
        courtier = _courtier_ou_none()
        if courtier is not None:
            return _read_courtier(courtier, op, sid, to, from_)
        avail = dict(country=country, number_type=number_type, contains=contains,
                     sms_enabled=sms_enabled, voice_enabled=voice_enabled)
        filters = dict(to=to, from_=from_, status=status, date_after=date_after,
                       date_before=date_before)
        if op != "next_page":
            _refuse_ignored(op, next_page_uri=next_page_uri)
        if op not in ("available_numbers",):
            _refuse_ignored(op, **avail)
        if op not in ("calls", "messages"):
            _refuse_ignored(op, **filters)
        if op in ("account", "available_numbers", "messages", "numbers", "next_page"):
            _refuse_ignored(op, sid=sid)

        if op == "account":
            _refuse_ignored(op, page_size=page_size)
            acct = _run(lambda: _client().get_account())
            balance = _run(lambda: _client().get_balance())
            out = _view(acct, full)
            out["balance"] = balance.get("balance")
            out["currency"] = balance.get("currency")
            return out
        if op == "numbers":
            return _view(_run(lambda: _client().list_numbers(page_size=page_size)), full)
        if op == "number":
            _refuse_ignored(op, page_size=page_size)
            return _view(_run(lambda: _client().get_number(_need(sid, "sid", op))), full)
        if op == "available_numbers":
            code, kind = _need(country, "country", op), number_type or "local"
            try:
                return _view(_client().search_available_numbers(
                    code, kind, contains=contains, sms_enabled=sms_enabled,
                    voice_enabled=voice_enabled, page_size=page_size), full)
            except UpstreamHTTPError as e:
                if e.status_code != 404:
                    raise _bad(_upstream_message(e))
                # Twilio answers 404 for a type it does not sell there: say which it does.
                kinds = _run(lambda: _client().available_number_types(code))
                raise _bad(f"Twilio sells no {kind} numbers in {code.upper()} to this account; "
                           f"types it sells there: {', '.join(kinds) or 'none'}.")
            except ValueError as e:
                raise _bad(str(e))
        if op == "calls" and sid:
            _refuse_ignored(op, page_size=page_size, **filters)
            return _view(_run(lambda: _client().get_call(sid)), full)
        if op == "calls":
            return _view(_run(lambda: _client().list_calls(
                to=to, from_=from_, status=status, start_time_after=date_after,
                start_time_before=date_before, page_size=page_size)), full)
        if op == "recordings":
            return _view(_run(lambda: _client().list_recordings(
                call_sid=sid, page_size=page_size)), full)
        if op == "recording_audio":
            _refuse_ignored(op, page_size=page_size, full=full)
            return _audio(_need(sid, "sid", op))
        if op == "messages":
            _refuse_ignored(op, status=status)
            return _view(_run(lambda: _client().list_messages(
                to=to, from_=from_, date_sent_after=date_after,
                date_sent_before=date_before, page_size=page_size)), full)
        if op == "message":
            _refuse_ignored(op, page_size=page_size)
            return _view(_run(lambda: _client().get_message(_need(sid, "sid", op))), full)
        if op == "next_page":
            _refuse_ignored(op, page_size=page_size)
            return _view(_run(lambda: _client().next_page(
                _need(next_page_uri, "next_page_uri", op))), full)
        raise _bad(f"invalid `op`: {op!r}.")

    @mcp.tool(annotations=ACTE)
    def twilio_act(
        op: Literal["add_number", "update_number", "remove_number", "call", "cancel_call",
                    "send_sms"],
        sid: Optional[str] = None,
        phone_number: Optional[str] = None,
        friendly_name: Optional[str] = None,
        bundle_sid: Optional[str] = None,
        address_sid: Optional[str] = None,
        voice_url: Optional[str] = None,
        sms_url: Optional[str] = None,
        rep: Optional[str] = None,
        to: Optional[List[str]] = None,
        from_: Optional[str] = None,
        body: Optional[str] = None,
        media_urls: Optional[List[str]] = None,
        announce: Optional[str] = None,
        language: Optional[str] = None,
        record: bool = True,
        timeout: Optional[int] = None,
        dry_run: bool = False,
    ) -> Any:
        """Act on the organization's Twilio account: numbers, calls, SMS.

        `op`:
        - `add_number` — buy `phone_number` (from `twilio` op='available_numbers').
          BILLED by Twilio: the first month at once, no proration on release.
          Countries with regulatory rules need `bundle_sid` (BU…, approved) and
          sometimes `address_sid` (AD…). Org admins only.
        - `update_number` — rename (`friendly_name`) or change the `voice_url` /
          `sms_url` of an owned number (`sid` = PN…). An empty string clears a URL.
        - `remove_number` — release an owned number (`sid`). IRREVERSIBLE: the
          number may never be bought back, the month paid is not refunded. Org
          admins only.
        - `call` — a BRIDGED call: Twilio first rings `rep` (your phone), plays
          `announce` if given, then dials the contact (`to`, one number) showing
          `from_` (a number of this account), and records both sides unless
          `record=false`. BILLED per minute, both legs. Returns the call `sid`;
          read it back with `twilio` op='calls' and its `sid`, its recording with
          op='recordings'.
        - `cancel_call` — end (or cancel if still ringing) the call `sid` (CA…).
        - `send_sms` — send `body` (and/or `media_urls`, MMS) from `from_` to each
          number in `to` (up to 50). `from_` is a number of this account, or an
          alphanumeric sender ID (up to 11 characters) where the country allows
          one: such a message cannot be replied to. BILLED per segment (160
          characters, 70 with accents outside GSM-7 or emojis). Returns one
          receipt line per recipient; one refused recipient does not stop the
          others. A batch that runs past 30 s stops and lists the rest under
          `not_attempted`: send those again, never the whole batch.

        `dry_run=true`: same validation, nothing sent to Twilio; returns what
        would be done.

        Without its own Twilio account connected, the organization's PLATFORM
        numbers are used (billed in credits): `send_sms` and `call` only, `from_`
        being one of those numbers (`twilio` op='numbers') or, for `send_sms`, a
        sender NAME (up to 11 letters, recipients in France, Italy or the UK, no
        replies); buying and releasing happen on the Phone page.

        Args:
            op: add_number | update_number | remove_number | call | cancel_call | send_sms.
            sid: update_number / remove_number (PN…), cancel_call (CA…).
            phone_number: add_number — the E.164 number to buy.
            friendly_name: add_number / update_number — a label for the number.
            bundle_sid: add_number — approved regulatory bundle (BU…).
            address_sid: add_number — registered address (AD…), where required.
            voice_url: add_number / update_number — URL Twilio calls on an inbound call.
            sms_url: add_number / update_number — URL Twilio calls on an inbound SMS.
            rep: call — the phone that rings first (yours, E.164).
            to: call — [the contact's number]; send_sms — recipients (E.164).
            from_: call / send_sms — number of this account (or SMS sender ID).
            body: send_sms — the text, up to 1600 characters.
            media_urls: send_sms — up to 10 public file URLs (MMS).
            announce: call — what the rep hears before the contact is dialed.
            language: call — language of `announce` (fr-FR, en-US…).
            record: call — record both sides (default true).
            timeout: call — seconds to ring each leg (5-600, default 30).
            dry_run: validate only, send nothing.
        """
        courtier = _courtier_ou_none()
        if courtier is not None:
            return _act_courtier(courtier, op, rep=rep, to=to, from_=from_, body=body,
                                 media_urls=media_urls, dry_run=dry_run)
        num_args = dict(phone_number=phone_number, friendly_name=friendly_name,
                        bundle_sid=bundle_sid, address_sid=address_sid,
                        voice_url=voice_url, sms_url=sms_url)
        call_args = dict(rep=rep, announce=announce, language=language,
                         timeout=timeout)
        msg_args = dict(body=body, media_urls=media_urls)

        if op in ("add_number", "remove_number") and not _caller_is_org_admin():
            raise _bad(f"op='{op}' is reserved to an admin of the organization: it "
                       "spends money or loses a number for good.")

        if op == "add_number":
            _refuse_ignored(op, sid=sid, to=to, from_=from_, **call_args, **msg_args)
            number = _need(phone_number, "phone_number", op)
            if dry_run:
                return {"dry_run": True, "would": "buy", "phone_number": number,
                        "bundle_sid": bundle_sid, "address_sid": address_sid}
            return _view(_run(lambda: _client().buy_number(
                number, friendly_name=friendly_name, address_sid=address_sid,
                bundle_sid=bundle_sid, voice_url=voice_url, sms_url=sms_url)), False)

        if op == "update_number":
            _refuse_ignored(op, phone_number=phone_number, bundle_sid=bundle_sid,
                            address_sid=address_sid, to=to, from_=from_, **call_args,
                            **msg_args)
            number_sid = _need(sid, "sid", op)
            changes = {k: v for k, v in dict(friendly_name=friendly_name, voice_url=voice_url,
                                             sms_url=sms_url).items() if v is not None}
            if not changes:
                raise _bad("op='update_number' needs `friendly_name`, `voice_url` or `sms_url`.")
            if dry_run:
                current = _run(lambda: _client().get_number(number_sid))
                return {"dry_run": True, "sid": number_sid, "changes": {
                    k: {"from": current.get(k), "to": v} for k, v in changes.items()}}
            return _view(_run(lambda: _client().update_number(number_sid, **changes)), False)

        if op == "remove_number":
            _refuse_ignored(op, to=to, from_=from_, **num_args, **call_args, **msg_args)
            number_sid = _need(sid, "sid", op)
            if dry_run:
                current = _run(lambda: _client().get_number(number_sid))
                return {"dry_run": True, "would": "release", "sid": number_sid,
                        "phone_number": current.get("phone_number")}
            return _run(lambda: _client().release_number(number_sid))

        if op == "call":
            _refuse_ignored(op, sid=sid, **num_args, **msg_args)
            first = _need(rep, "rep", op)
            contacts = _need(to, "to", op)
            if len(contacts) != 1:
                raise _bad("op='call' dials ONE contact: pass `to` as a single-number list.")
            caller = _need(from_, "from_", op)
            twiml = _run(lambda: bridge_twiml(
                contacts[0], caller_id=caller, announce=announce, language=language,
                record="record-from-answer-dual" if record else None,
                timeout=timeout or 30))
            if dry_run:
                return {"dry_run": True, "would": "call", "rings_first": first,
                        "then_dials": contacts[0], "caller_id": caller, "record": record,
                        "twiml": twiml}
            return _view(_run(lambda: _client().create_call(
                first, caller, twiml=twiml, timeout=timeout)), False)

        if op == "cancel_call":
            _refuse_ignored(op, to=to, from_=from_, **num_args, **call_args, **msg_args)
            call_sid = _need(sid, "sid", op)
            if dry_run:
                current = _run(lambda: _client().get_call(call_sid))
                return {"dry_run": True, "would": "cancel_call", "sid": call_sid,
                        "status": current.get("status")}
            return _view(_run(lambda: _client().hangup_call(call_sid)), False)

        if op == "send_sms":
            _refuse_ignored(op, sid=sid, **num_args, **call_args)
            recipients = _need(to, "to", op)
            sender = _need(from_, "from_", op)
            if not body and not media_urls:
                raise _bad("op='send_sms' requires `body`, `media_urls`, or both.")
            if len(recipients) > SMS_BATCH_MAX:
                raise _bad(f"op='send_sms': at most {SMS_BATCH_MAX} recipients per call.")
            if dry_run:
                checked = [_run(lambda r=r: e164(r, "to")) for r in recipients]
                return {"dry_run": True, "would": "send", "from": sender, "to": checked,
                        "characters": len(body or "")}
            client = _client()
            sent, failed, not_attempted = [], [], []
            started = time.monotonic()
            for index, number in enumerate(recipients):
                if time.monotonic() - started > SMS_TIME_BUDGET_S:
                    not_attempted = [{"index": i, "to": recipients[i]}
                                     for i in range(index, len(recipients))]
                    break
                try:
                    m = client.send_message(number, body=body, from_=sender,
                                            media_urls=media_urls)
                    sent.append({"index": index, "to": number, "sid": m.get("sid"),
                                 "status": m.get("status"),
                                 "segments": m.get("num_segments")})
                except UpstreamHTTPError as e:
                    if e.status_code in (401, 403):
                        raise _bad(_upstream_message(e))
                    failed.append({"index": index, "to": number,
                                   "error": _upstream_message(e)})
                except ValueError as e:
                    failed.append({"index": index, "to": number, "error": str(e)})
            out = {"total": len(recipients), "sent": sent, "failed": failed}
            if not_attempted:
                out["not_attempted"] = not_attempted
                out["warning"] = (f"Stopped after {SMS_TIME_BUDGET_S:.0f} s to stay inside the "
                                  "call timeout: send `not_attempted` again in a new call.")
            return out

        raise _bad(f"invalid `op`: {op!r}.")
