## prerequisite — an instantly api key (api v2) with its scopes

in Instantly, **Settings → Integrations → API keys → Create API key** (API V2), then paste the key into oto. It is sent as `Authorization: Bearer`. Reference: [Instantly API V2](https://developer.instantly.ai).
- ⚠️ **scopes are chosen when the key is created** and cannot be read back: **`all:all`** to read and act, **`all:read`** to read only — then every `instantly_act` op answers 403 with the missing scope named
- finer keys work too: `campaigns:read`, `leads:read`, `lead_lists:read`, `emails:read`, `accounts:read`, `lead-labels:read`, `workspaces:read` (the connection check) to read; `leads:create`, `leads:update`, `lead_lists:create`, `campaigns:update`, `emails:create` (replies), `emails:update`, `block_list_entries:create`, `webhooks:create`, `webhooks:delete` to act
- the key reaches **one workspace**; an API V1 key does not work here
- byo-only: no shared oto key — the campaigns, leads and mailboxes are your own
- the "test connection" button reads the workspace: no side effect

## usage — campaigns, replies, leads, webhooks

- "how are my campaigns doing this month" → `instantly(op="campaigns", status=1)`, then `instantly(op="campaign_analytics", start_date="2026-11-01", exclude_total_leads_count=true)`
- "which variant of this campaign gets replies" → `instantly(op="analytics_steps", campaign_id="…")`
- "the new replies" → `instantly(op="emails", email_type="received", is_unread=true, latest_of_thread=true)`, one in full with `instantly(op="email", email_id="…")`
- "answer that reply" → `instantly_act(op="reply_email", email_id="…", eaccount="<the sending account>", subject="Re: …", text="…")` — sent at once
- "mark this lead as meeting booked" → `instantly_act(op="update_interest_status", email="…", interest_value=2, campaign_id="…")`
- "add these people to the webinar campaign" → `instantly(op="campaigns", search="webinar")` for the id, then `instantly_act(op="add_leads", campaign_id="…", leads=[{"email": "…", "first_name": "…", "custom_variables": {"event": "…"}}], skip_if_in_workspace=true)`
- "is this person already in a campaign" → `instantly(op="lead_campaigns", email="…")`
- "the interested leads of this campaign" → `instantly(op="leads", campaign_id="…", filter="FILTER_LEAD_INTERESTED")`
- "move them to the follow-up list" → `instantly_act(op="move_leads", campaign_id="…", filter="FILTER_LEAD_INTERESTED", to_list_id="…", copy_leads=true)`, then `instantly(op="background_job", job_id="…")` until success
- "wake an agent on each reply" → `instantly_act(op="create_webhook", url="<the agent's https hook>", event_type="reply_received")`; `instantly(op="webhooks")` lists them, `instantly_act(op="delete_webhook", webhook_id="…")` removes one
- "stop emailing this company" → `instantly_act(op="add_blocklist_entry", value="example.com")`

## note — sending, pages and limits

- ⚠️ **these ops send email**: `reply_email` (at once), `add_leads` / `create_lead` / `move_leads` into an active campaign, `activate_campaign`; `update_interest_status` may start a subsequence that emails
- ⚠️ **a webhook posts lead data** (emails, replies) to its `url`: only an address the workspace trusts; its delivery headers are never shown
- **pages**: lists answer `next_starting_after`; pass it back as `starting_after` (absent = last page); `limit` 1 to 100
- **lean lists**: campaigns, leads, emails and accounts drop by default what is not needed to choose (sequences, step summaries, the HTML body…), named under `projection`; `full=true` returns raw rows
- listing emails is limited to **20 requests per minute**; a 429 is retried a minute later
- a 402 means the workspace has no active paid plan; a 403, a scope missing on the key (or a feature outside the plan)
- not covered: sending accounts' connection and warmup, campaign creation and sequence editing, SuperSearch and enrichment (credits), inbox placement tests, AI agents, workspace administration, deletions other than a webhook
