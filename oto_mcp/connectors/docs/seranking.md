## prerequisite — an se ranking api key

create the key in your SE Ranking account, **API > Dashboard**, then paste it into oto (for yourself, or for the whole org).
- one key opens both APIs: the **Data API** (domain, backlinks and keyword data, paid in SE Ranking credits) and the **Project API** (your tracked projects and audits, within your subscription's limits) — there is no separate key per API
- your plan decides which side answers: a plan without Data API credits, or without project tracking, refuses the other side's calls
- byo only: no shared oto key, each person or org uses its own SE Ranking subscription
- "test the connection" reads the subscription and its Data API credits left: under one credit, it is not green

## usage — domain research, keywords, tracked projects

two tools: `seranking` reads (free), `seranking_act` spends Data API credits or starts something on the SE Ranking side — its own tool, the one your client asks you to confirm.
- "how many credits are left?" → `seranking(op="get_subscription")` (free)
- "SEO overview of this domain in France" → `seranking_act(op="domain_overview", source="fr", domain="example.com")` (100 credits); its history with `op="domain_overview_history"`
- "which keywords does it rank for / who are its competitors?" → `seranking_act(op="domain_keywords", source="fr", domain="example.com", limit=100)`, `op="domain_competitors"`
- "its backlink profile" → `seranking_act(op="backlinks_summary", target="example.com", mode="domain")`, then `op="list_backlinks"` or `op="list_referring_domains"` with `limit`
- "volume and difficulty of these keywords" → `seranking_act(op="keyword_metrics", source="fr", keywords=["logiciel crm", "crm gratuit"])` (10 credits per keyword); ideas with `op="similar_keywords"`, `"related_keywords"` or `"question_keywords"` and one `keyword`
- "my tracked projects and their positions" → `seranking(op="list_sites")`, then `op="get_site_positions"`, `"get_site_summary"`, `"list_site_keywords"` with its `site_id`; competitors with `op="list_competitors"` and `op="get_competitor_positions"`
- "recheck the positions now" → `seranking_act(op="recheck_positions", site_id=…, site_engine_id=…)` for a whole engine, or `keywords=[{"site_engine_id": 1, "keyword_id": 2}, …]` for some keywords (one of the two)
- "audit this site" → `seranking_act(op="create_audit", domain="example.com")`, then `seranking(op="get_audit_status", audit_id=…)` and `op="get_audit_report"`

## note — credits, databases, what is not exposed

- `source` is the regional database, a lowercase ISO country code (`fr`, `us`, `de`), `uk` for the United Kingdom
- Data API credits are billed by SE Ranking on successful answers only; per record for backlinks and keyword ideas: bound `limit` before scaling (at most 100 per call)
- keyword lists answer one page: pass `page` or `offset` yourself for the next one
- an answer with status 244 means the site is too large to be served by the API (e.g. a search engine's own domain)
- the project's guest share link is never returned
- not exposed: adding or deleting projects, keywords and competitors, keyword groups, tags, sub-accounts, exports
