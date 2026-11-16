## prerequisite — a DataForSEO API login, or the shared key

DataForSEO is pay-as-you-go: no plan, no seat, a prepaid USD balance drawn per call (minimum top-up $50, the balance does not expire).
- your own key: in the [DataForSEO dashboard](https://app.dataforseo.com/api-access), copy the **API login** and the **API password** — not your sign-in password — into oto
- the platform key, when the instance has one, serves the orgs it is granted to; the super-admin posts it in /admin with both fields, API login and API password
- a new DataForSEO account must be verified in its dashboard before the data endpoints answer (until then: code 40104, `account_not_verified`; the balance still reads)
- "test the connection" reads the account and its balance: below $1 reads as not healthy
- `dataforseo(op="user_data")` reads the balance of YOUR key; it is refused on the shared key, whose account is the platform's

## usage — keywords, domains, backlinks, AI visibility, audits, technologies, Google results

one tool, `dataforseo`: `op` is the DataForSEO function, the other arguments are that function's own (a call that does not match answers the op's description, schema and an example).
- "how many searches for X, how hard is it?" → `dataforseo(op="keyword_overview", keywords=[…], location_code=2250, language_code="fr")`
- "find keyword ideas around X" → `op="keyword_ideas"` (same topic), `op="keyword_suggestions"` (contains X), `op="related_keywords"` (Google's related searches); "is X growing?" → `op="google_trends_explore"`
- "what does this site rank for, how much traffic, and since when?" → `op="domain_rank_overview"`, `op="ranked_keywords"`, `op="relevant_pages"`, `op="historical_rank_overview"`, with `target="example.com"`
- "who are its competitors, what do they rank for that we don't?" → `op="competitors_domain"`, then `op="domain_intersection"`; "who wins on THESE keywords?" → `op="serp_competitors", keywords=[…]`
- "traffic of these 200 domains" → `op="bulk_traffic_estimation", targets=[…]`, one call
- "who links to this site? who links to my competitors but not to me?" → `op="backlinks_summary"`, `op="referring_domains"`, then `op="backlinks_intersection"`; "what did we gain or lose?" → `op="backlinks_new_lost"`
- "do ChatGPT and Google's AI answers cite us?" → `op="llm_mentions_metrics"`, then `op="llm_mentions_search"` for the answers themselves; "ask a model what it recommends" → `op="llm_response"` (`op="llm_models"` lists the models)
- "what does the web say about our brand?" → `op="content_summary", keyword=…`
- "audit this page / this site" → `op="onpage_instant"` or `op="lighthouse"`, with `url=…`; a whole site: `op="onpage_crawl_start"`, `op="onpage_summary"` until the crawl is finished, then `op="onpage_pages"`, `"onpage_non_indexable"`, `"onpage_duplicate_tags"`, `"onpage_links"` or `"onpage_redirect_chains"`
- "what is this site built with? which French sites run Shopify?" → `op="domain_technologies", target=…`, `op="domains_by_technology"`
- "where does this site rank on Google, on Maps?" → `op="serp_organic"`, `op="serp_maps"`; a business profile and its reviews → `op="my_business_info"`, then `op="google_reviews_post"` and `op="google_reviews_get"` with its task id, until the reviews are ready

`location_code` is a DataForSEO code (2250 France, 2840 United States, 2826 United Kingdom), or `location_name` its English name; `language_code` (`fr`) or `language_name`. `op="labs_locations_and_languages"` lists them, free.

## note — cost and answers

every answer carries `cost`, the real amount DataForSEO charged, in USD; oto bills that amount. A refused call costs nothing.
- most calls cost about $0.01–0.02; AI-mention ops about $0.10, `historical_rank_overview` about $0.13; a site crawl about $0.00015 per page, paid at start, and reading its reports is free
- the answer is DataForSEO's, without its echo of the request and its timings: the rows are in `tasks[0].result`
- an error comes back named (`balance_exhausted`, `invalid_request`, `task_not_ready`…), even when DataForSEO answers it under HTTP 200
- figures are DataForSEO's estimates (its own index and clickstream), not the site's analytics; for the numbers a customer already trusts in Ahrefs or Ubersuggest, use those connectors with their own account
- the raw Google results page for one query, as a person sees it, is also `serper_search`

## note — the tools changed on 11/11/2026

DataForSEO is now served from its description: the eight tools `dataforseo_keywords`, `dataforseo_domain`, `dataforseo_backlinks`, `dataforseo_account`, `dataforseo_ai_visibility`, `dataforseo_web_mentions`, `dataforseo_site_audit` and `dataforseo_technologies` are now ONE tool, `dataforseo`. An old name refuses, naming its new call; saved tool lists were rewritten to `dataforseo`.
- the `op` is the function: `dataforseo_keywords(op="overview")` → `dataforseo(op="keyword_overview")`, `dataforseo_domain(op="keywords")` → `op="ranked_keywords"`, `dataforseo_backlinks(op="list")` → `op="backlinks_list"`, `dataforseo_account(op="balance")` → `op="user_data"`, `dataforseo_site_audit(op="crawl_results", report="pages")` → `op="onpage_pages"`, `dataforseo_technologies(op="domain")` → `op="domain_technologies"`
- the arguments are DataForSEO's: `location` becomes `location_code` or `location_name`, `language` becomes `language_code` or `language_name`; `full` and `options` are gone (the answer is DataForSEO's, trimmed of its echo); `cost_usd` is now `cost`
