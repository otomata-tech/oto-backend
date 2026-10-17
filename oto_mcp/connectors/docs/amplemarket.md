## prerequisite — amplemarket api key

an Amplemarket **admin** creates an API key under **Settings > API**, then pastes it into oto. The key is sent as `Authorization: Bearer`.
- byo-only: no shared oto key — the searches, enrichments and sequences run on your own Amplemarket seat and credits
- the key acts for the whole **workspace**, not for one user
- the "test connection" button reads the workspace (`account_info`): no side effect

## usage — prospect, enrich, enroll

- "find heads of sales in French SaaS companies" → `amplemarket(op="search_people", filters={"person_titles": ["Head of Sales"], "person_locations": ["France"], "company_keywords": ["SaaS"]})` — no email revealed
- "get this person's email" → `amplemarket(op="find_person", linkedin_url="…", reveal_email=True)` — spends credits
- "enrich these 500 people" → `amplemarket_act(op="create_person_enrichment", leads=[…], reveal_email=True)`, then `amplemarket(op="person_enrichment", id=…)` until the status is final
- "validate these emails" → `amplemarket_act(op="create_email_validation", emails=[…])`, then `amplemarket(op="email_validation", id=…)`
- "put them in the Q4 sequence" → `amplemarket(op="sequences", name="Q4")` for the id, then `amplemarket_act(op="add_sequence_leads", id=…, leads=[{"email": …, "data": {…}}])`
- "build a lead list from these profiles" → `amplemarket_act(op="create_lead_list", owner="<user email>", type="linkedin", leads=[…])`
- "my call tasks for today" → `amplemarket(op="users", email="…")` for the user id, then `amplemarket(op="tasks", user_id=…)`
- "never contact this domain" → `amplemarket_act(op="add_excluded_domains", domains=["example.com"])`

## note — credits, batches and pages

- ⚠️ **revealing spends credits**: `find_person` with `reveal_email` / `reveal_phone_numbers`, person enrichments, email validations and lead lists with enrichment options; an empty balance answers 402 `insufficient_credits`
- ⚠️ **`add_sequence_leads` starts outreach**: an active sequence emails or messages the leads; removing an exclusion lets sequences reach them again
- **batches are asynchronous**: `create_*` answers an `id`, the read op of the same name gives the status and results, `cancel_*` stops it
- **two paginations**: lists answer `next_page_after` (pass it back as `page_after`); the searcher pages by `page` (from 1) and answers `_pagination`
- rate limits: 500 calls/min overall, 30/min when adding leads to a sequence
