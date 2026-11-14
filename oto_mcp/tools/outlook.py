"""Outlook — a person's Microsoft 365 mailbox, via Microsoft Graph (`MailClient`).

Credential = the PERSON's Microsoft 365 account (carrier `microsoft`, OAuth, delegated
permissions) that has authorized THIS service (scopes `MAIL`), acquired and renewed by
`auth/microsoft.py`; several linked accounts are chosen by the generic `_account=` axis.

**Surface** — the Gmail shape (`tools/gmail.py`), two tools (17/10/2026, Alexis's
decision, for security: an MCP client authorizes TOOLS, not arguments):
- `outlook` reads or simulates (search/get/attachment/drafts/folders, and `op="draft"`,
  which writes a draft: nothing leaves, nothing is destroyed) — `lecture.SIMULATION`;
- `outlook_act` acts (send/archive/trash/move) — `lecture.ACTE`, the tool the client asks
  to confirm. `op="send"` sends an existing draft by `draft_id`, or a full message, which
  goes through a draft (`create_draft` then `send_draft`, then `sent_message`: the draft's
  immutable id designates its copy in Sent Items, read back for its `webLink` — sending is
  asynchronous, the copy may not be there yet).
  The body is markdown (`body`, rendered as `gmail` renders it) or HTML sent
  AS IS (`html`, or `html_source` — a `file_source` ref whose content is the HTML):
  nothing rewrites or cleans an HTML body here, what Outlook shows is what was given.
The old names (`outlook_message`, `outlook_compose`) refuse by naming the call to make
(`outils_retires.RENOMMAGES_ACT`).

Protocol facts the tools carry (`oto.tools.microsoft.mail`): `$search` and `$filter`
exclude each other (`query` and `unread` are not combined); every id is IMMUTABLE
(the client asks Graph for them on every call): it survives a move to another folder
(archive, trash and move all go through `move`) and a draft's move to Sent Items once
sent — the answer of a move still gives both ids, `new_id` being the stable form of an
id given in Outlook's older format; an attachment
travels inline up to 3 MB, and one with a `content_id` is an inline image (`cid:`);
Graph does not expose the Outlook signature, so nothing is appended to a body.

⚠️ `outlook_act` WRITES to the person's mailbox: send, archive, trash, move. `outlook`
writes only drafts; its default is `op="search"` — a READ.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Literal, Optional, Union

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult

from .. import access, file_content, file_source, output_projection
from ._microsoft_graph import bad, markdown_html, need, raw, refuse_ignored, run, token
from .lecture import ACTE, SIMULATION, documente

logger = logging.getLogger(__name__)

#: The Microsoft service these tools call: its scopes, its card (`auth/microsoft`).
_SERVICE = "outlook"
_LABEL = "Outlook"

_OPS = ("search", "get", "attachment", "drafts", "folders", "draft")
_ACT_OPS = ("send", "archive", "trash", "move")


def _ops_error(ops: tuple) -> str:
    return "op must be " + ", ".join(f"'{o}'" for o in ops[:-1]) + f" or '{ops[-1]}'"

#: The well-known folders the moving ops go to (`mail.py`: id or well-known name).
_DESTINATIONS = {"archive": "archive", "trash": "deleteditems"}
#: Ceiling of an HTML body read from `html_source` — far above any real email, far
#: below Graph's 4 MB request.
_HTML_MAX_BYTES = 1024 * 1024


def _adresse(r: Optional[dict]) -> Optional[str]:
    """A Graph recipient → `Name <address>` (or the address alone)."""
    e = (r or {}).get("emailAddress") or {}
    adresse, nom = e.get("address"), e.get("name")
    if not adresse:
        return nom
    return f"{nom} <{adresse}>" if nom and nom != adresse else adresse


def _message(m: dict) -> dict:
    """A message's view: enough to recognize it, answer it, find it again."""
    return {
        "id": m.get("id"),
        "subject": m.get("subject"),
        "from": _adresse(m.get("from")),
        "to": [_adresse(r) for r in m.get("toRecipients") or []],
        "cc": [_adresse(r) for r in m.get("ccRecipients") or []],
        "date": m.get("receivedDateTime") or m.get("sentDateTime"),
        "isRead": m.get("isRead"),
        "isDraft": m.get("isDraft"),
        "hasAttachments": m.get("hasAttachments"),
        "importance": m.get("importance"),
        "preview": m.get("bodyPreview"),
        "conversationId": m.get("conversationId"),
        "folder_id": m.get("parentFolderId"),
        "webLink": m.get("webLink"),
    }


def _piece(a: dict) -> dict:
    return {k: a.get(k) for k in ("id", "name", "contentType", "size", "isInline")}


def _dossier(f: dict) -> dict:
    return {k: f.get(k) for k in ("id", "displayName", "totalItemCount",
                                  "unreadItemCount", "childFolderCount")}


def _attachments(sources: Optional[list], plafond: int) -> list[dict]:
    """The `source` refs (`file_source`: drive, gmail, url, project_file) as the bytes
    `MailClient.create_draft` takes — resolved server-side, each bounded to what Graph
    accepts inline, BEFORE anything is written. A ref's `content_id` goes to the lib,
    which makes it an inline attachment and checks it (before any write too)."""
    out = []
    for i, src in enumerate(sources or ()):
        try:
            rf = file_source.resolve(src, max_bytes=plafond)
        except file_source.FileSourceError as e:
            raise bad(f"attachment #{i + 1} could not be read ({e}). Outlook attaches "
                      f"files up to {plafond // (1024 * 1024)} MB each. Nothing was "
                      "written.")
        piece = {"name": os.path.basename(rf.filename or "") or f"attachment-{i + 1}",
                 "content_type": rf.mime, "content_bytes": rf.data}
        if src.get("content_id") is not None:
            piece["content_id"] = src["content_id"]
        out.append(piece)
    return out


def _check_one_body(body: Optional[str], html: Optional[str],
                    html_source: Optional[dict]) -> None:
    """Exactly one of the three ways to give the body — two would leave one unsaid."""
    donnes = [nom for nom, v in (("body", body), ("html", html),
                                 ("html_source", html_source)) if v is not None]
    if len(donnes) != 1:
        raise bad("Give exactly one of `body` (markdown), `html` (HTML sent as is) or "
                  f"`html_source` (a ref to an HTML file) — got "
                  f"{', '.join(f'`{n}`' for n in donnes) or 'none'}. Nothing was written.")
    if html is not None and not html.strip():
        raise bad("`html` is empty. Nothing was written.")


def _body_html(body: Optional[str], html: Optional[str],
               html_source: Optional[dict]) -> str:
    """The HTML that goes to Graph: `body` rendered from markdown, `html` untouched,
    or the UTF-8 text of `html_source`, untouched."""
    if body is not None:
        return markdown_html(body, cid=True)
    if html is not None:
        return html
    try:
        rf = file_source.resolve(html_source, max_bytes=_HTML_MAX_BYTES)
    except file_source.FileSourceError as e:
        raise bad(f"`html_source` could not be read ({e}). An HTML body is limited to "
                  f"{_HTML_MAX_BYTES // (1024 * 1024)} MB. Nothing was written.")
    try:
        texte = rf.data.decode("utf-8")
    except UnicodeDecodeError:
        raise bad(f"`html_source` ({rf.filename}, {rf.mime}) is not UTF-8 text: give the "
                  "HTML file itself, encoded in UTF-8. Nothing was written.") from None
    if not texte.strip():
        raise bad(f"`html_source` ({rf.filename}) is empty. Nothing was written.")
    return texte


# The composition arguments, shared by `outlook(op="draft")` and `outlook_act(op="send")`:
# written once, served in both descriptions.
_COMPOSITION_DOC = """\
A new message needs `to` (and usually `subject`). A reply is `reply_to=<message id>`:
Outlook sets the recipients and the subject itself and quotes the original below your
text; `reply_all=True` answers every recipient. `to`, `cc`, `bcc` and `subject` are
refused on a reply; `attachments` (inline images included) are accepted.

**The body — exactly one of three.** `body` is markdown, rendered as HTML (the `gmail`
rendering): the choice for an ordinary message. `html` is an HTML body sent AS IS — no
markdown pass, no rewriting, no cleanup: use it when the layout matters (a branded email,
a newsletter, styled tables), or to send an HTML file someone prepared. `html_source` is
the same, read from a file: a `source` ref (same kinds as `attachments`) whose content is
the HTML, UTF-8, up to 1 MB. ⚠️ Outlook renders HTML with Word's engine, which ignores
much modern CSS: before writing HTML for Outlook, read the oto guide "Mails HTML
compatibles Outlook" (`oto_guide op=read slug=outlook-html`).

**Images in the body**: an attachment with a `content_id` (e.g.
`{"kind":"project_file","project_id":…,"file_id":…,"content_id":"logo"}`) is an INLINE
image, shown where the HTML says `<img src="cid:logo">` — not a link to a file; the id is
unique in the message, without `<>` or spaces. A PDF or any other file stays an ordinary
attachment (no `content_id`).

**No signature is appended**: Microsoft Graph does not expose the Outlook signature —
write the sign-off in the body if one is wanted."""

_COMPOSITION_ARGS = """\
    body: the message, in markdown — or `html` / `html_source`, not several.
    html: the message as HTML, sent exactly as given (a reply too: it goes above the
        quoted message).
    html_source: a `source` ref to the HTML file (`{"kind":"project_file",
        "project_id":…,"file_id":…}`, `{"kind":"url","url":…}`, drive, gmail), UTF-8,
        ≤ 1 MB, sent exactly as read.
    to: recipient addresses — a new message only.
    cc: carbon-copy addresses — a new message only.
    bcc: blind-copy addresses — a new message only.
    subject: the subject line — a new message only.
    reply_to: id of the message to reply to (from `outlook`).
    reply_all: with `reply_to`, answer every recipient (default False).
    attachments: files to attach (a new message or a reply), as `source` refs oto
        resolves server-side, up to 3 MB each: `{"kind":"drive","file_id":…}`,
        `{"kind":"gmail","message_id":…,"filename":…}`, `{"kind":"url","url":…}`
        (e.g. a signed URL from `oto_upload_url`; `"filename"` forces its name), `{"kind":"project_file",
        "project_id":…,"file_id":…}`. Add `"content_id":"<id>"` to make one an
        inline image of the body (`<img src="cid:<id>">`)."""

_OUTLOOK_DOC = f"""The person's Outlook mailbox — READ it, or prepare a DRAFT. Nothing here
sends, archives, trashes or moves: that is `outlook_act`, the tool your client asks you to
confirm.

`op`:
- **"search"** (default): messages, most recent first, without their body (`preview`
  only). `query` is an Outlook (KQL) search — `from:jane subject:invoice`, plain words;
  `folder` narrows to one folder; `unread` True/False keeps unread/read ones. ⚠️ `unread`
  cannot be combined with `query` (Microsoft refuses it): put the condition in the query,
  or list without one. Returns {{messages: [{{id, subject, from, to, cc, date, isRead,
  hasAttachments, preview, conversationId, folder_id, webLink}}], count}}.
- **"get"**: one message by `message_id`, its body as text, and its attachments (`id`,
  `name`, `contentType`, `size`).
- **"attachment"**: the CONTENT of an attachment of `message_id`, by `attachment_id` or
  `filename` (from op="get"). Small text and PDF come back as text inline, a spreadsheet
  as CSV per sheet (`sheet`, `max_rows`). An image (png/jpeg/gif/webp, ≤3 MB) and a
  scanned PDF whose pages are JPEG (≤5 pages, ≤3 MB) come back as IMAGE CONTENT you can
  look at — nothing stored. Anything else, or over those caps, comes as a short-lived
  signed URL (`image_unavailable` says why an image or scan was not served as image).
- **"drafts"**: the drafts, most recent first — a draft `id` is what
  `outlook_act(op="send", draft_id=…)` sends.
- **"folders"**: the top-level mail folders (`id`, `displayName`, counts) — a folder id
  goes in `folder`, or in `destination` of `outlook_act(op="move")`.
- **"draft"**: WRITE A DRAFT in the Drafts folder — a new message or a reply. It does NOT
  leave the mailbox. Returns `kind: "draft"`, its `id` and its `webLink` (opens it in
  Outlook on the web, for the person to review): send it with
  `outlook_act(op="send", draft_id=<id>)` once the person agrees.

{_COMPOSITION_DOC}

Args:
    op: search (default) | get | attachment | drafts | folders | draft.
    query: op="search" — Outlook (KQL) search, e.g. `from:jane invoice`.
    folder: op="search" — a folder id or a well-known name (`inbox`, `sentitems`,
        `archive`…); every folder when omitted.
    unread: op="search" — True: unread only, False: read only.
    message_id: op="get"/"attachment" — the message id.
    attachment_id: op="attachment" — the attachment id (from op="get").
    filename: op="attachment" — the attachment by name, instead.
    limit: op="search"/"drafts"/"folders" — max items (default 25).
    sheet: op="attachment" of an .xlsx — sheet name or 0-based index.
    max_rows: op="attachment" of an .xlsx — rows per sheet (default 200).
    full: return Graph's raw objects instead of the trimmed view.
    fields: op="search"/"drafts" — keep only these keys of each message.
    For op="draft":
{_COMPOSITION_ARGS}
"""

_OUTLOOK_ACT_DOC = f"""ACT on the person's Outlook mailbox — ⚠️ every op here takes effect: a
mail LEAVES, a message moves. Reading and drafting are `outlook`.

`op` (required):
- **"send"**: send a mail, the mail LEAVES. Either an existing draft, as it stands:
  `draft_id` (from `outlook(op="draft")` or `outlook(op="drafts")`) — the usual path, the
  person could read it first; or a full message, the same arguments as
  `outlook(op="draft")`: it is written as a draft, then sent. Read `kind` — "sent" — before
  reporting. The answer gives the message's `id` and its `webLink`: the copy in Sent
  Items, which keeps the same `id`. Sending is asynchronous: when that copy is not
  visible yet, `webLink` is null and `note` says so — the mail still LEFT.
- **"archive"**: moves `message_ids` to the Archive folder.
- **"trash"**: moves `message_ids` to Deleted Items (recoverable).
- **"move"**: moves `message_ids` to `destination` (a folder id, or `inbox`, `archive`,
  `deleteditems`, `junkemail`…).
A moved message KEEPS its id (oto asks Outlook for immutable ids): the answer gives
`{{id, new_id}}` for each, equal — unless `id` came in Outlook's older format, then
`new_id` is its stable form: use `new_id` afterwards.

A full message follows the rules of `outlook(op="draft")`: a reply sets its own recipients
and subject, exactly one body (`body`, `html` or `html_source`), inline images by
`content_id`, no signature appended.

Args:
    op: send | archive | trash | move.
    draft_id: op="send" — the draft to send, as it stands (no other argument).
    message_ids: op="archive"/"trash"/"move" — the messages to move.
    destination: op="move" — the target folder (id or well-known name).
    For op="send" without `draft_id`:
{_COMPOSITION_ARGS}
"""


def register(mcp: FastMCP) -> None:
    from oto.tools.microsoft import MailClient
    from oto.tools.microsoft.mail import MAX_INLINE_ATTACHMENT

    def _client() -> MailClient:
        """The Graph client of THIS caller, with their current token."""
        return MailClient(token(_SERVICE))

    def _run(fn):
        return run(fn, _LABEL)

    def _deplacer(c: MailClient, ids: list[str], destination: str) -> list[dict]:
        """One `move` per message: a PARTIAL move is possible, and is named — what
        went through, where it failed, what was not attempted (the `gmail_act`
        trash rule: an applied write the caller learns nothing about gets replayed)."""
        faits: list[dict] = []
        for i, mid in enumerate(ids):
            try:
                moved = _run(lambda: c.move(mid, destination))
            except Exception as e:  # noqa: BLE001 — re-raised named, with the real state
                if not faits:
                    raise
                restants = ids[i + 1:]
                raise bad(
                    f"PARTIAL move to `{destination}` — it stops at the first failure, "
                    f"but what precedes did happen. ALREADY moved ({len(faits)}): "
                    f"{faits}. FAILED on `{mid}`: {e}. NOT ATTEMPTED "
                    f"({len(restants)}): {restants}. Only replay the not-attempted "
                    "ones.") from e
            faits.append({"id": mid, "new_id": (moved or {}).get("id")})
        return faits

    def _composer(*, envoyer: bool, body, html, html_source, to, cc, bcc, subject,
                  reply_to, reply_all, attachments) -> dict:
        """Write a draft, then send it when `envoyer` — the ONE composition path of
        `outlook(op="draft")` and `outlook_act(op="send")`."""
        _check_one_body(body, html, html_source)
        if reply_to:
            for nom, valeur in (("to", to), ("cc", cc), ("bcc", bcc), ("subject", subject)):
                if valeur:
                    raise bad(f"`{nom}` is not accepted on a reply: Outlook sets the "
                              "recipients and the subject of a reply itself. Nothing "
                              "was written.")
            contenu = _body_html(body, html, html_source)
            pieces = _attachments(attachments, MAX_INLINE_ATTACHMENT)
            c = _client()
            brouillon = _run(lambda: c.create_reply_draft(
                reply_to, body_html=contenu, reply_all=reply_all, attachments=pieces))
        else:
            if reply_all:
                raise bad("`reply_all` goes with `reply_to` (the message to answer).")
            if not to:
                raise bad("`to` is required for a new message (or `reply_to` to answer "
                          "one). Nothing was written.")
            contenu = _body_html(body, html, html_source)
            pieces = _attachments(attachments, MAX_INLINE_ATTACHMENT)
            c = _client()
            brouillon = _run(lambda: c.create_draft(
                to=to, subject=subject or "", body_html=contenu, cc=cc or (),
                bcc=bcc or (), attachments=pieces))
        out = {"id": brouillon.get("id"), "subject": brouillon.get("subject"),
               "to": [_adresse(r) for r in brouillon.get("toRecipients") or []],
               "cc": [_adresse(r) for r in brouillon.get("ccRecipients") or []],
               "webLink": brouillon.get("webLink")}
        if not envoyer:
            return {"kind": "draft", **out}
        return _envoyer(c, brouillon["id"], out, ecrit_a_l_instant=True)

    def _envoyer(c: MailClient, draft_id: str, out: dict, *, ecrit_a_l_instant: bool) -> dict:
        """Send the draft `draft_id`, then read its copy in Sent Items for its link."""
        try:
            envoye = _run(lambda: c.send_draft(draft_id))
        except Exception as e:  # noqa: BLE001 — re-raised named, with the real state
            if not ecrit_a_l_instant:
                raise
            raise bad(f"The draft {draft_id} was SAVED but sending it "
                      f"failed: {e}. Nothing left the mailbox; retry the send, or "
                      "leave the draft for the person.") from e
        envoi = {"kind": "sent", "id": envoye, "subject": out.get("subject"),
                 "to": out.get("to"), "cc": out.get("cc")}
        # The draft's immutable id designates its copy in Sent Items, once it is there.
        try:
            copie = _run(lambda: c.sent_message(envoye))
        except Exception as e:  # noqa: BLE001 — the mail LEFT: said, not raised
            logger.warning("outlook: mail %s sent, reading its Sent Items copy failed: %s",
                           envoye, type(e).__name__)
            return {**envoi, "webLink": None,
                    "note": f"The mail LEFT, but reading its copy in Sent Items failed "
                            f"({e}): no link to give."}
        if copie is None:
            return {**envoi, "webLink": None,
                    "note": "The mail LEFT; its copy in Sent Items was not visible yet "
                            "(sending is asynchronous), hence no link. It keeps this `id`."}
        return {**envoi, "webLink": copie.get("webLink"),
                "sentDateTime": copie.get("sentDateTime"),
                **({} if out.get("subject") is not None else
                   {"subject": copie.get("subject"),
                    "to": [_adresse(r) for r in copie.get("toRecipients") or []],
                    "cc": [_adresse(r) for r in copie.get("ccRecipients") or []]})}

    def _refuse_composition(op: str, **valeurs) -> None:
        donnes = [k for k, v in valeurs.items() if v not in (None, False)]
        if donnes:
            raise bad(f"{', '.join(f'`{k}`' for k in donnes)} only apply when composing "
                      f"(outlook op='draft', outlook_act op='send'), not op='{op}'. "
                      "Nothing was done.")

    @mcp.tool(name="outlook", annotations=SIMULATION)
    @documente(_OUTLOOK_DOC)
    def outlook(
        op: Literal["search", "get", "attachment", "drafts", "folders",
                    "draft"] = "search",
        query: Optional[str] = None,
        folder: Optional[str] = None,
        unread: Optional[bool] = None,
        message_id: Optional[str] = None,
        attachment_id: Optional[str] = None,
        filename: Optional[str] = None,
        limit: int = 25,
        sheet: Optional[Union[int, str]] = None,
        max_rows: Optional[int] = None,
        full: bool = False,
        fields: Optional[list[str]] = None,
        body: Optional[str] = None,
        html: Optional[str] = None,
        html_source: Optional[dict] = None,
        to: Optional[list[str]] = None,
        cc: Optional[list[str]] = None,
        bcc: Optional[list[str]] = None,
        subject: Optional[str] = None,
        reply_to: Optional[str] = None,
        reply_all: bool = False,
        attachments: Optional[list[dict]] = None,
    ) -> Union[dict, ToolResult]:
        # Refusal BEFORE any credential resolution: an unknown op never reaches the client.
        if op not in _OPS:
            raise bad(_ops_error(_OPS))
        if (sheet is not None or max_rows is not None) and op != "attachment":
            raise bad(f"`sheet`/`max_rows` only apply to op='attachment' (got op='{op}').")
        if fields is not None and op not in ("search", "drafts"):
            raise bad(f"`fields` only applies to op='search'/'drafts' (got op='{op}').")
        if op == "draft":
            refuse_ignored(op, query=query, folder=folder, unread=unread,
                           message_id=message_id, attachment_id=attachment_id,
                           filename=filename)
            return _composer(envoyer=False, body=body, html=html, html_source=html_source,
                             to=to, cc=cc, bcc=bcc, subject=subject, reply_to=reply_to,
                             reply_all=reply_all, attachments=attachments)
        _refuse_composition(op, body=body, html=html, html_source=html_source, to=to,
                            cc=cc, bcc=bcc, subject=subject, reply_to=reply_to,
                            reply_all=reply_all, attachments=attachments)
        vue = raw if full else _message
        c = _client()

        def _liste(messages: list) -> dict:
            out = {"messages": [vue(m) for m in messages], "count": len(messages)}
            return output_projection.project(out, items_path="messages", fields=fields)

        if op == "search":
            refuse_ignored(op, message_id=message_id, attachment_id=attachment_id,
                           filename=filename)
            if query is not None and not query.strip():
                raise bad("`query` is empty — omit it to list the most recent messages.")
            return _liste(_run(lambda: c.search_messages(query, folder=folder, top=limit,
                                                         unread=unread)))

        if op == "drafts":
            refuse_ignored(op, query=query, folder=folder, unread=unread,
                           message_id=message_id, attachment_id=attachment_id,
                           filename=filename)
            return _liste(_run(lambda: c.search_messages(folder="drafts", top=limit)))

        if op == "folders":
            refuse_ignored(op, query=query, folder=folder, unread=unread,
                           message_id=message_id, attachment_id=attachment_id,
                           filename=filename)
            dossiers = _run(lambda: c.list_folders(limit=limit))
            return {"folders": [raw(f) if full else _dossier(f) for f in dossiers],
                    "count": len(dossiers)}

        if op == "get":
            refuse_ignored(op, query=query, folder=folder, unread=unread,
                           attachment_id=attachment_id, filename=filename)
            mid = need(message_id, "message_id", op)
            m = _run(lambda: c.get_message(mid))
            pieces = _run(lambda: c.list_attachments(mid)) if m.get("hasAttachments") else []
            if full:
                return {**m, "attachments": pieces}
            return {**_message(m), "body": (m.get("body") or {}).get("content"),
                    "bcc": [_adresse(r) for r in m.get("bccRecipients") or []],
                    "attachments": [_piece(a) for a in pieces]}

        if op == "attachment":
            refuse_ignored(op, query=query, folder=folder, unread=unread)
            mid = need(message_id, "message_id", op)
            if (attachment_id is None) == (filename is None):
                raise bad("op='attachment' requires `attachment_id` OR `filename` "
                          "(only one) — both come from op='get'.")
            aid = attachment_id
            if aid is None:
                pieces = _run(lambda: c.list_attachments(mid))
                memes = [a for a in pieces if a.get("name") == filename]
                if len(memes) != 1:
                    noms = [(a.get("name"), a.get("id")) for a in pieces]
                    raise bad(f"{len(memes)} attachments are named {filename!r} on this "
                              f"message — pass `attachment_id` (name, id): {noms}.")
                aid = memes[0]["id"]
            piece = _run(lambda: c.get_attachment(mid, aid))
            if piece.get("contentBytes") is None:
                raise bad(f"« {piece.get('name')} » is not a file attachment "
                          f"({piece.get('@odata.type')}): an attached email or a link "
                          "has no content to read here — open the message in Outlook "
                          "(`webLink` of op='get').")
            data = base64.b64decode(piece["contentBytes"])
            sub = access.current_user_sub_or_raise()
            try:
                return file_content.render_attachment_for_agent(
                    data, piece.get("name") or aid,
                    piece.get("contentType") or "application/octet-stream",
                    sub=sub, prefix="outlook-attachments", sheet=sheet, max_rows=max_rows)
            except (file_content.MediaUnavailable, file_content.SpreadsheetError) as e:
                raise bad(str(e)) from None

        # Structurally unreachable (input guard above).
        raise bad(_ops_error(_OPS))

    @mcp.tool(name="outlook_act", annotations=ACTE)
    @documente(_OUTLOOK_ACT_DOC)
    def outlook_act(
        op: Literal["send", "archive", "trash", "move"],
        draft_id: Optional[str] = None,
        message_ids: Optional[list[str]] = None,
        destination: Optional[str] = None,
        body: Optional[str] = None,
        html: Optional[str] = None,
        html_source: Optional[dict] = None,
        to: Optional[list[str]] = None,
        cc: Optional[list[str]] = None,
        bcc: Optional[list[str]] = None,
        subject: Optional[str] = None,
        reply_to: Optional[str] = None,
        reply_all: bool = False,
        attachments: Optional[list[dict]] = None,
    ) -> dict:
        if op not in _ACT_OPS:
            raise bad(_ops_error(_ACT_OPS))
        if op == "send":
            refuse_ignored(op, message_ids=message_ids, destination=destination)
            if draft_id is None:
                return _composer(envoyer=True, body=body, html=html,
                                 html_source=html_source, to=to, cc=cc, bcc=bcc,
                                 subject=subject, reply_to=reply_to, reply_all=reply_all,
                                 attachments=attachments)
            # A draft is sent AS IT STANDS: an argument beside it would be silently
            # ignored — and the person would believe it was applied.
            _refuse_composition("send with draft_id", body=body, html=html,
                                html_source=html_source, to=to, cc=cc, bcc=bcc,
                                subject=subject, reply_to=reply_to, reply_all=reply_all,
                                attachments=attachments)
            return _envoyer(_client(), draft_id, {}, ecrit_a_l_instant=False)

        # ---- moves: every one is a move (the id is immutable) --------------------
        refuse_ignored(op, draft_id=draft_id)
        _refuse_composition(op, body=body, html=html, html_source=html_source, to=to,
                            cc=cc, bcc=bcc, subject=subject, reply_to=reply_to,
                            reply_all=reply_all, attachments=attachments)
        ids = list(need(message_ids, "message_ids", op))
        if op == "move":
            cible = need(destination, "destination", op)
        else:
            refuse_ignored(op, destination=destination)
            cible = _DESTINATIONS[op]
        return {"moved": _deplacer(_client(), ids, cible), "destination": cible}
