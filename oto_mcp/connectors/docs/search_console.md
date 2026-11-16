## prerequisite — sign in with Google and authorize Search Console

from this card, click **connect**: Google asks you to authorize **Search Console read access only** (scope `webmasters.readonly`) on the account you choose. the Google account itself (address, token) is carried by the **Google account** connector — one account can authorize several services, one service at a time, without re-authorizing the others.
- pick the Google account that is a user of your Search Console properties: the tool sees exactly the properties **that Google user** can open, at the permission level Search Console gives it (Owner, Full, Restricted)
- **read-only**: performance, URL inspection and sitemaps are read; nothing is submitted, added or removed. to submit sitemaps from an agent, use the **Google Search Console** card (service account, `full` access level)
- no key, no service account to add to a property: the access is your own, and is cut off by removing the app from your Google account (myaccount.google.com > Security > Third-party connections) or by disconnecting the card
- several Google accounts: the tool acts on the one the call names with `_account=<email>` (the tool's `account=<email>` is the same choice — two different addresses are refused), otherwise the one the project pins, otherwise the default; `google_accounts` tells which ones have authorized Search Console

## usage — read a property's search performance

`search_console(op=…)`: `list_sites`, `query_search_analytics`, `inspect_url`, `list_sitemaps`, `get_sitemap` — the same ops and arguments as the service-account connector `google_search_console`.
- "which properties can I read?" → `search_console(op="list_sites")`: each `siteUrl` is the `site_url` of the other ops
- "top queries of October" → `search_console(op="query_search_analytics", site_url="sc-domain:example.com", start_date="2026-10-01", end_date="2026-10-31", dimensions=["query"], row_limit=100)`
- "is this page indexed?" → `search_console(op="inspect_url", site_url="sc-domain:example.com", inspection_url="https://www.example.com/pricing")`
- "which sitemaps are submitted?" → `search_console(op="list_sitemaps", site_url=…)`

## note — read a Search Console figure without getting it wrong

- a Domain property is named `sc-domain:example.com`, a URL-prefix property by its full URL **with its trailing slash**
- data lags about two days; `data_state="all"` adds the fresh, incomplete days
- `query_search_analytics` returns at most `row_limit` rows (up to 25,000): for more, call again with `start_row` = previous `start_row` + `row_limit`, until fewer rows come back
- URL inspection is bounded to 2,000 a day per property
