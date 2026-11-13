## prerequisite — lemlist api key

create an API key in [lemlist](https://app.lemlist.com) (Settings → Integrations → API), then paste it into oto (account / connectors page).
- everyone sees THEIR data: your own key is required

## usage — the whole lemlist API

the connector mirrors the **141 documented routes**, without exception. by family:

**campaigns** — `lemlist_list_campaigns`, `lemlist_campaign`, `lemlist_sequence`, `lemlist_schedule`, `lemlist_get_campaign_stats`
- "create a campaign "Q4 outbound", add an email step at D+3"
- "what is blocking the launch of this campaign?" (missing sender, DNS, daily limit)
- "set the sending window to 8am-12pm Monday to Thursday", "duplicate it for the NL team"
- "stats for campaign X", "compare my 5 campaigns this quarter"

**leads** — `lemlist_create_lead`, `lemlist_lead`, `lemlist_enrich*`
- "add this lead, find their email and verify it"
- a lead already in a campaign is not an error: `lemlist_create_lead` returns `created: false` with `reason` (`already_in_other_campaign` / `already_in_campaign`) — the contact is already taken, count it as such
- a created lead does not say whether it goes out: `lemlist_create_lead` returns `review_state: "unknown"` (lemlist does not say, and `isPaused` is ruled out because it does not distinguish review from sending). To find out, use `lemlist_campaign(op="reports")`: if `reviewedCount` or `inSequenceLeadCount` go up with the addition, the lead goes out
- "pause this lead on all campaigns", "mark them as interested"
- "import the leads from HubSpot filter X into this campaign"

**lemlist CRM** — `lemlist_contact`, `lemlist_company`, `lemlist_team(op="fields")`
- ⚠️ a **contact** is not a **lead**: the lead is a person's copy INSIDE a campaign, the contact is the person themselves. this is the most costly confusion here.

**inbox** — `lemlist_inbox` (conversations, drafts, labels) and `lemlist_act` (the direct sends, and the personas of the shared base)
**unsubscribes** — `lemlist_unsubscribe`: ⚠️ **three distinct lists** (v1 emails/domains, v2 variables, a contact's do-not-contact flag); writing to one does not write to the others
**signals** — `lemlist_watchlist`: companies that are hiring, fundraising, job changes, visiting your website… (website visitors: section below)
**the rest** — `lemlist_task`, `lemlist_database` (shared base, read-only: people, companies, filters, personas — saving or deleting a persona is `lemlist_act`), `lemlist_team`, `lemlist_mailbox` (SMTP/IMAP connection + lemwarm), `lemlist_deliverability`, `lemlist_webhook`, `lemlist_get_activities` (pass `compact=True`, section below)

## usage — reading activities

`lemlist_get_activities` returns each event with its whole lead: every personalised copy of the sequence (HTML), the custom variables, lemlist's metadata. 100 events weigh hundreds of thousands of characters — more than an agent run can read.
- **agents: pass `compact=True`** — each event keeps `_id`, `type`, `createdAt`, `campaignId`, `campaignName`, `sequenceStep`, `leadId`, `contactId`, `leadEmail`, `leadFirstName`, `leadLastName`, `leadCompanyName`, `fromEmail` (who replied), `sendUserEmail`, `subject`, and `excerpt`: the first 280 characters of the body (text first, else lemlist's preview, else the HTML without its tags). never HTML, never the lead's variables.
- `fields=["type", "lead.variables.email"]` keeps only those keys (dotted path for a nested key, lists traversed, never an index); a key an event lacks is skipped. with `compact=True`, each field named is taken from the full event and added to the short view. a malformed list is refused before any request.
- neither given: the events as lemlist sends them. both apply with `all_pages=True` too.

## usage — website visitors

lemlist identifies the COMPANIES visiting your site (its watch-list type `companyEmployeeVisitedMyWebsite`), and names the person for some US traffic. a client who has it does not need a separate visitor-identification tool.
- **before anything**: lemlist's tracking script must be on the site. it is installed from the lemlist app (Signal agents → Create signal agent → Website visitors → copy the script), never through the API
- "watch who visits our pricing page" → `lemlist_watchlist(op="filters", watch_type="companyEmployeeVisitedMyWebsite")` for the accepted filters (`website`, `pagesTracked`, `minVisitDuration`, `minPageViewed`, `visitLocation`, `internalTraffic`, `excludedVisitorIps`, `maxIdentificationsPerDay`), canonical values from `op="filter_values"`, then `op="create"` — born as a draft
- ⚠️ **an active list spends 20 lemlist credits per visit caught**: set `maxIdentificationsPerDay`, and confirm with the user before `activate=True` (`lemlist_team(op="credits")` counts what is left)
- "which companies visited since yesterday?" → `lemlist_watchlist(op="signals", watch_type="companyEmployeeVisitedMyWebsite", received_from="2026-08-30T00:00:00Z")` — each signal carries `company` (name, domain, linkedinUrl, industry) and `signalData`: `websiteVisited` (the page), `visitorWebsite`, `visitorCountry`, `visitorDuration` (seconds); `contact` only when lemlist named the visitor
- to run on each visit rather than on a schedule: `lemlist_webhook(op="add", target_url=<the agent's hook URL>, event_type="signalRegistered", secret=<the agent's otoh_ token>)` on an agent whose webhook auth is `lemlist`. `signalRegistered` fires for EVERY watch list of the team: check the signal type before acting
- "push every visiting company into campaign X" → `op="create"` with `signal_processing_type="push_to_campaign"`, `campaign_id` and `persona_id` (who to look for at the company, from `lemlist_database(op="personas")`) — ⚠️ once active, this list feeds the campaign ON ITS OWN
- not live-tested yet on a tracked site: the shapes above are lemlist's documented ones

## note — what sends, and what is hidden by default

four tools send or **arm** sending. all four are **hidden by default** — they remain callable, you just have to enable them (`oto_enable_tool <name>`):

- `lemlist_campaign_start` — starting the campaign runs the sequence for all its launched leads
- `lemlist_launch_lead` — take a lead out of manual review
- `lemlist_act` — **direct** email / LinkedIn / WhatsApp (`op=send_email|send_linkedin|send_whatsapp`): no campaign, no sequence, no review in front of them, the message goes out. it also saves or deletes a persona of the shared base (`op=create_persona|delete_persona`)
- `lemlist_campaign_auto_review` — sends nothing itself, but makes every lead **added** afterwards go out: with it, `lemlist_create_lead` becomes a send

everything else works on data or on a campaign that has nothing to send yet.

⚠️ **a campaign created by the API is born `state=running`**, contrary to what its `status` ("draft") suggests — verified live on 31/08. it sends nothing as long as no lead is launched, but `lemlist_campaign(op="start")` answers "already running" and `op="pause"` is the real switch. to build one calmly: create, then pause. (a **duplicated** campaign is running too — `state: running` in `op="reports"` — pause it the same way.)

pause ≠ recall: pausing stops progression, not what is already scheduled.

two surfaces send **indirectly**, and stay visible by saying so: a watch list set to `push_to_campaign` feeds a campaign on its own, and `lemlist_mailbox(op="lemwarm_start")` sends — but within the warm-up network, never to a prospect.

## note — lemlist API pitfalls

- **deleting a lead ≠ unsubscribing them**, and lemlist serves both through the same route, the default being the soft one. here the two ops are named separately (`op="delete"` vs `op="unsubscribe"`).
- **`lemlist_lead(op="pause")` without `campaign_id` pauses the lead on ALL campaigns**, not just one.
- **`lemlist_contact(op="list_manage")` ADDS by default**; `action="remove"` removes.
- deleting a step is refused while the campaign is running — pause it first.
- enrichments spend credits (`lemlist_team(op="credits")` counts them).

## note — doc↔API gaps found live (31/08/2026)

the connector already absorbs them; this is here to understand an error message, not to act on.

- `lemlist_task(op="create")` requires `record_id` (the contact or the lead), which the lemlist doc says is optional.
- `lemlist_watchlist(op="create")`: read `op="filters"` first — each type has its own mandatory filters, values must come from `op="filter_values"`, and numbers travel as strings.
- `lemlist_campaign(op="export_leads")` filters on `state="all"` by default: lemlist's default returns an empty list that reads as "no leads".
- what this account does not allow (plan limit, not a bug): LinkedIn steps (`Upgrade your plan`), the CRM endpoints (`crm_filters`, `crm_users`, `import_crm` → "Endpoint not available" without a CRM integration), and a watch list's history (beta not enabled).
