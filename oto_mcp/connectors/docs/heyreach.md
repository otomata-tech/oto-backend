## prerequisite — a heyreach api key

in HeyReach, open **Integrations → HeyReach API** and copy the workspace's **API key**, then paste it into oto. Reference: [HeyReach API](https://documenter.getpostman.com/view/23808049/2sA2xb5F75).
- one key per **workspace**: it acts on every LinkedIn sender account, campaign, list and conversation of that workspace
- keys never expire, but can be deleted or deactivated in HeyReach (the connector then answers 401)
- byo-only: no shared oto key — the senders are your own LinkedIn accounts
- the "test connection" button calls HeyReach's key check: no side effect

## usage — campaigns, leads, inbox, webhooks

- "how are my running campaigns doing" → `heyreach(op="campaigns", statuses=["IN_PROGRESS"])`, then `heyreach(op="stats_by_campaign", date_from="2026-11-01T00:00:00Z", date_to="2026-11-04T23:59:59Z")`
- "who replied in campaign 1001" → `heyreach(op="campaign_leads", campaign_id=1001, limit=100)` and read `leadMessageStatus` = MessageReply
- "my unread LinkedIn replies" → `heyreach(op="conversations", seen=false, limit=50)`, then `heyreach(op="conversation", sender_id=<linkedInAccountId>, conversation_id=<id>)` for the whole thread
- "answer her" → `heyreach_act(op="send_message", sender_id=<linkedInAccountId>, conversation_id=<id>, message="…")` — sent at once, cannot be unsent
- "add these people to the Q4 campaign" → `heyreach(op="campaigns", keyword="Q4")` for the id, then `heyreach_act(op="add_campaign_leads", campaign_id=…, leads=[{"profileUrl": "https://www.linkedin.com/in/…/", "firstName": "…", "customUserFields": [{"name": "opener", "value": "…"}]}])`
- "stop contacting this lead" → `heyreach_act(op="set_campaign_lead_stopped", campaign_id=…, profile_url="https://www.linkedin.com/in/…/")`
- "pause the campaign" → `heyreach_act(op="set_campaign_paused", campaign_id=…)`; `set_campaign_resumed` restarts it
- "tag her as hot" → `heyreach_act(op="add_lead_tags", profile_url="…", tags=["hot"], create_missing_tags=true)`
- "wake my agent on every reply" → `heyreach_act(op="create_webhook", name="Replies to the follow-up agent", webhook_url="https://…", event_type="EVERY_MESSAGE_REPLY_RECEIVED")`; `heyreach(op="webhooks")` lists them, `delete_webhook` removes one

## note — sending, leads, pages and limits

- ⚠️ **`add_campaign_leads` starts outreach**: an IN_PROGRESS campaign starts connecting with and messaging the leads; `set_campaign_resumed` restarts a paused one; a lead added to a list may be picked up by a running campaign that draws from it
- **a lead uses HeyReach's own keys** (`profileUrl` required, `firstName`, `lastName`, `companyName`, `position`, `emailAddress`, `customUserFields` for the sequence's variables — names in letters, digits and `_`), at most 100 per call; in `add_campaign_leads`, a lead may carry its own `linkedInAccountId`, or `sender_id` gives one sender to all
- **two paginations**: most lists answer `totalCount`, next page = `offset` + `limit` (default 50, up to 100; 1000 for `list_leads`); `conversations` answers `nextCursor` — pass it back as `cursor` while `hasNextPage`
- lists come back trimmed (a conversation's thread, a lead's long prose, a sender's daily limits, a campaign's exclusion settings, named under `projection`): `full=true` returns the raw rows; a member's picture and a webhook's custom headers never come back
- leads and conversations carry **personal data**: read only what the task needs
- rate limit: **300 requests per minute for the whole workspace** (429 above)
- not served: connecting or reconnecting a LinkedIn account, proxies, campaign creation and sequence editing, blacklist, email enrichment, deleting leads from a list, organization and workspace management — done in the HeyReach app
