## prerequisite — connect with your Microsoft 365 account

on the "Outlook" card, click **Authorize Outlook** and choose your work account. Nothing else to install or register. The account itself is the "Microsoft 365 account" connector: Outlook borrows it and only adds its own permissions (read and write your mail, send it).
- the agent acts **with your rights**, in **your** mailbox: it reads what you can read, and what it sends leaves from your address. Each person in the org connects their own account
- work or school accounts only; a guest of a client's Microsoft 365 fills in **Client directory** at connection (the client's domain; see the "Microsoft 365 account" connector)
- ⚠️ **many organizations require a Microsoft 365 administrator to authorize oto a first time.** If Microsoft shows "admin approval required", the card comes back with that reason: an administrator approves oto once for the whole organization (`microsoft_admin_consent`, see the "Microsoft 365 account" connector), then everyone can connect
- an account already linked for another Microsoft service (SharePoint, the calendar…) only needs to authorize this one; an account that has not authorized Outlook is refused by the tools, naming this card
- the connection lasts over time; it drops if the password changes, if the organization revokes it or after a long inactivity: the card then says which account is "to reconnect", the others keep working

## usage — search, read, draft, send

Two tools, under the account the call names (`_account="me@fabrikam.com"`), otherwise the default one: `outlook(op=search|get|attachment|drafts|folders|draft)` reads or prepares a draft — nothing leaves, nothing moves; `outlook_act(op=send|archive|trash|move)` acts, and your client asks you to confirm it.
- "this week's unread emails" → `outlook(unread=true)`; "the invoices from Jane" → `outlook(query="from:jane invoice")`
- "read this email" → `outlook(op="get", message_id="…")`: its text and its attachments; "open the PDF attached" → `op="attachment"` with `attachment_id` or `filename` — a PDF or a text comes back as text, an Excel as CSV, an image or a scanned PDF with JPEG pages as an image you can look at (up to 3 MB and 5 pages; beyond, a signed URL)
- "draft a reply" → `outlook(op="draft", reply_to="…", body="…")`, attachments and inline images accepted; "send it" → `outlook_act(op="send", draft_id="…")`, the draft as it stands
- "write to Marc with the contract" → `outlook(op="draft", to=["marc@…"], subject="…", body="…", attachments=[{"kind":"drive","file_id":"…"}])`, then `outlook_act(op="send", draft_id="…")`
- "send this HTML newsletter" → `outlook_act(op="send", to=[…], subject="…", html="<html>…")`, or `html_source={"kind":"project_file",…}` for an HTML file: sent exactly as given, nothing rewritten. An image shown in it is an attachment with `"content_id":"logo"`, referenced as `<img src="cid:logo">`; Outlook's rendering has its limits, see the oto guide "Mails HTML compatibles Outlook" (`outlook-html`)
- "archive the newsletters" → `outlook_act(op="archive", message_ids=[…])`; `op="trash"` moves to Deleted Items, `op="move"` to any folder (`outlook(op="folders")` lists them)
- each response is a trimmed view; `full=true` returns the complete Microsoft Graph object, `fields=[…]` keeps only some keys of each message

## note — what is misleading

- ⚠️ **only `outlook_act` sends**: `outlook(op="draft")` saves a draft, `outlook_act(op="send")` sends it. The answer says `kind`: `draft` (saved in Drafts, nothing left) or `sent` — with the link of the sent message, unless Microsoft has not filed it in Sent Items yet (`webLink` null and a `note`: the mail did leave)
- ⚠️ **no signature is added**: Microsoft does not let an application read your Outlook signature. Write the sign-off in the body
- a message keeps its id when it is moved (archive, trash, move) or sent: the id an answer gives stays valid
- a search (`query`) cannot be combined with `unread`: Microsoft refuses it. Put the condition in the query, or list without one
- a search comes back in Microsoft's order (most recent first), from its index: a message received seconds ago may not be found yet
- an attachment is limited to 3 MB when writing, an HTML body read from a file to 1 MB; reading one has the usual limits

## note — scope

mail only: search, read, attachments, drafts, replies, sending, archive, trash, move between folders. Nothing deletes a message for good, nothing changes rules, categories or the signature. The calendar is the "Outlook Calendar" connector.
