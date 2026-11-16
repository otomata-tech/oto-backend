## prerequisite — an instantly api key (api v2) with its scopes

in Instantly, **Settings → Integrations → API keys → Create API key** (API V2), then paste the key into oto. Reference: [Instantly API V2](https://developer.instantly.ai).
- ⚠️ **scopes are chosen when the key is created** and cannot be read back: **`all:all`** to read and act, **`all:read`** to read only — then every `instantly_act` op answers 403 with the missing scope named
- the key reaches **one workspace**; an API V1 key does not work here
- your own key only: no shared oto key — the campaigns, leads and mailboxes are your own
- the "test connection" button reads the workspace: no side effect

## usage — campaigns, replies, leads, webhooks

`instantly` reads, `instantly_act` acts; each call names its function in `op`.

- "how are my campaigns doing this month" → `instantly(op="list_campaigns", status=1)`, then `instantly(op="get_campaign_analytics", start_date="2026-11-01", exclude_total_leads_count=true)`
- "which variant of this campaign gets replies" → `instantly(op="get_campaign_steps_analytics", campaign_id=<id from list_campaigns>)`
- "the new replies" → `instantly(op="list_emails", email_type="received", is_unread=true, latest_of_thread=true)`, one in full with `instantly(op="get_email", email_id=…)`
- "answer that reply" → `instantly_act(op="reply_to_email", reply_to_uuid=<the email's id>, eaccount=<the sending account>, subject="Re: …", text="…")` — sent at once
- "mark this lead as meeting booked" → `instantly_act(op="update_lead_interest_status", lead_email="…", interest_value=2, campaign_id=…)`
- "add these people to the webinar campaign" → `instantly(op="list_campaigns", search="webinar")` for the id, then `instantly_act(op="add_leads", campaign_id=…, leads=[{"email": "…", "first_name": "…", "custom_variables": {"event": "…"}}], skip_if_in_workspace=true)`
- "the interested leads of this campaign" → `instantly(op="list_leads", campaign=…, filter="FILTER_LEAD_INTERESTED")`
- "move them to the follow-up list" → `instantly_act(op="move_leads", campaign=…, filter="FILTER_LEAD_INTERESTED", to_list_id=…, copy_leads=true)`, then `instantly(op="get_background_job", job_id=<the job's id>)` until success
- "wake an agent on each reply" → `instantly_act(op="create_webhook", target_hook_url="<the agent's https hook>", event_type="reply_received")`; `instantly(op="list_webhooks")` lists them, `delete_webhook` removes one
- "stop emailing this company" → `instantly_act(op="add_block_list_entry", bl_value="example.com")`

## note — sending, pages and limits

- ⚠️ **these ops send email**: `reply_to_email` (at once), `add_leads` / `create_lead` / `move_leads` into an active campaign, `activate_campaign`; `update_lead_interest_status` may start a subsequence that emails
- ⚠️ **a webhook posts lead data** (emails, replies) to its address: only an address the workspace trusts; its delivery headers are never shown
- **`move_leads` does not paginate**: its `limit` bounds how many leads are moved; it answers a background job, read with `get_background_job`
- **pages**: lists answer `next_starting_after`; pass it back as `starting_after` (absent = last page); `limit` 1 to 100
- **lean lists**: campaigns, leads, emails and accounts drop by default what is not needed to choose (sequences, the HTML body…); `get_campaign` and `get_email` return one in full
- listing emails is limited to **20 requests per minute**
- a 402 means the workspace has no active paid plan; a 403, a scope missing on the key (or a feature outside the plan)
- not covered: sending accounts' connection and warmup, campaign creation and sequence editing, SuperSearch and enrichment (credits), inbox placement tests, AI agents, workspace administration, deletions other than a webhook
