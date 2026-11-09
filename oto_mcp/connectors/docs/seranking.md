## prerequisite — an SE Ranking API key

one key opens both SE Ranking APIs: the Data API (any domain's SEO data, paid in credits) and the Project API (the projects tracked in your account). the plan decides which of the two answers.

- in the SE Ranking account, API > Dashboard: create a key and paste it here
- "test the connection" reads the subscription and its Data API credits left: zero credits reads as not healthy, even when the Project API still answers
- a key can be set for you alone or for the whole org; the credits it spends are the account's own

## usage — read a domain, research keywords, follow the tracked projects

two tools. `seranking` reads the tracked projects and audits, and spends nothing. `seranking_act` spends the account's Data API credits, or uses the plan's checks.

- "which projects do we track?" → `seranking(op="list_sites")`: each `id` is the `site_id` of the other project ops
- "where do we rank this week?" → `seranking(op="get_site_positions", site_id=…, date_from="2026-11-02", date_to="2026-11-09")`
- "how does example.com do in France?" → `seranking_act(op="domain_overview", source="fr", domain="example.com")` (100 credits)
- "its top organic keywords" → `seranking_act(op="domain_keywords", source="fr", domain="example.com", limit=100)` (100 credits)
- "volume and difficulty of these keywords" → `seranking_act(op="keyword_metrics", source="us", keywords=[…])` (10 credits per keyword)
- "audit the site" → `seranking_act(op="create_audit", domain="example.com")`, then `seranking(op="get_audit_status", …)` until it is finished, then `op="get_audit_report"`

## note — spend SE Ranking credits knowingly

- `source` is a regional database: a lowercase ISO country code (`us`, `fr`, `de`), `uk` for the United Kingdom
- only successful answers are billed; a 4xx costs nothing
- per call: 100 credits for a domain overview, history, keywords, competitors and backlinks; up to 1,000 for similar, related and question keywords (10 per keyword returned); 10 per keyword sent to `keyword_metrics`. `get_subscription` is free and says what is left
- "insufficient funds" means no credits left: retrying frees nothing, top up the account
- a site too large for the API (such as google.com) comes back empty with status 244
- the Project API blocks a key for 10 minutes after repeated bursts: space out the calls
