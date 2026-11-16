## prerequisite — authorize Gmail on your Google account

From this card, click **connect**: Google asks you to authorize **Gmail only** (scope `gmail.modify`) on the account you choose. The Google account itself (address, token) is carried by the **Google Account** connector — one account can authorize several services, one service at a time, without re-authorizing the others.
- multiple Google accounts: each tool acts on the one the call names with `_account=<email>` (the tools' `account=<email>` is the same choice — two different addresses are refused), otherwise the one the project pins, otherwise the default; an unknown address is refused, never replaced by another, and the response names the account that served (`_account`); `google_accounts` tells you which ones have authorized Gmail
- an account that has not authorized Gmail is refused by the tools, naming this card — come back here to authorize it

## usage — search, read, draft, send

Two tools, under the chosen account: `gmail(op=search|get|attachment|drafts|draft|accounts)` reads or prepares a draft — nothing leaves, nothing is deleted; `gmail_act(op=send|archive|trash|delete_draft)` acts, and your client asks you to confirm it.
- "find this week's unread emails" → `gmail(query="is:unread newer_than:7d")`; "archive the newsletters" → `gmail_act(op="archive", message_ids=[…])`
- "draft a reply to this email" → `gmail(op="draft", reply_to="…", body="…")`; "send it" → `gmail_act(op="send", draft_id="…")`, the draft as it stands; "delete this draft" → `gmail_act(op="delete_draft", draft_id="…")` (the draft `id`, not its `message_id`: `trash` answers 404 on a draft)
- composing (`gmail` op=draft, or `gmail_act` op=send with a full message) appends the sending account's **Gmail signature** (after `--`), like the web client — the Gmail API never does it on its own. `sign=False` composes without it; the response reports `signature`: `appended`, `none_configured` or `disabled`
- "read the `.xlsx` spreadsheet attached to this email, `devis` sheet" — an attachment (`op=attachment`) comes back as CSV per sheet, truncated
- "look at the photo or the scan attached" — an image, or a scanned PDF with JPEG pages, comes back as an image you can look at (up to 3 MB and 5 pages; beyond, a signed URL)

## note — project scope (#605)

A `{kind: "url"}` attachment of a composed mail is read server-side: under a project with `excluded_url_prefixes`, a matching url is refused, naming the pattern (`file_source` seam). Details: `docs/projects.md`.
