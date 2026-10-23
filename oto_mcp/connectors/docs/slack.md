## prerequisite — the instance's published app, or your own

Two ways to get a Slack credential, same tools behind both:
- **"Add to Slack"** — when the instance publishes a Slack app, the card offers it first: one click, Slack's consent screen, done. The tokens land in this connector, one account per workspace (a reinstall of the same workspace replaces its account). Nothing to create at api.slack.com.
- **your own app** — you create an app in your workspace and paste its tokens here. A one-time task per workspace, ~5 minutes. Use it when the instance publishes no app, or when you read channels a lot: Slack caps history reads (`conversations.history` / `conversations.replies`) for distributed apps not listed in its Marketplace, and an app you create for your own workspace is exempt.

Whichever the path, the two tokens:
- **bot token** (`xoxb-`): read channels, post under the app's identity. This is the default token.
- **user token** (`xoxp-`): post **in your name**, and search (`search:read` only exists as a user token). Optional.
- either one is enough. With **both**, oto reads with the bot and makes you **choose who writes** on each send: `author="me"` (in your name) or `author="app"` (under the app's name) — with no choice, the send is refused rather than going out under the wrong name. The response says who wrote and in which channel, and whether that channel is shared externally.
- otherwise, an admin can grant you your org's platform key

⚠️ **switching app restarts channel memberships.** Memberships belong to the app, not to the key: moving a workspace from your own app to the published one (or back) means re-inviting the new app in private channels (`/invite @<app>`).

## usage — `@<app>` in Slack starts the org's agent

With the instance's published app, an org admin can make one hosted agent answer in Slack: mention the app in a channel (or DM it) and that agent runs, **as you** — your Slack email must be the one of your account in the org — then answers in the thread. Guests and people from another workspace are refused. It only works with the published app ("Add to Slack"), not with your own app.

## setup — create the app by pasting a manifest (the shortest way)

1. go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → choose **From a manifest** (not "AI agent" nor "Starter app": those are conversational-app templates, unrelated)
2. choose the workspace. The editor that opens is **already filled** with an example (`display_information: name: Demo App`): **select all and replace it** — pasting over it without clearing welds the two manifests together and Slack refuses ("Nested mappings are not allowed in compact mappings"). The manifest to put in its place, which already declares all the scopes the oto tools need:

```yaml
display_information:
  name: Oto
  description: Reads and writes in Slack for your Oto agent
features:
  bot_user:
    display_name: Oto
    always_online: false
oauth_config:
  scopes:
    bot:
      - channels:read
      - channels:history
      - channels:join
      - groups:read
      - groups:history
      - im:read
      - im:history
      - im:write
      - mpim:read
      - mpim:history
      - users:read
      - users:read.email
      - chat:write
      - reactions:write
      - files:read
      - usergroups:read
      - usergroups:write
    user:
      - search:read
      - chat:write
      - usergroups:read
      - usergroups:write
settings:
  org_deploy_enabled: false
  socket_mode_enabled: false
  token_rotation_enabled: false
```

3. **Create**, then **Install to Workspace** and authorize
4. in **OAuth & Permissions**, copy the **Bot User OAuth Token** (`xoxb-`) and, if you want to post in your name, the **User OAuth Token** (`xoxp-`)
5. paste them here — nothing else to configure

*(without a manifest: **Blank app**, then declare the same scopes by hand in OAuth & Permissions before installing. The manifest avoids exactly this step.)*

⚠️ **a scope does not replace channel membership.** without membership, Slack answers `not_in_channel` — which wrongly looks like a token problem. two cases, and only one can be automated:
- **public channel**: `slack_join_channel` gets the app in on its own (this is what the manifest's `channels:join` scope above is for)
- **private channel**: no Slack API lets you invite yourself. a human who is already a member must type `/invite @Oto` in the channel — oto cannot do it for them, and says so instead of letting you believe it's an outage

Slack reference: [create an app from a manifest](https://api.slack.com/reference/manifests) · [install with oauth v2](https://api.slack.com/authentication/oauth-v2)

## setup — a second workspace, a second installation

a Slack token is issued **per installation**: two workspaces = two independent sets of tokens. Redo the installation in the second workspace (same manifest), then set its tokens as a second workspace of the connector (section "several workspaces").

## usage — what you can do

send and read slack messages from claude — in your name or under the app's, depending on the tokens set.
- "send a message in #general" → `slack_post_message`
- "dm jean by email" → `slack_find_user_by_email` then `slack_open_dm` then `slack_post_message`
- "read the latest messages of this channel" → `slack_read_history` (top-level messages; `oldest`/`latest` to read only a window)
- "what was said in this thread?" → `slack_read_thread` — a thread's **replies**, which `slack_read_history` does not return (it only shows the count)
- "get oto into #channel" → `slack_join_channel` (public channels only)
- "react 👍 to this message" → `slack_add_reaction`
- "who is in @sales?" → `slack(op="usergroups")` (all groups), `slack(op="usergroup_members", usergroup=…)` (one group's members)
- "create @support / add jean to @sales / rename a group" → `slack_act` (`op="create_usergroup"`, `"add_usergroup_members"`, `"update_usergroup"`…; `dry_run=true` shows the member diff first)

⚠️ **user groups**: they require a paid Slack plan. An app installed before these scopes were added must be **reinstalled** (OAuth & Permissions → add `usergroups:read` / `usergroups:write` → Reinstall), then its new tokens set here. Writing with the **bot** token only works when the workspace lets **everyone** manage user groups; otherwise write with `author="me"` and the user token of a member allowed to (an admin, by default). Slack replaces a group's member list as a whole and refuses an empty one — a group is emptied by disabling it.
