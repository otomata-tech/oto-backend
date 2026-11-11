"""Gmail — oto-core surface (GmailClient) exposed per user, multi-account.

Each user connects one or more Google accounts on
`https://manage.oto.cx/` (Google section) through the unified OAuth flow (scope
`gmail.modify`). The `gmail_*` tools act on the account the call names
(`_account=`, or the `account` parameter — the email address), otherwise the one
the project pins, otherwise the default: `auth/google.credentials_for`.

No platform key: access is strictly per-user via OAuth (like the
datastore and WhatsApp), so no `resolve_api_key` here.

**Two tools (17/10/2026, Alexis's decision, for security)** — an MCP client authorizes
TOOLS, not arguments, so what reads never sends nor deletes:
- `gmail` reads or simulates: accounts, search, get, attachment, drafts, and `op="draft"`
  (writes a draft: nothing leaves, nothing is destroyed) — `lecture.SIMULATION`;
- `gmail_act` acts: `op="send"` (an existing draft by `draft_id`, or a full message),
  `op="archive"`, `op="trash"`, `op="delete_draft"` (destroys a draft by its `draft_id`) —
  `lecture.ACTE`, the tool the client asks to confirm.
The old names (`gmail_message`, `gmail_compose`, `gmail_list_accounts`) refuse by naming
the call to make (`outils_retires.RENOMMAGES_ACT`).
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from typing import Literal, Optional, Union
from urllib.parse import quote

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, file_content, file_source, session_org
from .lecture import ACTE, SIMULATION, documente
from ..auth import google as google_oauth

# The ops of each tool. Single source: input validation AND the refusal message derive
# from it, so an added op cannot be accepted without being announced (nor the reverse).
_OPS = ("search", "get", "attachment", "drafts", "draft", "accounts")
_ACT_OPS = ("send", "archive", "trash", "delete_draft")


def _ops_error(ops: tuple) -> str:
    return "op must be " + ", ".join(f"'{o}'" for o in ops[:-1]) + f" or '{ops[-1]}'"


def _is_not_found(e: Exception) -> bool:
    """A Google API 404 (googleapiclient `HttpError`: `resp.status`)."""
    return getattr(getattr(e, "resp", None), "status", None) == 404


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _need(value, name: str, op: str):
    """Required argument for THIS op — actionable error, never a fallback.

    An EMPTY value counts as absent: `message_ids=[]` on `op='trash'`
    would return a `{"trashed": []}` that passes for a success although nothing was
    requested, and `query=""` on `op='search'` would sweep the whole mailbox.
    """
    if value is None or (isinstance(value, (str, list)) and not value):
        raise _bad(f"op='{op}' requires {name}")
    return value


def _client_for_user(account: Optional[str] = None):
    """Instantiates an oto-core GmailClient with the user's credentials.

    `account` (email) targets a specific account; None = default account.
    Raises an actionable McpError if no Google account is connected.
    """
    sub = access.current_user_sub_or_raise()
    try:
        creds = google_oauth.credentials_for(sub, account=account, service="gmail")
    except RuntimeError as e:
        raise _bad(str(e))
    from oto.tools.google.gmail.lib.gmail_client import GmailClient
    return GmailClient(credentials=creds)


_GOOGLE_CLIENT_TIMEOUT_S = 20
# oto-backend#867 lot 2 — `_client_for_user` can trigger a token refresh
# (`google_oauth.credentials_for` → `_refresh_access_token`, synchronous HTTP
# 15s), inside an `async def` handler: off the loop + bounded, same method as the
# Unipile identity list (lot 1) and the FOD routes (lot 2). The Gmail API calls
# are already in `to_thread` — only the client construction (hence the refresh)
# still ran in the loop.
async def _client_for_user_async(account: Optional[str] = None):
    try:
        return await asyncio.wait_for(asyncio.to_thread(_client_for_user, account),
                                      timeout=_GOOGLE_CLIENT_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise _bad(f"Google did not respond within {_GOOGLE_CLIENT_TIMEOUT_S}s "
                   "(token refresh) — retry.")


_ATTACHMENTS_TIMEOUT_S = 90


def _resolve_attachments(attachments):
    """Resolves `file_source` refs into TEMPORARY files (the GmailClient expects
    local PATHS for its attachments, but the server does not have the user's disk).
    `attachments` = list of `{"kind":"drive|gmail|url|project_file", …}` (see
    file_source.resolve). Returns `(paths, cleanup)` — the caller MUST call
    `cleanup()` in a finally. Raises FileSourceError on an unreadable ref (first
    cleans up the temp already written)."""
    if not attachments:
        return [], (lambda: None)
    tmpdir = tempfile.mkdtemp(prefix="oto-gmail-att-")

    def cleanup():
        shutil.rmtree(tmpdir, ignore_errors=True)

    try:
        paths = []
        for i, src in enumerate(attachments):
            rf = file_source.resolve(src)
            # defensive basename: never let a filename traverse the tmpdir.
            name = os.path.basename(rf.filename or "") or f"attachment-{i}"
            path = os.path.join(tmpdir, name)
            with open(path, "wb") as f:
                f.write(rf.data)
            paths.append(path)
        return paths, cleanup
    except Exception:
        cleanup()
        raise


def _signed_html(body: str, html: Optional[str], markdown: bool, signature: str) -> str:
    """The message's HTML body, with the account's signature appended after `--`.

    The Gmail API NEVER appends the signature: it is the web client that adds it at
    composition (oto#178, feedback 794/795). The signature is HTML, so the body
    must be too: `html` as is, otherwise the rendered markdown — the same rendering
    GmailClient would do itself —, otherwise the escaped plain text, line breaks kept
    (`markdown=False` means "no markdown", not "no HTML")."""
    if html is not None:
        corps = html
    elif markdown:
        from oto.tools.google.gmail.lib.gmail_client import _markdown_to_html_fragment
        corps = _markdown_to_html_fragment(body)
    else:
        import html as html_mod
        corps = ('<div dir="ltr">' + html_mod.escape(body).replace("\n", "<br>")
                 + "</div>")
    if _porte_son_separateur(signature):
        return f"{corps}<br>{signature}"
    return f"{corps}<br>--<br>{signature}"


def _porte_son_separateur(signature: str) -> bool:
    """True when the account's signature already opens with its own `--` (typed by
    the person in Gmail's settings): adding ours would show two in a row (#1345).
    Read on the visible text — tags removed, entities decoded, leading blanks
    (non-breaking spaces included) skipped."""
    import html as html_mod
    import re
    texte = html_mod.unescape(re.sub(r"<[^>]*>", "", signature))
    return texte.lstrip(" \t\r\n\u00a0").startswith("--")


def _lien_brouillon(message_id: Optional[str], account: Optional[str]) -> Optional[str]:
    """The Gmail web link of a draft, under the account that wrote it (#1345): a
    procedure can then note where the draft waits. The account is the one named, or
    the one the call resolved (`_account` of the call trace); None outside an MCP call
    with no account named. The `#drafts?compose=` form is the Gmail web client's own
    address of an open draft, not a documented API contract."""
    email = account or (session_org.current_call_trace() or {}).get("resolved_account")
    if not message_id or not email:
        return None
    return (f"https://mail.google.com/mail/u/{quote(email, safe='@')}/"
            f"#drafts?compose={quote(message_id, safe='')}")


# The composition arguments, shared by `gmail(op="draft")` and `gmail_act(op="send")`:
# written once, served in both descriptions.
_COMPOSITION_ARGS = """\
    body: the message, rendered from markdown to HTML by default.
    to: recipient(s), comma-separated. REQUIRED for a new message (omit when replying).
    subject: subject line (new message only; a reply keeps the thread's subject).
    reply_to: id of the message to reply to. When set, this is a threaded REPLY
        (subject/thread preserved) and `to`/`subject` are ignored.
    cc: carbon-copy recipient(s), comma-separated.
    bcc: blind-copy recipient(s), comma-separated — a new message only.
    html: explicit HTML body (bypasses markdown rendering).
    from_name: optional display name for the From header.
    markdown: render `body` from markdown when `html` is absent (default True).
    sign: append the account's Gmail signature (default True); False writes
        the body alone.
    attachments: files to attach, as `source` refs oto resolves server-side
        (the agent has no local disk). Each item — `kind` selects the origin:
        - Drive: `{"kind":"drive","file_id":"<id>"}` (id from drive_list/metadata)
        - Gmail: `{"kind":"gmail","message_id":"<id>","filename":"<name>"}`
        - URL:   `{"kind":"url","url":"https://…"}` — e.g. a signed URL from
          `oto_upload_url` (upload a local PDF first) or drive_download. Named
          by the server's `Content-Disposition`, else the path; `"filename"`
          forces the name (its extension types the file).
        - Project file: `{"kind":"project_file","project_id":<id>,"file_id":<id>}`
          (ids from oto_project_files op=list)
    account: email of the Google account to use — same choice as `_account`."""

_SIGNATURE_DOC = """\
The sending account's Gmail signature is appended by default, after `--` (none added when
the signature already opens with its own), like the Gmail web client does (the API never adds it on its own) — so do NOT write a sign-off block of
your own in `body`. `signature` in the answer says what happened: "appended",
"none_configured" (the account has no signature — nothing was added), or "disabled"
(`sign=False`)."""

_GMAIL_DOC = f"""The user's Gmail mailbox — READ it, or prepare a DRAFT. Nothing here sends,
archives or deletes: that is `gmail_act`, the tool your client asks you to confirm.

`op`:
- **"search"** (default): search with Gmail query syntax. `query` e.g.
  `from:foo@bar.com is:unread newer_than:7d`. Returns
  {{messages: [{{id, threadId, from, subject, date, snippet, labelIds}}], count}}.
- **"get"**: a full message (headers, body, attachment metadata) by `message_id`.
- **"attachment"**: the CONTENT of an attachment of `message_id`, by `filename` (from
  op="get"; `index` breaks a tie between same-named ones). Small text (JSON/CSV/Markdown/
  plain, ≤256 KB) comes back inline as `{{encoding: "text", content}}`; a PDF as its
  extracted text plus `raw_url`, a short-lived signed URL to the original (a scanned or
  protected PDF has no text: `text_unavailable` says why); an .xlsx as CSV per sheet
  (`sheet`, `max_rows` — default 200, max 5000; if `truncated`, ask one sheet or raise
  `max_rows`; `raw_url` always gives the original file). An image (png/jpeg/gif/webp,
  ≤3 MB) and a scanned PDF whose pages are JPEG (≤5 pages, ≤3 MB) come back as IMAGE
  CONTENT you can look at — nothing stored. Anything else, or over those caps, as a
  short-lived signed URL `{{encoding: "url", url, expires_in}}` (`image_unavailable` says
  why an image or scan was not served as image).
- **"drafts"**: the drafts. Returns {{drafts: [{{id, message_id, to, subject, date,
  snippet}}], count}} — a draft `id` is what `gmail_act(op="send", draft_id=…)` sends.
- **"draft"**: WRITE A DRAFT — a new message (`to`, `subject`, `body`) or a threaded reply
  (`reply_to`). It does NOT leave the mailbox. Returns `kind: "draft"`, its `id` (the `draft_id`, distinct from
  `message_id`) and `web_link` (the draft opened in Gmail web, to note where it waits): send it with
  `gmail_act(op="send", draft_id=<id>)` once the person agrees.
  ⚠️ A fresh draft can be MISSING from an already-open Gmail tab for a while (the API has
  it, the client view lags): tell the person to reload or search `in:drafts`.
- **"accounts"**: the Gmail accounts this call can use — the user's own, then the
  mailboxes an admin shared with their team or organization. Returns {{accounts:
  [{{email, is_default, shared}}]}}; pass an `email` as `_account` to act on that mailbox.
  An unknown email is refused, never replaced.

{_SIGNATURE_DOC}

Args:
    op: search (default) | get | attachment | drafts | draft | accounts.
    query: op="search" — Gmail search query.
    message_id: op="get"/"attachment" — the message id (from op="search").
    filename: op="attachment" — name of the attachment (e.g. "Contrat.pdf").
    index: op="attachment" — 0-based tiebreaker between same-named attachments.
    max_results: op="search"/"drafts" — max items to return (default 20).
    sheet: op="attachment" of an .xlsx — sheet name or 0-based index.
    max_rows: op="attachment" of an .xlsx — rows per sheet (default 200, max 5000).
    For op="draft":
{_COMPOSITION_ARGS}
"""

_GMAIL_ACT_DOC = f"""ACT on the user's Gmail mailbox — ⚠️ every op here takes effect: a mail
LEAVES, a message moves. Reading and drafting are `gmail`.

`op` (required):
- **"send"**: send a mail, the mail LEAVES. Either an existing draft, as it stands:
  `draft_id` (from `gmail(op="draft")` or `gmail(op="drafts")`) — the usual path, the
  person could read it first; or a full message: `to`, `subject`, `body` (or `reply_to`
  for a threaded reply), the same arguments as `gmail(op="draft")`. Returns `kind:
  "sent"`, the message `id` and `threadId`. Never report "sent" without reading `kind`.
- **"archive"**: removes the INBOX label from `message_ids`.
- **"trash"**: moves `message_ids` to the trash (Gmail deletes them after 30 days). One
  call per message: a PARTIAL trash is possible and is named — replay only what was not
  attempted. A DRAFT is not trashed this way (Google answers 404): use "delete_draft".
- **"delete_draft"**: permanently deletes a draft (it does not go to the trash — it is
  gone), by `draft_id`. That is the draft `id` returned by `gmail(op="draft")` or
  `gmail(op="drafts")`, NOT its `message_id`. Returns `{{kind: "draft_deleted",
  draft_id}}`.

{_SIGNATURE_DOC} (A draft is sent as it stands: its signature was added when it was
written.)

Args:
    op: send | archive | trash | delete_draft.
    draft_id: op="send" — the draft to send, as it stands (no other argument);
        op="delete_draft" — the draft to delete.
    message_ids: op="archive"/"trash" — the message ids to act on.
    For op="send" without `draft_id`:
{_COMPOSITION_ARGS}
"""

#: The arguments that only make sense when composing — refused elsewhere, never ignored.
_COMPOSITION_KEYS = ("body", "to", "subject", "reply_to", "cc", "bcc", "html",
                     "from_name", "attachments")


def _refuse_composition(op: str, **valeurs) -> None:
    donnes = [k for k, v in valeurs.items() if v is not None]
    if donnes:
        raise _bad(f"{', '.join(f'`{k}`' for k in donnes)} only apply when composing "
                   f"(gmail op='draft', gmail_act op='send'), not op='{op}'. Nothing was "
                   "done.")


async def _composer(client, *, envoyer: bool, body, to, subject, reply_to, cc, bcc, html,
                    from_name, markdown, attachments, sign, account=None) -> dict:
    """Write a draft (`envoyer=False`) or send (`True`) — the ONE composition path of both
    tools: signature, attachments, reply or new message."""
    if not body:
        raise _bad("`body` is required to compose a message. Nothing was written.")
    signature_etat = "disabled"
    if sign:
        try:
            signature = await asyncio.to_thread(client.get_signature)
        except Exception as e:
            # No send WITHOUT the signature silently expected: named refusal,
            # with the gesture that works if the caller accepts doing without it.
            raise _bad(f"Could not read the Gmail signature ({e}) — nothing "
                       "was written. Retry, or pass `sign=False` to "
                       "compose without a signature.")
        if signature:
            html = _signed_html(body, html, markdown, signature)
            signature_etat = "appended"
        else:
            signature_etat = "none_configured"
    if not reply_to and not to:
        raise _bad("`to` is required for a new message (or provide `reply_to` to reply).")
    try:
        # oto-backend#867 lot 2 — each attachment (drive/gmail/url) is
        # resolved by a synchronous HTTP call (file_source.resolve), in series:
        # off the loop + bounded, same method as the token refresh
        # above. 90s and not 20s: a send tolerates waiting for real
        # attachments (up to 25 MB each) — what must not happen
        # is freezing the whole process in the meantime.
        att = await asyncio.wait_for(
            asyncio.to_thread(_resolve_attachments, attachments),
            timeout=_ATTACHMENTS_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise _bad(f"Fetching the attachments took too long "
                   f"(> {_ATTACHMENTS_TIMEOUT_S}s) — retry.")
    except file_source.FileSourceError as e:
        raise _bad(str(e))
    att_paths, _cleanup = att

    def _acte(res: object) -> dict:
        """The return NAMES the act. Without this field, "sent" and "draft" can only
        be told apart by the NUMBER of keys returned (3 vs 2) — a difference you
        must already know about to read it. An agent that reports "draft created" after
        a real send did not lie: it had nothing to read that said so.
        Paid for on 14/08: three emails sent to a client."""
        out = dict(res) if isinstance(res, dict) else {"result": res}
        out["kind"] = "sent" if envoyer else "draft"
        if not envoyer:
            lien = _lien_brouillon(out.get("message_id"), account)
            if lien:
                out["web_link"] = lien
        out["signature"] = signature_etat
        return out

    try:
        if reply_to:
            if not envoyer:
                return _acte(await asyncio.to_thread(
                    lambda: client.create_draft_reply(
                        message_id=reply_to, body=body, html=html, cc=cc, markdown=markdown,
                        attachments=att_paths,
                    )
                ))
            return _acte(await asyncio.to_thread(
                lambda: client.reply(
                    message_id=reply_to, body=body, html=html, cc=cc,
                    from_name=from_name, markdown=markdown, attachments=att_paths,
                )
            ))
        if not envoyer:
            return _acte(await asyncio.to_thread(
                lambda: client.create_draft(
                    to=to, subject=subject or "", body=body, html=html, cc=cc, bcc=bcc,
                    markdown=markdown, attachments=att_paths,
                )
            ))
        return _acte(await asyncio.to_thread(
            lambda: client.send(
                to=to, subject=subject or "", body=body, html=html,
                cc=cc, bcc=bcc, from_name=from_name, markdown=markdown, attachments=att_paths,
            )
        ))
    finally:
        _cleanup()


def register(mcp: FastMCP) -> None:

    @mcp.tool(name="gmail", annotations=SIMULATION)
    @documente(_GMAIL_DOC)
    async def gmail(
        op: Literal["search", "get", "attachment", "drafts", "draft",
                    "accounts"] = "search",
        query: Optional[str] = None,
        message_id: Optional[str] = None,
        filename: Optional[str] = None,
        index: int = 0,
        max_results: int = 20,
        sheet: Optional[Union[int, str]] = None,
        max_rows: Optional[int] = None,
        body: Optional[str] = None,
        to: Optional[str] = None,
        subject: Optional[str] = None,
        reply_to: Optional[str] = None,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
        html: Optional[str] = None,
        from_name: Optional[str] = None,
        markdown: bool = True,
        attachments: Optional[list[dict]] = None,
        sign: bool = True,
        account: Optional[str] = None,
    ) -> Union[dict, ToolResult]:
        # Refusal BEFORE any credential resolution: an unknown op never reaches the client.
        if op not in _OPS:
            raise _bad(_ops_error(_OPS))
        if (sheet is not None or max_rows is not None) and op != "attachment":
            raise _bad(f"`sheet`/`max_rows` only apply to op='attachment' of an "
                       f".xlsx spreadsheet (got op='{op}').")
        if op != "draft":
            _refuse_composition(op, body=body, to=to, subject=subject, reply_to=reply_to,
                                cc=cc, bcc=bcc, html=html, from_name=from_name,
                                attachments=attachments)

        if op == "accounts":
            sub = access.current_user_sub_or_raise()
            accounts = await asyncio.to_thread(
                google_oauth.reachable_accounts, sub, service="gmail")
            return {"accounts": [
                {"email": a.get("google_email"), "is_default": a.get("is_default", False),
                 "shared": a.get("shared")}
                for a in accounts]}

        client = await _client_for_user_async(account)

        if op == "search":
            messages = await asyncio.to_thread(
                client.search, _need(query, "query", op), max_results)
            return {"messages": messages, "count": len(messages)}

        if op == "get":
            return await asyncio.to_thread(
                client.get_message, _need(message_id, "message_id", op))

        if op == "attachment":
            mid = _need(message_id, "message_id", op)
            name = _need(filename, "filename", op)
            try:
                att = await asyncio.to_thread(client.get_attachment, mid, name, index)
            except Exception as e:
                raise _bad(str(e))
            data, att_filename, mime = att["data"], att["filename"], att["mimeType"]
            sub = access.current_user_sub_or_raise()
            try:
                return await asyncio.to_thread(
                    file_content.render_attachment_for_agent, data, att_filename, mime,
                    sub=sub, prefix="gmail-attachments", sheet=sheet, max_rows=max_rows)
            except (file_content.MediaUnavailable, file_content.SpreadsheetError) as e:
                raise _bad(str(e))

        if op == "drafts":
            drafts = await asyncio.to_thread(client.list_drafts, max_results)
            return {"drafts": drafts, "count": len(drafts)}

        # op == "draft" — the only one left (input guard above).
        return await _composer(
            client, envoyer=False, body=body, to=to, subject=subject, reply_to=reply_to,
            cc=cc, bcc=bcc, html=html, from_name=from_name, markdown=markdown,
            attachments=attachments, sign=sign, account=account)

    @mcp.tool(name="gmail_act", annotations=ACTE)
    @documente(_GMAIL_ACT_DOC)
    async def gmail_act(
        op: Literal["send", "archive", "trash", "delete_draft"],
        draft_id: Optional[str] = None,
        message_ids: Optional[list[str]] = None,
        body: Optional[str] = None,
        to: Optional[str] = None,
        subject: Optional[str] = None,
        reply_to: Optional[str] = None,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
        html: Optional[str] = None,
        from_name: Optional[str] = None,
        markdown: bool = True,
        attachments: Optional[list[dict]] = None,
        sign: bool = True,
        account: Optional[str] = None,
    ) -> dict:
        if op not in _ACT_OPS:
            raise _bad(_ops_error(_ACT_OPS))
        if op == "delete_draft":
            _need(draft_id, "draft_id", op)
            if message_ids is not None:
                raise _bad("`message_ids` does not apply to op='delete_draft': pass the "
                           "draft's `draft_id` (its `id` in gmail op='draft'/'drafts'). "
                           "Nothing was done.")
            _refuse_composition(op, body=body, to=to, subject=subject, reply_to=reply_to,
                                cc=cc, bcc=bcc, html=html, from_name=from_name,
                                attachments=attachments)
        elif op != "send":
            if draft_id is not None:
                raise _bad(f"`draft_id` only applies to op='send'/'delete_draft', not "
                           f"op='{op}'. Nothing was done.")
            _refuse_composition(op, body=body, to=to, subject=subject, reply_to=reply_to,
                                cc=cc, bcc=bcc, html=html, from_name=from_name,
                                attachments=attachments)
        elif message_ids is not None:
            raise _bad("`message_ids` only applies to op='archive'/'trash', not "
                       "op='send'. Nothing was sent.")
        elif draft_id is not None:
            # A draft is sent AS IT STANDS: an argument beside it would be silently
            # ignored — and the person would believe it was applied.
            _refuse_composition("send with draft_id", body=body, to=to, subject=subject,
                                reply_to=reply_to, cc=cc, bcc=bcc, html=html,
                                from_name=from_name, attachments=attachments)

        client = await _client_for_user_async(account)

        if op == "send":
            if draft_id is not None:
                sent = await asyncio.to_thread(client.send_draft, draft_id)
                return {**sent, "kind": "sent", "draft_id": draft_id}
            return await _composer(
                client, envoyer=True, body=body, to=to, subject=subject,
                reply_to=reply_to, cc=cc, bcc=bcc, html=html, from_name=from_name,
                markdown=markdown, attachments=attachments, sign=sign)

        if op == "delete_draft":
            await asyncio.to_thread(client.delete_draft, draft_id)
            return {"kind": "draft_deleted", "draft_id": draft_id}

        ids = list(_need(message_ids, "message_ids", op))
        if op == "archive":
            return {"archived": await asyncio.to_thread(client.archive_messages, ids)}

        # op == "trash". Gmail has no batch trash: it is one call per message, hence a
        # loop — and hence a PARTIAL write is possible. If the 3rd fails, the first two
        # ARE in the trash; letting the bare exception bubble up would only tell the agent
        # "failed", it would conclude "nothing went through" and replay a write already
        # done (signal #227, same fault as #600). We name the three batches.
        trashed: list = []
        for i, mid in enumerate(ids):
            try:
                res = await asyncio.to_thread(client.trash_message, mid)
            except Exception as e:  # noqa: BLE001 — re-raised named, with the real state
                restants = ids[i + 1:]
                if _is_not_found(e):
                    raise _bad(
                        f"Google does not know `{mid}` as a message (404). If it is a "
                        "DRAFT, it is not trashed this way: call "
                        "gmail_act(op='delete_draft', draft_id=<the draft `id` from "
                        "gmail op='draft'/'drafts', not its `message_id`>). "
                        f"ALREADY in the trash ({len(trashed)}): {trashed}. "
                        f"NOT ATTEMPTED ({len(restants)}): {restants}."
                    ) from e
                raise _bad(
                    "PARTIAL trash — the write stops at the first "
                    "failure, but what precedes did happen. "
                    f"ALREADY in the trash ({len(trashed)}): {trashed}. "
                    f"FAILED on `{mid}`: {type(e).__name__}: {e}. "
                    f"NOT ATTEMPTED ({len(restants)}): {restants}. "
                    "Only replay the not-attempted ones — retrying the first "
                    "ones is not necessary."
                ) from e
            trashed.append(res.get("id", mid))
        return {"trashed": trashed}
