"""Lemlist — the NON-campaign half of the API: CRM, inbox, signals, settings.

Second module of the same connector (`providers/lemlist.py` declares
`modules=("lemlist", "lemlist_crm")`), for readability rather than
scope: `tools/lemlist.py` holds the campaign and its leads, this file holds
everything else — contacts and companies (the lemlist CRM), inbox, unsubscribes,
watch lists, tasks, shared base, team, mailboxes, lemwarm, deliverability
alerts, webhooks.

**A CONTACT is not a LEAD.** The lead is a person's copy INSIDE a
campaign — its sending state, its variables; it lives in `lemlist_lead`. The
contact is the person in the lemlist CRM, independent of campaigns. This is
the most costly confusion of this API, and it separates the two modules.

What SENDS here fits in a single tool, hidden by default:
`lemlist_act` (the connector's acting tool — `<ns>_act`, the one an MCP client asks to
confirm). Its three routes (`/inbox/email`, `/inbox/linkedin`,
`/inbox/whatsapp`) are the most immediate sends of the whole connector —
no campaign, no sequence, no review in front of them: the message goes out. Hence
the BARE tool, the only grain that `DEFAULT_HIDDEN_TOOLS` knows how to hide.
Since 23/10/2026 it also saves and deletes the personas of the shared base, so that
`lemlist_database` only reads — and a recipe (`oto_recipe`) can call it.

Two surfaces can send INDIRECTLY and stay visible, by saying so:
a watch list set to `push_to_campaign` feeds a campaign on its own,
and `lemlist_mailbox(op="lemwarm_start")` sends — but within the warm-up network
(other lemlist mailboxes), never to a prospect.

Key resolved per call via `access.resolve_api_key("lemlist")`: each user sees
THEIR data, so a user key is required, no platform quota.
"""
from __future__ import annotations

import html
import re
from typing import Literal, Optional

import requests
from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INTERNAL_ERROR, INVALID_PARAMS

from .. import access
from .lecture import ACTE, LECTURE


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


#: The module of oto-core that shapes a page of the shared base (`fields`, `compact`).
_DATABASE_VIEWS = "oto.tools.lemlist.database_views"


def _raised_by_database_view(e: BaseException) -> bool:
    """True when `e` was raised inside the view module itself, not by the request or
    anything it calls: the view refuses a page that has no `results` list with a bare
    `RuntimeError`, and only that one is renamed."""
    tb = e.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    return tb is not None and tb.tb_frame.f_globals.get("__name__") == _DATABASE_VIEWS


def _page_without_results(kind: str) -> McpError:
    """lemlist answered a search with a page that has no `results` list. The view
    cannot apply, and returning the page whole would hand the agent the megabytes
    `fields`/`compact` were asked to avoid."""
    return McpError(ErrorData(code=INTERNAL_ERROR, message=(
        f"lemlist returned a {kind} page without a `results` list, so `fields` and "
        "`compact` cannot shape it. Call again without `fields` and `compact` to "
        "read the page as lemlist sends it."),
        data={"code": "lemlist_page_without_results", "retryable": False}))


def _need(**kwargs) -> None:
    """Refuse au bord ce que lemlist refuserait par un 400 muet."""
    missing = [k for k, v in kwargs.items() if not v]
    if missing:
        raise _bad(f"{', '.join('`%s`' % m for m in missing)} required")


# --- Inbox messages: plain text and a size cap -----------------------------------
# One HTML email with a pasted signature image (base64 inside an <img>) weighs
# 300 000+ characters: a single message read with `limit=1` was more than an
# agent's whole tool-result budget, with no way to get at the text. Opt-in, so
# callers that read the raw HTML keep it.

_DATA_URI = re.compile(r"data:[\w/+.-]+;base64,[A-Za-z0-9+/=\s]+")
_BASE64_BLOB = re.compile(r"^[A-Za-z0-9+/=\s]{2000,}$")
_LOOKS_HTML = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>")
_INVISIBLE = re.compile(r"<(style|script|head)\b.*?</\1\s*>", re.I | re.S)
_LINE_BREAK = re.compile(r"<\s*(br|/p|/div|/li|/tr|/h[1-6]|/blockquote)\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")


def _plain_text(value: str) -> str:
    """HTML (or text with inline images) to readable plain text."""
    value = _DATA_URI.sub("[image]", value)
    if _BASE64_BLOB.match(value):
        return f"[binary, {len(value)} characters omitted]"
    if not _LOOKS_HTML.search(value):
        return value
    value = _INVISIBLE.sub("", value)
    value = _LINE_BREAK.sub("\n", value)
    value = html.unescape(_TAG.sub("", value))
    value = re.sub(r"[ \t\u00a0]+", " ", value)
    return re.sub(r"\s*\n\s*(\n\s*)+", "\n\n", value).strip()


def _readable_messages(value, text_only: bool, max_chars: Optional[int]):
    """Walk the messages payload, whatever its shape, and return it with every
    string made plain text (`text_only`) and cut at `max_chars`, plus the number
    of strings that were cut — so a cut text is never mistaken for a whole one."""
    cut = 0

    def walk(v):
        nonlocal cut
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, str):
            if text_only:
                v = _plain_text(v)
            if max_chars and len(v) > max_chars:
                cut += 1
                v = v[:max_chars] + f" [... truncated, {len(v) - max_chars} more characters]"
        return v

    return walk(value), cut


# Page servie par les deux listes de désinscription quand `limit` est omis. Sans
# elle, lemlist rend TOUT : 2 439 lignes et 325 Ko en un appel sur un compte
# client réel (14/10/2026), d'où une session d'agent qui se noie au premier
# appel — et qui, relancée par son cron, refait le même appel. C'est aussi le
# plafond que la doc lemlist donne à la liste v2 (`limit` max 100).
_UNSUB_DEFAULT_PAGE = 100


def _unsub_page(rows, limit: int, offset: Optional[int]) -> dict:
    """La page, plus `next_offset` quand elle est pleine (il peut en rester).

    Pleine se juge contre le plafond documenté aussi : un `limit=500` que lemlist
    ramène à 100 lignes n'est pas la dernière page. `next_offset` suit les lignes
    reçues, pas le `limit` demandé."""
    full = isinstance(rows, list) and len(rows) >= min(limit, _UNSUB_DEFAULT_PAGE)
    return {"limit": limit, "offset": offset or 0,
            "next_offset": (offset or 0) + len(rows) if full else None}


def register(mcp: FastMCP) -> None:
    from oto.tools.lemlist import LemlistClient

    def _client() -> tuple[LemlistClient, bool]:
        key, is_platform = access.resolve_api_key("lemlist")
        return LemlistClient(api_key=key), is_platform

    # --- CRM: contacts & companies ---------------------------------------------

    @mcp.tool()
    def lemlist_contact(
        op: Literal["list", "get", "upsert", "delete",
                    "lists", "list_create", "list_manage", "list_export"],
        id_or_email: Optional[str] = None,
        contact: Optional[dict] = None,
        list_id: Optional[str] = None,
        contact_ids: Optional[list[str]] = None,
        name: Optional[str] = None,
        search: Optional[str] = None,
        ids_or_emails: Optional[list[str]] = None,
        email: Optional[str] = None,
        not_in_any_campaign: Optional[bool] = None,
        company_id: Optional[str] = None,
        company_domain: Optional[str] = None,
        company_linkedin_url: Optional[str] = None,
        company_salesnav_url: Optional[str] = None,
        field_rejection_reason: Optional[str] = None,
        entity: Optional[str] = None,
        action: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> dict:
        """Contacts of the lemlist CRM, and the lists that group them.

        A contact is the PERSON, campaign-independent — its copy inside a
        campaign is a lead (`lemlist_lead`). Nothing here sends.

        Args by op:
        - `list`: `ids_or_emails` (≤ 100, exact lookup) or the filters
          `search`, `email`, `list_id`, `not_in_any_campaign`, `company_id`,
          `company_domain`, `company_linkedin_url`, `company_salesnav_url`,
          `field_rejection_reason` (why a CRM sync rejected a field),
          `limit`, `offset`.
        - `get` / `delete`: `id_or_email`.
        - `upsert`: `contact` — one dict, create AND update. Identity keys
          `contactId` / `email` / `linkedinUrl`; then `firstName`, `lastName`,
          `phone`, `jobTitle`, `location`, `contactOwner`, the company link…
        - `lists`: optional `search`. `list_create`: `name`.
        - `list_manage`: `list_id` + `contact_ids`. ADDS by default —
          `action="remove"` takes them out.
        - `list_export`: `list_id`, optional `entity` (`contact` or `company`).
          Returns CSV text.
        """
        client, is_platform = _client()
        if op == "list":
            result = {"contacts": client.list_contacts(
                ids_or_emails=ids_or_emails, search=search, email=email,
                list_id=list_id, not_in_any_campaign=not_in_any_campaign,
                company_id=company_id, company_domain=company_domain,
                company_linkedin_url=company_linkedin_url,
                company_salesnav_url=company_salesnav_url,
                field_rejection_reason=field_rejection_reason,
                limit=limit, offset=offset)}
        elif op == "get":
            _need(id_or_email=id_or_email)
            result = client.get_contact(id_or_email)
        elif op == "upsert":
            _need(contact=contact)
            result = client.upsert_contact(contact)
        elif op == "delete":
            _need(id_or_email=id_or_email)
            result = client.delete_contact(id_or_email)
        elif op == "lists":
            result = {"lists": client.list_contact_lists(search=search)}
        elif op == "list_create":
            _need(name=name)
            result = client.create_contact_list(name)
        elif op == "list_manage":
            _need(list_id=list_id, contact_ids=contact_ids)
            result = client.manage_contact_list(list_id, contact_ids, action=action)
        elif op == "list_export":
            _need(list_id=list_id)
            result = {"csv": client.export_contact_list(list_id, entity=entity)}
        else:
            raise _bad(f'unknown op "{op}" — expected: list, get, upsert, delete, '
                       "lists, list_create, list_manage, list_export")
        return result

    @mcp.tool()
    def lemlist_company(
        op: Literal["list", "upsert", "delete", "notes", "note_create"],
        company_id: Optional[str] = None,
        company: Optional[dict] = None,
        note: Optional[str] = None,
        ids_or_domains: Optional[list[str]] = None,
        search: Optional[str] = None,
        force: Optional[bool] = None,
        fields: Optional[list[str]] = None,
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = None,
        crm_sync_status: Optional[str] = None,
        field_rejection_reason: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        page: Optional[int] = None,
    ) -> dict:
        """Companies of the lemlist CRM, and their notes.

        Args by op:
        - `list`: `ids_or_domains` (exact lookup) or `search`, `fields` (keep
          only these), `sort_by`/`sort_order`, `crm_sync_status`,
          `field_rejection_reason`, `limit`, `offset`.
        - `upsert`: `company` — identity keys `companyId` / `domain` /
          `linkedinUrl`, then `name`, `industry`, `size`, `location`…
        - `delete`: `company_id`; `force=True` deletes it even with contacts
          attached (lemlist refuses otherwise).
        - `notes`: `company_id` (+ `limit`, `page`, `sort_by`, `sort_order`).
          `note_create`: `company_id` + `note`.
        """
        client, is_platform = _client()
        if op == "list":
            result = {"companies": client.list_companies(
                ids_or_domains=ids_or_domains, search=search, fields=fields,
                sort_by=sort_by, sort_order=sort_order,
                crm_sync_status=crm_sync_status,
                field_rejection_reason=field_rejection_reason,
                limit=limit, offset=offset)}
        elif op == "upsert":
            _need(company=company)
            result = client.upsert_company(company)
        elif op == "delete":
            _need(company_id=company_id)
            result = client.delete_company(company_id, force=force)
        elif op == "notes":
            _need(company_id=company_id)
            result = {"notes": client.get_company_notes(
                company_id, limit=limit, page=page,
                sort_by=sort_by, sort_order=sort_order)}
        elif op == "note_create":
            _need(company_id=company_id, note=note)
            result = client.create_company_note(company_id, note)
        else:
            raise _bad(f'unknown op "{op}" — expected: list, upsert, delete, '
                       "notes, note_create")
        return result

    # --- Inbox ------------------------------------------------------------------

    @mcp.tool()
    def lemlist_inbox(
        op: Literal["list", "messages", "labels", "label_get", "label_create",
                    "labels_attach", "labels_remove", "drafts", "draft_get",
                    "draft_create", "draft_update", "draft_delete"],
        user_id: Optional[str] = None,
        contact_id: Optional[str] = None,
        draft_id: Optional[str] = None,
        draft_owner: Optional[str] = None,
        label_id: Optional[str] = None,
        label_ids: Optional[list[str]] = None,
        label_name: Optional[str] = None,
        append: bool = True,
        channel: Optional[str] = None,
        content: Optional[str] = None,
        subject: Optional[str] = None,
        cc: Optional[list[str]] = None,
        attachments: Optional[list[dict]] = None,
        reply_to_activity_id: Optional[str] = None,
        source_metadata: Optional[dict] = None,
        draft: Optional[dict] = None,
        mark_as_read: Optional[bool] = None,
        text_only: bool = False,
        max_chars: Optional[int] = None,
        limit: Optional[int] = None,
        skip: Optional[int] = None,
        page: Optional[int] = None,
    ) -> dict:
        """Read the unified inbox, write drafts, manage labels. Sends nothing —
        sending is `lemlist_act`.

        Conversations are keyed by CONTACT, all channels merged.

        Args by op:
        - `list`: `user_id` (required — an inbox belongs to a person), `page`,
          `limit`.
        - `messages`: `contact_id` + optional `user_id`, `limit`, `skip`.
          ⚠️ `mark_as_read=True` MUTATES on a read call.
          ⚠️ One HTML email can weigh 300 000+ characters (images pasted as
          base64): to read what was SAID, pass `text_only=True` (HTML turned
          to plain text, inline images dropped) and `max_chars` (each text cut
          there). The answer then carries `truncated_fields`, the number of
          texts that were cut — 0 means you have them whole.
        - `labels` / `label_get` (`label_id`) / `label_create` (`label_name`).
        - `labels_attach`: `contact_id` + `label_ids`; `append=False` REPLACES
          the conversation's labels. `labels_remove`: same minus `append`.
        - `drafts` / `draft_get` / `draft_delete`: `contact_id` + `draft_owner`
          (a user id — a draft belongs to a person) + `draft_id` where relevant.
        - `draft_create`: `contact_id`, `draft_owner`, `channel` (`email`,
          `linkedin`, `whatsapp`, `sms`), `content`, optional `subject`, `cc`,
          `attachments`, `reply_to_activity_id` (answer a specific message),
          `source_metadata`.
        - `draft_update`: same ids + `draft` (the fields to change).
        """
        client, is_platform = _client()
        if op == "list":
            _need(user_id=user_id)
            result = {"inboxes": client.list_inboxes(
                user_id, page=page, limit=limit)}
        elif op == "messages":
            _need(contact_id=contact_id)
            messages = client.get_contact_messages(
                contact_id, user_id=user_id, limit=limit, skip=skip,
                mark_as_read=mark_as_read)
            if max_chars is not None and max_chars < 1:
                raise _bad("`max_chars` must be at least 1")
            if text_only or max_chars:
                messages, cut = _readable_messages(messages, text_only, max_chars)
                result = {"messages": messages, "truncated_fields": cut}
            else:
                result = {"messages": messages}
        elif op == "labels":
            result = {"labels": client.list_inbox_labels()}
        elif op == "label_get":
            _need(label_id=label_id)
            result = client.get_inbox_label(label_id)
        elif op == "label_create":
            _need(label_name=label_name)
            result = client.create_inbox_label(label_name)
        elif op == "labels_attach":
            _need(contact_id=contact_id, label_ids=label_ids)
            result = client.attach_inbox_labels(contact_id, label_ids, append=append)
        elif op == "labels_remove":
            _need(contact_id=contact_id, label_ids=label_ids)
            result = client.remove_inbox_labels(contact_id, label_ids)
        elif op in ("drafts", "draft_get", "draft_create", "draft_update",
                    "draft_delete"):
            _need(contact_id=contact_id, draft_owner=draft_owner)
            if op == "drafts":
                result = {"drafts": client.list_drafts(contact_id, draft_owner)}
            elif op == "draft_create":
                _need(channel=channel, content=content)
                result = client.create_draft(
                    contact_id, draft_owner, channel=channel, content=content,
                    subject=subject, cc=cc, attachments=attachments,
                    reply_to_activity_id=reply_to_activity_id,
                    source_metadata=source_metadata)
            else:
                _need(draft_id=draft_id)
                if op == "draft_get":
                    result = client.get_draft(contact_id, draft_id, draft_owner)
                elif op == "draft_update":
                    _need(draft=draft)
                    result = client.update_draft(
                        contact_id, draft_id, draft_owner, draft)
                else:
                    result = client.delete_draft(contact_id, draft_id, draft_owner)
        else:
            raise _bad(
                f'unknown op "{op}" — expected: list, messages, labels, label_get, '
                "label_create, labels_attach, labels_remove, drafts, draft_get, "
                "draft_create, draft_update, draft_delete")
        return result

    @mcp.tool(annotations=ACTE)
    def lemlist_act(
        op: Literal["send_email", "send_linkedin", "send_whatsapp",
                    "create_persona", "delete_persona"],
        message: Optional[str] = None,
        send_user_id: Optional[str] = None,
        contact_id: Optional[str] = None,
        lead_id: Optional[str] = None,
        send_user_email: Optional[str] = None,
        send_user_mailbox_id: Optional[str] = None,
        send_user_whatsapp_account_id: Optional[str] = None,
        subject: Optional[str] = None,
        cc: Optional[list[str]] = None,
        reply_to_activity_id: Optional[str] = None,
        name: Optional[str] = None,
        filters: Optional[list[dict]] = None,
        mode: Optional[str] = None,
        persona_id: Optional[str] = None,
    ) -> dict:
        """Send a message from the inbox — DIRECTLY, to a real person — or save or
        delete a persona of the shared base.
        `oto_guide op=read slug="lemlist-playbook"`: build order, where each id comes from, and the doc↔API gaps.

        The most immediate send of the whole connector: no campaign, no
        sequence, no review in front of it. Hidden by default for that reason.

        The sender ids are not optional decoration — lemlist will not guess a
        mailbox. Get them from `lemlist_team(op="user_channels")`.

        Args by op:
        - `send_email`: `send_user_id`, `send_user_email`, `send_user_mailbox_id`,
          `message`; optional `contact_id`/`lead_id`, `subject`, `cc`,
          `reply_to_activity_id`.
        - `send_linkedin`: `send_user_id`, `lead_id`, `contact_id`, `message`.
        - `send_whatsapp`: the same plus `send_user_whatsapp_account_id`.
        - `create_persona`: `name` + `filters` + `mode` (`leads` or `companies`) —
          a saved search of the shared base, used by watch lists.
        - `delete_persona`: `persona_id` — irreversible.
        """
        if op.startswith("send_"):
            _need(message=message, send_user_id=send_user_id)
        client, is_platform = _client()
        if op == "send_email":
            _need(send_user_email=send_user_email,
                  send_user_mailbox_id=send_user_mailbox_id)
            result = client.send_inbox_email(
                send_user_id=send_user_id, send_user_email=send_user_email,
                send_user_mailbox_id=send_user_mailbox_id, message=message,
                contact_id=contact_id, lead_id=lead_id, subject=subject, cc=cc,
                reply_to_activity_id=reply_to_activity_id)
        elif op == "send_linkedin":
            _need(lead_id=lead_id, contact_id=contact_id)
            result = client.send_linkedin_message(
                send_user_id=send_user_id, lead_id=lead_id,
                contact_id=contact_id, message=message)
        elif op == "send_whatsapp":
            _need(lead_id=lead_id, contact_id=contact_id,
                  send_user_whatsapp_account_id=send_user_whatsapp_account_id)
            result = client.send_whatsapp_message(
                send_user_id=send_user_id,
                send_user_whatsapp_account_id=send_user_whatsapp_account_id,
                lead_id=lead_id, contact_id=contact_id, message=message)
        elif op == "create_persona":
            _need(name=name, mode=mode)
            result = client.create_persona(name, filters=filters or [], mode=mode)
        elif op == "delete_persona":
            _need(persona_id=persona_id)
            result = client.delete_persona(persona_id)
        else:
            raise _bad(f'unknown op "{op}" — expected: send_email, send_linkedin, '
                       'send_whatsapp, create_persona, delete_persona')
        return result

    # --- Unsubscribes ------------------------------------------------------------

    @mcp.tool()
    def lemlist_unsubscribe(
        op: Literal["list", "get", "add", "delete", "export",
                    "var_list", "var_get", "var_add", "var_bulk", "var_remove",
                    "var_export", "contact_status", "contact_add",
                    "contact_remove", "contact_export"],
        email: Optional[str] = None,
        value: Optional[str] = None,
        values: Optional[list[str]] = None,
        contact_id: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> dict:
        """Manage who must not be contacted. THREE lists, not one.

        They do not hold the same objects, and writing to one does not write to
        the others:
        - v1 `list`/`get`/`add`/`delete`/`export` — emails and whole DOMAINS.
        - v2 `var_*` — any identifying value (email, domain, LinkedIn URL,
          phone), with `var_bulk` for up to 10 000 at once.
        - v2 `contact_*` — the do-not-contact flag on a CRM contact, which rides
          the CONTACT rather than one of its values.

        To check ONE value, use `get` (v1) or `var_get` (v2): a listing can hold
        thousands of rows. `list` and `var_list` are paged: 100 rows when
        `limit` is omitted (lemlist caps `var_list` at 100 anyway), and
        `next_offset` says where the next page starts (null on the last one).

        Args: `email` (v1), `value` / `values` (variables), `contact_id`
        (contacts), `limit`/`offset` on the two listings. The `*_export` ops
        return CSV text.
        """
        client, is_platform = _client()
        if op in ("list", "var_list") and limit is None:
            limit = _UNSUB_DEFAULT_PAGE
        if op == "list":
            rows = client.list_unsubscribes(offset=offset, limit=limit)
            result = {"unsubscribes": rows, **_unsub_page(rows, limit, offset)}
        elif op in ("get", "add", "delete"):
            _need(email=email)
            result = {"get": client.get_unsubscribe, "add": client.add_unsubscribe,
                      "delete": client.delete_unsubscribe}[op](email)
        elif op == "export":
            result = {"csv": client.export_unsubscribes()}
        elif op == "var_list":
            rows = client.list_unsubscribed_variables(offset=offset, limit=limit)
            result = {"variables": rows, **_unsub_page(rows, limit, offset)}
        elif op in ("var_get", "var_add", "var_remove"):
            _need(value=value)
            result = {"var_get": client.get_unsubscribed_variable,
                      "var_add": client.unsubscribe_variable,
                      "var_remove": client.resubscribe_variable}[op](value)
        elif op == "var_bulk":
            _need(values=values)
            result = client.bulk_unsubscribe_variables(values)
        elif op == "var_export":
            result = {"csv": client.export_unsubscribed_variables()}
        elif op in ("contact_status", "contact_add", "contact_remove"):
            _need(contact_id=contact_id)
            result = {"contact_status": client.get_contact_subscription,
                      "contact_add": client.unsubscribe_contact,
                      "contact_remove": client.resubscribe_contact}[op](contact_id)
        elif op == "contact_export":
            result = {"csv": client.export_unsubscribed_contacts()}
        else:
            raise _bad(
                f'unknown op "{op}" — expected: list, get, add, delete, export, '
                "var_list, var_get, var_add, var_bulk, var_remove, var_export, "
                "contact_status, contact_add, contact_remove, contact_export")
        return result

    # --- Tasks, signals, shared base ----------------------------------------------

    @mcp.tool()
    def lemlist_task(
        op: Literal["list", "create", "update", "ignore"],
        task_id: Optional[str] = None,
        task_type: Optional[str] = None,
        assigned_to: Optional[str] = None,
        due_date: Optional[str] = None,
        record_id: Optional[str] = None,
        title: Optional[str] = None,
        message: Optional[str] = None,
        priority: Optional[str] = None,
        done: Optional[bool] = None,
        ids: Optional[list[str]] = None,
        images: Optional[list[str]] = None,
        videos: Optional[list[str]] = None,
        filters: Optional[list[dict]] = None,
        page: Optional[int] = None,
    ) -> dict:
        """Manual tasks assigned to a user (call, LinkedIn touch, follow-up).

        A task is a REMINDER for a human: creating one sends nothing.

        Args by op:
        - `list`: `page`, `filters` (list of `{filterId, …}`).
        - `create`: `task_type` (`email`, `manual`, `phone`, `linkedin`),
          `assigned_to` (user id), `due_date` AND `record_id` (the contact
          `ctc_…` or lead `lea_…` the task hangs off) — all four required, the
          last one despite lemlist documenting it optional; then `title`,
          `message`, `priority` (`0` high … `2` low), `images`/`videos`
          (public HTTPS URLs).
        - `update`: `task_id` + any of `title`, `message`, `priority`,
          `due_date`, `assigned_to`, `done`, `images`, `videos`.
        - `ignore`: `ids` — dismiss without completing.
        """
        client, is_platform = _client()
        if op == "list":
            result = {"tasks": client.list_tasks(page=page, filters=filters)}
        elif op == "create":
            _need(task_type=task_type, assigned_to=assigned_to,
                  due_date=due_date, record_id=record_id)
            result = client.create_task(
                task_type=task_type, assigned_to=assigned_to, due_date=due_date,
                record_id=record_id, title=title, message=message,
                priority=priority, images=images, videos=videos)
        elif op == "update":
            _need(task_id=task_id)
            data = {k: v for k, v in {
                "title": title, "message": message, "priority": priority,
                "dueDate": due_date, "assignedTo": assigned_to, "done": done,
                "images": images, "videos": videos,
            }.items() if v is not None}
            if not data:
                raise _bad("nothing to update — pass at least one field")
            result = client.update_task(task_id, data)
        elif op == "ignore":
            _need(ids=ids)
            result = client.ignore_tasks(ids)
        else:
            raise _bad(f'unknown op "{op}" — expected: list, create, update, ignore')
        return result

    @mcp.tool()
    def lemlist_watchlist(
        op: Literal["list", "create", "update", "delete", "filters",
                    "filter_values", "library", "history", "signals",
                    "push_signals"],
        watch_list_id: Optional[str] = None,
        name: Optional[str] = None,
        watch_type: Optional[str] = None,
        filters: Optional[list] = None,
        emoji: Optional[str] = None,
        signal_processing_type: Optional[str] = None,
        activate: Optional[bool] = None,
        segment_type: Optional[str] = None,
        opportunity_template: Optional[dict] = None,
        persona_id: Optional[str] = None,
        campaign_id: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = None,
        received_from: Optional[str] = None,
        received_to: Optional[str] = None,
        settings: Optional[dict] = None,
        filter_id: Optional[str] = None,
        query: Optional[str] = None,
        contact: Optional[dict] = None,
        company: Optional[dict] = None,
        custom_fields: Optional[dict] = None,
        status: Optional[str] = None,
        page: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """Watch lists: standing alerts on buying signals (hiring, funding, job
        change, tech move, website visit…), and the signals they catch.
        `oto_guide op=read slug="lemlist-playbook"`: build order, where each id comes from, and the doc↔API gaps.

        ⚠️ A list created with `signal_processing_type="push_to_campaign"` and
        `activate=True` feeds a campaign ON ITS OWN — the one configuration
        here that can end in messages being sent without a further call.

        Args by op:
        - `list`: `watch_type`, `status`, `page`, `limit`.
        - `create`: `name` + `watch_type` (`companyIsHiring`,
          `companyRaisedFunds`, `jobChange`, `newHire`, `technologyChange`,
          `buyingIntent`, `companyEmployeeVisitedMyWebsite`…), then `filters`.
          Read `op="filters"` for that type
          FIRST: each type has its own REQUIRED filters (`companyIsHiring`
          needs `title`, `location` and `maxIdentificationsPerDay`), the values
          must be the canonical ones `op="filter_values"` returns — a free
          string is rejected — and numbers travel as STRINGS
          (`{"filterId": "maxIdentificationsPerDay", "in": ["5"]}`).
          Optional `emoji`, `signal_processing_type` (`manual`,
          `create_opportunity`, `push_to_campaign`), `activate` (default False
          — the list is created as a draft), `segment_type`,
          `opportunity_template` (what `create_opportunity` builds: owner,
          channel, priority), and with `push_to_campaign` only:
          `campaign_id` (the campaign fed) and `persona_id` (`pdp_…`, from
          `lemlist_database op="personas"`: who to push when a signal names
          a company and no person).
        - `update`: `watch_list_id` + `settings` (raw PATCH body).
          `delete`: `watch_list_id`.
        - `filters` (optional `watch_type`) / `filter_values` (`filter_id`,
          optional `query`) / `library`: what a list can be built from.
        - `history`: `watch_list_id`. `signals`: the catches, filterable by
          `watch_list_id`, `watch_type`, `status`, `received_from`/`received_to`
          (ISO bounds), and sorted by `sort_by`/`sort_order`.
        - `push_signals`: `watch_list_id` + `contact` (needs `linkedinUrl`) +
          `company` (needs `domain` and `name`), optional `custom_fields` —
          feed a signal detected OUTSIDE lemlist.

        Website visitors (`companyEmployeeVisitedMyWebsite`), lemlist's own
        visitor identification:
        - Needs lemlist's tracking script on the site, installed from the
          lemlist app (Signal agents ▸ Create ▸ Website visitors) — no API
          call installs it.
        - ⚠️ An ACTIVE list spends 20 lemlist credits per visit caught: set
          the `maxIdentificationsPerDay` filter, and confirm before `activate`.
        - Filters: `website`, `pagesTracked`, `minVisitDuration`,
          `minPageViewed`, `visitLocation`, `internalTraffic`,
          `excludedVisitorIps`, `maxIdentificationsPerDay`.
        - Each signal carries `company` (name, domain, linkedinUrl, industry)
          and `signalData` (`websiteVisited` = the page, `visitorWebsite`,
          `visitorCountry`, `visitorDuration` in seconds); `contact` only when
          lemlist could name the visitor (some US traffic).
        - Read them on a schedule: `op="signals"`,
          `watch_type="companyEmployeeVisitedMyWebsite"`, `received_from`.
          Or on each visit: `lemlist_webhook op="add"` with
          `event_type="signalRegistered"` (fires for EVERY watch list — check
          the type).
        """
        client, is_platform = _client()
        if op == "list":
            result = {"watch_lists": client.list_watch_lists(
                page=page, limit=limit, type=watch_type, status=status)}
        elif op == "create":
            _need(name=name, watch_type=watch_type)
            if (persona_id is not None or campaign_id is not None) and \
                    signal_processing_type != "push_to_campaign":
                # The client refuses it too (ValueError), but that would reach the
                # agent as an internal error: refused here, as a bad parameter.
                raise _bad("`persona_id` and `campaign_id` need "
                           "`signal_processing_type=\"push_to_campaign\"`")
            # Same precautions: `segment_type`, `signal_processing_type` and
            # `activate` have defaults on the client side because the API REQUIRES them
            # (see the gaps found live). Passing them as None would cause
            # exactly the error they serve to avoid — we omit them.
            result = client.create_watch_list(name, type=watch_type, **{
                k: v for k, v in (
                    ("filters", filters), ("emoji", emoji),
                    ("signal_processing_type", signal_processing_type),
                    ("activate", activate), ("segment_type", segment_type),
                    ("signal_opportunity_template", opportunity_template),
                    ("persona_id", persona_id), ("campaign_id", campaign_id),
                ) if v is not None})
        elif op == "update":
            _need(watch_list_id=watch_list_id, settings=settings)
            result = client.update_watch_list(watch_list_id, settings)
        elif op == "delete":
            _need(watch_list_id=watch_list_id)
            result = client.delete_watch_list(watch_list_id)
        elif op == "filters":
            result = {"filters": client.get_watch_list_filters(type=watch_type)}
        elif op == "filter_values":
            _need(filter_id=filter_id)
            result = {"values": client.get_watch_list_filter_values(
                filter_id, query=query)}
        elif op == "library":
            result = {"library": client.get_watch_list_library()}
        elif op == "history":
            _need(watch_list_id=watch_list_id)
            result = {"history": client.get_watch_list_history(
                watch_list_id, page=page, limit=limit)}
        elif op == "signals":
            result = {"signals": client.get_signals(
                page=page, limit=limit, type=watch_type, status=status,
                watch_list_id=watch_list_id, sort_by=sort_by,
                sort_order=sort_order, received_at_from=received_from,
                received_at_to=received_to)}
        elif op == "push_signals":
            _need(watch_list_id=watch_list_id, contact=contact, company=company)
            result = client.push_external_signals(
                watch_list_id, contact=contact, company=company,
                custom_fields=custom_fields)
        else:
            raise _bad(
                f'unknown op "{op}" — expected: list, create, update, delete, '
                "filters, filter_values, library, history, signals, push_signals")
        return result

    @mcp.tool(annotations=LECTURE)
    def lemlist_database(
        op: Literal["people", "companies", "filters", "personas"],
        filters: Optional[list[dict]] = None,
        search: Optional[str] = None,
        excludes: Optional[list[str]] = None,
        page: Optional[int] = None,
        size: Optional[int] = None,
        mode: Optional[str] = None,
        fields: Optional[list[str]] = None,
        compact: bool = False,
    ) -> dict:
        """Search lemlist's SHARED prospecting database. Reads only.

        Distinct from `lemlist_contact`/`lemlist_company`, which hold only YOUR
        data. Read `op="filters"` first: the filter vocabulary is server-side,
        and a `filters` payload built from guesswork comes back empty. Every
        call spends the quota of your lemlist key. Saving or deleting a persona
        is `lemlist_act`.

        Records are large (3 people ≈ 228k characters): pass `compact=True`
        unless you need a field it drops.

        Args by op:
        - `people`: `filters`, `search`, `excludes`, `page`, `size`, `fields`,
          `compact`.
        - `companies`: `filters`, `page`, `size`, `fields`, `compact`.
        - `filters`: no argument — the available filters.
        - `personas`: optional `mode` — the saved personas.

        Shaping each record of `results` (`total`, `page`, `size` kept):
        - `compact=True`: the short view — identity, title, LinkedIn, current
          positions only, headcount, industry, country, latest job posts. The
          page names what it left out in `omitted_fields`.
        - `fields`: keep only these keys, a nested one by a dotted path
          (`experiences.title`). With `compact=True`, each one named replaces
          the short view's key of the same first level.
        - Neither: the page as lemlist sends it.
        """
        client, is_platform = _client()
        # Passed only when asked for: a call without a view stays the call lemlist
        # has always received, whatever the installed client.
        view = {}
        if fields is not None:
            view["fields"] = fields
        if compact:
            view["compact"] = compact
        if op in ("people", "companies"):
            try:
                if op == "people":
                    result = client.search_people_database(
                        filters=filters, page=page, size=size, excludes=excludes,
                        search=search, **view)
                else:
                    result = client.search_companies_database(
                        filters=filters, page=page, size=size, **view)
            except ValueError as e:
                # A malformed `fields` or `compact`: the client refuses it before any
                # request. Raised bare, it would reach the agent as an internal error.
                # Only then: `requests` has ValueErrors of its own (a bad URL, a body
                # that is not JSON), which say nothing about the caller's arguments.
                if not view or isinstance(e, requests.RequestException):
                    raise
                raise _bad(str(e)) from e
            except RuntimeError as e:
                if not view or not _raised_by_database_view(e):
                    raise
                raise _page_without_results(op) from e
        elif op == "filters":
            result = {"filters": client.get_database_filters()}
        elif op == "personas":
            result = {"personas": client.list_personas(mode=mode)}
        else:
            raise _bad(f'unknown op "{op}" — expected: people, companies, filters, '
                       "personas")
        return result

    # --- Team, mailboxes, deliverability, webhooks -----------------------------------

    @mcp.tool()
    def lemlist_team(
        op: Literal["team", "credits", "senders", "user", "user_channels",
                    "crm_users", "crm_filters", "fields"],
        user_id: Optional[str] = None,
        state: Optional[str] = None,
        crm: Optional[str] = None,
        filter_type: Optional[str] = None,
        entity: Optional[str] = None,
        source: Optional[str] = None,
    ) -> dict:
        """The workspace itself: team, credits, senders, users, CRM link, fields.

        Where the ids other tools ask for come from.

        Args by op:
        - `team` / `credits`: no argument (credits are what enrichment spends).
        - `senders`: optional `state` (campaign status) — members and the
          campaigns they send from.
        - `user`: `user_id`. `user_channels`: no argument — the mailboxes,
          LinkedIn and WhatsApp accounts the key's user can send on, i.e. the
          ids `lemlist_inbox_send` requires.
        - `crm_users`: the users connected to a CRM. `crm_filters`: `crm`
          (hubspot, salesforce…) + `user_id`, optional `filter_type` (`lead`,
          `contact`, `report`) — the selection `lemlist_lead(op="import_crm")`
          imports from.
        - `fields`: optional `entity` (`contact`/`company`) and `source`
          (`default`/`custom`/`crm_synced`) — which custom fields exist.
        """
        client, is_platform = _client()
        if op == "team":
            result = client.get_team()
        elif op == "credits":
            result = client.get_team_credits()
        elif op == "senders":
            result = {"senders": client.get_team_senders(state=state)}
        elif op == "user":
            _need(user_id=user_id)
            result = client.get_user(user_id)
        elif op == "user_channels":
            result = {"channels": client.get_user_channels()}
        elif op == "crm_users":
            result = {"users": client.get_team_crm_users()}
        elif op == "crm_filters":
            _need(crm=crm, user_id=user_id)
            result = {"filters": client.get_crm_filters(
                crm=crm, user_id=user_id, type=filter_type)}
        elif op == "fields":
            result = {"fields": client.list_fields(entity=entity, source=source)}
        else:
            raise _bad(f'unknown op "{op}" — expected: team, credits, senders, user, '
                       "user_channels, crm_users, crm_filters, fields")
        return result

    @mcp.tool()
    def lemlist_mailbox(
        op: Literal["connect", "disconnect", "test", "lemwarm",
                    "lemwarm_update", "lemwarm_start", "lemwarm_pause"],
        email_account_id: Optional[str] = None,
        mailbox_id: Optional[str] = None,
        smtp_imap: Optional[dict] = None,
        settings: Optional[dict] = None,
    ) -> dict:
        """Mailboxes: connect/disconnect/test, and lemwarm (deliverability
        warm-up).

        Args by op:
        - `connect`: `smtp_imap` — one dict carrying `sender_name`,
          `sender_email`, `smtp_host`, `smtp_port`, `smtp_login`,
          `smtp_password`, `imap_host`, `imap_port`, `imap_login`,
          `imap_password` (all required), plus `smtp_secure`, `imap_secure`,
          `user_id`. ⚠️ MAILBOX CREDENTIALS travel here.
        - `disconnect` / `test`: `email_account_id`. Disconnecting stops the
          campaigns sending from it.
        - `lemwarm` / `lemwarm_update` (`settings`: `warmEmailMax`,
          `warmEmailRampup`, `answerPercentage`…) / `lemwarm_start` /
          `lemwarm_pause`: `mailbox_id`.

        `lemwarm_start` DOES send — inside the warm-up network (other lemlist
        mailboxes), never to a prospect. That is why it sits in a visible tool
        while the prospect-facing sends do not.
        """
        client, is_platform = _client()
        if op == "connect":
            _need(smtp_imap=smtp_imap)
            result = client.connect_email_account(**smtp_imap)
        elif op in ("disconnect", "test"):
            _need(email_account_id=email_account_id)
            result = (client.disconnect_email_account(email_account_id)
                      if op == "disconnect"
                      else client.test_email_account(email_account_id))
        elif op in ("lemwarm", "lemwarm_update", "lemwarm_start", "lemwarm_pause"):
            _need(mailbox_id=mailbox_id)
            if op == "lemwarm":
                result = client.get_lemwarm_settings(mailbox_id)
            elif op == "lemwarm_update":
                _need(settings=settings)
                result = client.update_lemwarm_settings(mailbox_id, settings)
            elif op == "lemwarm_start":
                result = client.start_lemwarm(mailbox_id)
            else:
                result = client.pause_lemwarm(mailbox_id)
        else:
            raise _bad(f'unknown op "{op}" — expected: connect, disconnect, test, '
                       "lemwarm, lemwarm_update, lemwarm_start, lemwarm_pause")
        return result

    @mcp.tool()
    def lemlist_deliverability(
        op: Literal["list", "get", "create", "update", "delete"],
        alert_id: Optional[str] = None,
        widget: Optional[str] = None,
        metric: Optional[str] = None,
        severity: Optional[str] = None,
        scope: Optional[str] = None,
        threshold: Optional[float] = None,
        comparison_operator: Optional[str] = None,
        period_days: Optional[int] = None,
        period_mode: Optional[str] = None,
        scope_entities: Optional[list[str]] = None,
        channel_config: Optional[dict] = None,
        recheck_delay_hours: Optional[int] = None,
        settings: Optional[dict] = None,
    ) -> dict:
        """Deliverability alerts — fire when inbox rate, spam rate, score,
        delivery or bounce rate crosses a threshold.

        Args by op:
        - `list` / `get` (`alert_id`) / `delete` (`alert_id`).
        - `create`: `widget` (`warmup`/`outreach`), `metric` (`inboxRate`,
          `spamRate`, `score`, `deliveryRate`, `bounceRate`), `severity`
          (`warning`/`critical`), `scope` (`global`/`mailbox`/`domain`),
          `threshold`, `comparison_operator` (`equal`/`below`/`above`),
          `period_days`, `period_mode` (`rolling`/`consecutive`) — all
          required; then `scope_entities`, `channel_config` (in-app only when
          omitted; `email` needs at least one address), `recheck_delay_hours`.
        - `update`: `alert_id` + `settings` (`threshold`, `enabled`,
          `channelConfig`…).
        """
        client, is_platform = _client()
        if op == "list":
            result = {"alerts": client.list_deliverability_alerts()}
        elif op == "get":
            _need(alert_id=alert_id)
            result = client.get_deliverability_alert(alert_id)
        elif op == "create":
            _need(widget=widget, metric=metric, severity=severity, scope=scope,
                  comparison_operator=comparison_operator,
                  period_days=period_days, period_mode=period_mode)
            if threshold is None:
                raise _bad("`threshold` required")
            result = client.create_deliverability_alert(
                widget=widget, metric=metric, severity=severity, scope=scope,
                threshold=threshold, comparison_operator=comparison_operator,
                period_days=period_days, period_mode=period_mode,
                scope_entities=scope_entities, channel_config=channel_config,
                recheck_delay_hours=recheck_delay_hours)
        elif op == "update":
            _need(alert_id=alert_id, settings=settings)
            result = client.update_deliverability_alert(alert_id, settings)
        elif op == "delete":
            _need(alert_id=alert_id)
            result = client.delete_deliverability_alert(alert_id)
        else:
            raise _bad(f'unknown op "{op}" — expected: list, get, create, update, delete')
        return result

    @mcp.tool()
    def lemlist_webhook(
        op: Literal["list", "add", "delete"],
        hook_id: Optional[str] = None,
        target_url: Optional[str] = None,
        event_type: Optional[str] = None,
        secret: Optional[str] = None,
        campaign_id: Optional[str] = None,
        is_first: Optional[bool] = None,
        zap_id: Optional[str] = None,
    ) -> dict:
        """Webhooks — push lemlist events to a URL of yours (`/hooks`).

        Args by op:
        - `list`: no argument.
        - `add`: `target_url` + optional `event_type` (ONE event:
          `emailsReplied`, `linkedinInviteAccepted`, `enrichmentDone`,
          `signalRegistered`… ~70 of them; omit to subscribe to all),
          `secret`, `campaign_id` (narrow to one campaign), `is_first` (first
          occurrence per lead only), `zap_id` (tag the hook as coming from a
          given Zap).
        - `delete`: `hook_id`.
        """
        client, is_platform = _client()
        if op == "list":
            result = {"webhooks": client.list_webhooks()}
        elif op == "add":
            _need(target_url=target_url)
            result = client.add_webhook(
                target_url, type=event_type, secret=secret,
                campaign_id=campaign_id, is_first=is_first, zap_id=zap_id)
        elif op == "delete":
            _need(hook_id=hook_id)
            result = client.delete_webhook(hook_id)
        else:
            raise _bad(f'unknown op "{op}" — expected: list, add, delete')
        return result

    @mcp.tool()
    def lemlist_delete_activity_transcript(activity_id: str) -> dict:
        """Delete the recording transcript attached to a call activity.

        Its own tool because it is the only destructive gesture on the activity
        log — a transcript deleted here is not recoverable through the API.
        """
        client, is_platform = _client()
        result = client.delete_activity_recording_transcript(activity_id)
        return result
