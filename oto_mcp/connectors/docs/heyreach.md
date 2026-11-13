## prerequisite — a heyreach api key

in HeyReach, open **Integrations → HeyReach API** and copy the workspace's **API key**, then paste it into oto. Reference: [HeyReach API](https://documenter.getpostman.com/view/23808049/2sA2xb5F75).
- one key per **workspace**: it acts on every LinkedIn sender account, campaign, list and conversation of that workspace
- keys never expire, but can be deleted or deactivated in HeyReach (the connector then answers 401)
- your own key only: no shared oto key — the senders are your own LinkedIn accounts
- the "test connection" button calls HeyReach's key check: no side effect

## usage — campaigns, leads, inbox, webhooks

`heyreach` reads, `heyreach_act` acts; each call names its function in `op`.

- "how are my running campaigns doing" → `heyreach(op="list_campaigns", limit=50, statuses=["IN_PROGRESS"])`, then `heyreach(op="get_stats_by_campaign", start_date="2026-11-01T00:00:00Z", end_date="2026-11-04T23:59:59Z")`
- "who replied in this campaign" → `heyreach(op="list_campaign_leads", campaign_id=<id from list_campaigns>, limit=100)` and read `leadMessageStatus` = MessageReply
- "my LinkedIn replies" → `heyreach(op="list_conversations", limit=50)`, then `heyreach(op="get_conversation", linkedin_account_id=<linkedInAccountId>, conversation_id=<id>)` for the whole thread
- "answer her" → `heyreach_act(op="send_message", linkedin_account_id=…, conversation_id=…, message="…")` — sent at once, cannot be unsent
- "add these people to the Q4 campaign" → `heyreach(op="list_campaigns", limit=50, keyword="Q4")` for the id, then `heyreach_act(op="add_leads_to_campaign", campaign_id=…, account_lead_pairs=[{"lead": {"profileUrl": "https://www.linkedin.com/in/…/", "firstName": "…"}}])`
- "stop contacting this lead" → `heyreach_act(op="stop_lead_in_campaign", campaign_id=…, lead_url="https://www.linkedin.com/in/…/")`
- "pause the campaign" → `heyreach_act(op="pause_campaign", campaign_id=…)`; `resume_campaign` restarts it
- "tag her as hot" → `heyreach_act(op="add_lead_tags", lead_profile_url="…", tags=["hot"], create_tag_if_not_existing=true)`
- "wake my agent on every reply" → `heyreach_act(op="create_webhook", webhook_name="…", webhook_url="https://…", event_type="EVERY_MESSAGE_REPLY_RECEIVED")`; `heyreach(op="list_webhooks", limit=50)` lists them, `delete_webhook` removes one

## note — sending, leads, pages and limits

- ⚠️ **`add_leads_to_campaign` starts outreach**: an IN_PROGRESS campaign starts connecting with and messaging the leads; `resume_campaign` restarts a paused one; a lead added to a list may be picked up by a running campaign that draws from it
- **a lead uses HeyReach's own keys** (`profileUrl` required, `firstName`, `lastName`, `companyName`, `position`, `emailAddress`, `customUserFields` for the sequence's variables), at most 100 per call; each pair may name the sender (`linkedInAccountId`) that will contact it
- **pages**: most lists take `limit` and `offset` — next page = `offset` + `limit`, until fewer than `limit` items come back; `list_conversations` answers `nextCursor`, passed back as `cursor` while `hasNextPage`
- lists come back trimmed to what is needed to choose: each item keeps the keys its op names; a member's picture and a webhook's custom headers never come back
- leads and conversations carry **personal data**: read only what the task needs
- rate limit: **300 requests per minute for the whole workspace** (429 above)
- not served: connecting or reconnecting a LinkedIn account, proxies, campaign creation and sequence editing, blacklist, email enrichment, deleting leads from a list, organization and workspace management — done in the HeyReach app
