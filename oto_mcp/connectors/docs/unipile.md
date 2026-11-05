## prerequisite — unipile hosted connection

connect your accounts (LinkedIn, WhatsApp, Telegram, Instagram) through **[Unipile](https://www.unipile.com) hosted auth** from the oto dashboard — no cookie to paste, no extension. the session runs at Unipile (real Chrome + residential proxy), which avoids fingerprint blocks.
- **hosted messaging** option enabled by your org's admin (or your own Unipile key as BYO)

## usage — linkedin prospecting + multi-channel messaging

LinkedIn search, scraping and messaging (and WhatsApp/Telegram/Instagram messaging), you act as yourself.
- "search LinkedIn for CFOs in the Lyon region in my 1st-degree network"
- "open this slug's LinkedIn profile and summarize their career"
- "send an invitation to this prospect with a note" then "reply in the thread when they accept"
- "read my latest WhatsApp conversations and reply to the last one"
- "show my recent LinkedIn home feed" or "comment on this post"

## note — another member's account

a messaging account is its owner's. another member of the org uses it only if its owner (or whoever manages it) set a rule on it — `oto_instance op=set_rule`, for a person, a team or the whole org, at a level: `read` reads its conversations, `sensitive` also sends. `oto_identity op=list` shows such an account with its `level` and `owner`; it is used NAMED on each call, `_account=<its id>`, never as your default — or, when you have no account of your own on that channel, unnamed (an account of your org lent to you first, the most recent loan; the response says `lent_by`). without a rule it does not serve; below the level a call requires (a send needs `sensitive`), the call is refused `insufficient_level`, naming who manages it.
