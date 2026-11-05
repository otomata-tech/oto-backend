## prerequisite — connect with your Microsoft 365 account

on the "Teams" card, click **Authorize Teams** and choose your work account. Nothing else to install or register. The account itself is the "Microsoft 365 account" connector: Teams borrows it and only adds its own permissions (your teams and channels, posting in a channel, your chats).
- the agent acts **with your rights**: it sees the teams, channels and chats you are a member of, and what it posts is posted **in your name**. Each person in the org connects their own account
- work or school accounts only; a guest of a client's Microsoft 365 fills in **Client directory** at connection (the client's domain; see the "Microsoft 365 account" connector)
- an account already linked for another Microsoft service only needs to authorize this one; an account that has not authorized Teams is refused by the tools, naming this card

## setup — reading channel messages: the administrator's approval

Microsoft lets a person authorize alone everything above, **except reading the messages of a channel**: that permission (ChannelMessage.Read.All) can only be granted by a Microsoft 365 administrator of the organization, once for everyone.
- until then, reading a channel is refused, and the answer gives the approval link to send to the administrator; the card says "Reading channel messages requires your Microsoft admin's approval"
- `microsoft_admin_consent(services=["teams"])` gives the same link (valid seven days), for the account's directory or the one you name in `tenant`
- once approved, nothing to reconnect: the next read sees it. Chats and posting never need it
- some organizations also require an administrator for the first connection itself ("admin approval required" on Microsoft's screen): the same link covers both

## usage — read and post

`teams` reads — where you can read and post (`op=teams|channels|chats`) and the messages (`op=list|replies`) — and `teams_act(op=post|reply)` writes: two tools, so that allowing reads never allows posting (your client asks you to confirm `teams_act`) — on a channel (`team_id` + `channel_id`) or a chat (`chat_id`), under the account the call names (`_account=`), otherwise the default one.
- "my teams" → `teams()`; "the channels of Sales" → `teams(op="channels", team_id="…")`; "my chats" → `teams(op="chats")`
- "the latest messages of my chat with Marc" → `teams(op="list", chat_id="…")`
- "what was said in #general" → `teams(op="list", team_id="…", channel_id="…")` (administrator's approval needed), and a thread → `op="replies"` with `message_id`
- "the screenshot in that message" → `teams(op="image", image="…")` with one of the message's `images`: you see the image itself
- "post the summary in #general" → `teams_act(team_id="…", channel_id="…", body="…")`; "answer in that thread" → `teams_act(op="reply", …)` with `message_id`; in a chat → `teams_act` with `chat_id`
- `body` is written in markdown and posted as formatted text; each message comes back as text (`full=true` for the complete Microsoft Graph object)

## note — what is misleading

- ⚠️ **a post is visible at once** to the members of the channel or chat: Teams has no draft
- ⚠️ **reading a channel needs the administrator's approval**, reading a chat does not: a refusal on a channel is not a problem with your account
- a chat has no threads: reply in it with `teams_act(op="post")`
- a channel's `list` returns its root messages only; the replies of one are `op="replies"`
- an image pasted in a message is not in its `text`: the message lists it under `images`; on a channel, viewing it needs the same administrator's approval as reading the message
- ids look like `19:…@thread.tacv2`: pass them exactly as returned

## note — scope

teams, channels, chats: list them, read messages and the images pasted in them (channels with the administrator's approval), post, reply in a channel thread. Nothing creates a team or a channel, adds a member, edits or deletes a message, or reads files shared in Teams (those are in SharePoint: the "SharePoint & OneDrive" connector).
